"""Drafting, human approval and publication.

`interrupt()` is the piece worth understanding. When the writer reaches it, LangGraph persists the
entire graph state to the checkpointer and the call returns control to the caller. No worker, no
thread and no memory is held while a human takes four hours to look at it — the run resumes from
the checkpoint when a decision arrives, on a different process if necessary.

That is what makes human-in-the-loop viable in production rather than a demo. Polling loops and
held connections do not survive a deploy, a pod eviction, or an analyst going to lunch.

Publication is the only write in the system and it sits behind that interrupt. In a regulated
environment that is a business decision, not a technical one, and it is the right one.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.types import Command, interrupt

from sentinel.core.observability import log
from sentinel.core.registry import get_provider
from sentinel.core.resilience import with_retry
from sentinel.rca.prompts import load_prompt
from sentinel.rca.state import RCADraft, SentinelState

# Ceiling on confidence when the run degraded. A partial investigation may be right, but it may not
# claim to be sure — the analyst needs to see the difference at a glance.
DEGRADED_CONFIDENCE_CAP = 0.6


@lru_cache
def _writer():
    return get_provider("llm").structured(RCADraft)


async def writer_node(state: SentinelState) -> Command:
    """Produces the draft RCA from accumulated evidence."""
    evidence = state.get("evidence", [])

    if not evidence:
        log.warning("no_evidence_to_draft", anomaly=state["anomaly_id"])
        return Command(
            goto="__end__",
            update={
                "outcome": "inconclusive",
                "errors": [{"node": "writer", "kind": "no_evidence"}],
            },
        )

    evidence_text = "\n".join(
        f"[{e.id}] ({e.agent} via {e.source_tool}, ref {e.source_ref}, conf {e.confidence:.2f})\n    {e.claim}"
        for e in evidence
    )

    prompt = load_prompt("writer", "v3").format(
        evidence=evidence_text,
        notes="\n".join(state.get("verifier_notes", [])) or "none",
    )

    if state.get("degraded"):
        failed = ", ".join(sorted({e.get("node", "?") for e in state.get("errors", [])}))
        prompt += (
            f"\n\nThis run was DEGRADED. These checks did not complete: {failed}. "
            f"State this explicitly and keep confidence at or below "
            f"{DEGRADED_CONFIDENCE_CAP}."
        )

    try:
        draft: RCADraft = await with_retry(lambda: _writer().ainvoke(prompt), operation="writer")
    except Exception as exc:
        log.error("drafting_failed", anomaly=state["anomaly_id"], error=type(exc).__name__)
        return Command(
            goto="__end__",
            update={
                "outcome": "failed",
                "errors": [{"node": "writer", "kind": "drafting_failed"}],
            },
        )

    # Enforced in code, not left to the prompt: a degraded run cannot present itself as confident
    # no matter what the model wrote.
    if state.get("degraded") and draft.confidence > DEGRADED_CONFIDENCE_CAP:
        draft.confidence = DEGRADED_CONFIDENCE_CAP

    # Evidence ids the model cited that do not exist are dropped rather than published.
    valid_ids = {e.id for e in evidence}
    dropped = [eid for eid in draft.evidence_ids if eid not in valid_ids]
    if dropped:
        log.warning("dropped_invalid_evidence_ids", anomaly=state["anomaly_id"], ids=dropped)
        draft.evidence_ids = [eid for eid in draft.evidence_ids if eid in valid_ids]

    log.info(
        "drafted",
        anomaly=state["anomaly_id"],
        severity=draft.severity,
        confidence=draft.confidence,
        evidence_cited=len(draft.evidence_ids),
    )

    return Command(goto="approval", update={"draft": draft})


async def approval_node(state: SentinelState) -> Command:
    """Pauses for a human decision.

    `interrupt()` raises out of the node after checkpointing. The value passed to it is what the
    reviewing UI displays; the value returned when the graph is resumed is the analyst's decision.
    """
    draft = state.get("draft")
    if draft is None:
        return Command(goto="__end__", update={"outcome": "failed"})

    decision = interrupt(
        {
            "anomaly_id": state["anomaly_id"],
            "draft": draft.model_dump(),
            "degraded": state.get("degraded", False),
            "retrieval_mode": state.get("retrieval_mode", "full"),
            "evidence": [e.model_dump() for e in state.get("evidence", [])],
            "usd_spent": round(state.get("usd_spent", 0.0), 4),
            "actions": ["approve", "edit", "reject"],
        }
    )

    action = (decision or {}).get("action", "reject")
    reviewer = (decision or {}).get("reviewer", "unknown")

    log.info("human_decision", anomaly=state["anomaly_id"], action=action, reviewer=reviewer)

    if action == "reject":
        # A rejection is a free human label — the strongest online quality signal available, and
        # the reason `evaluation/` does not depend solely on LLM judges.
        return Command(
            goto="__end__",
            update={
                "outcome": "rejected",
                "approved_by": reviewer,
                "verifier_notes": [
                    *state.get("verifier_notes", []),
                    f"rejected by {reviewer}: {(decision or {}).get('reason', '')}",
                ],
            },
        )

    if action == "edit":
        edited = (decision or {}).get("draft")
        if edited:
            draft = RCADraft(**edited)

    return Command(goto="publish", update={"draft": draft, "approved_by": reviewer})


async def publish_node(state: SentinelState) -> Command:
    """The single write in the system.

    Reached only through `approval_node`, and `approved_by` is asserted here as well: a graph edit
    that accidentally routed around the interrupt would otherwise publish silently.
    """
    if not state.get("approved_by"):
        log.critical("publish_without_approval", anomaly=state["anomaly_id"])
        return Command(
            goto="__end__",
            update={
                "outcome": "failed",
                "errors": [{"node": "publish", "kind": "unapproved"}],
            },
        )

    draft = state.get("draft")
    try:
        from sentinel.rca.clients import itsm_client

        ticket = await itsm_client().attach_rca(
            anomaly_id=state["anomaly_id"],
            tenant_id=state["tenant_id"],
            draft=draft.model_dump() if draft else {},
            approved_by=state["approved_by"],
        )
    except Exception as exc:
        # The analyst already approved; losing the write is recoverable and must not look like a
        # rejection. The draft stays in state for a retry.
        log.error("publish_failed", anomaly=state["anomaly_id"], error=type(exc).__name__)
        return Command(
            goto="__end__",
            update={
                "outcome": "publish_failed",
                "errors": [{"node": "publish", "kind": type(exc).__name__}],
            },
        )

    log.info("published", anomaly=state["anomaly_id"], ticket=ticket, approved_by=state["approved_by"])
    return Command(goto="__end__", update={"outcome": "published"})
