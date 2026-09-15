"""Orchestrator.

The supervisor holds no specialist tools by construction. The prompt says so as well, but the
enforcement that survives six months of prompt edits is the route-accuracy evaluator in
`tests/test_trajectory.py` — a constraint you can only assert in a test is a constraint you do not
have.

Parallel dispatch is the default early in an investigation: knowledge, metrics, tracing and
change are independent lines of enquiry, and running them concurrently is why the state reducers
in `state.py` exist.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from sentinel.core.observability import log
from sentinel.core.registry import get_provider
from sentinel.rca.guard import budget_exceeded, structured_with_repair
from sentinel.rca.prompts import load_prompt
from sentinel.rca.state import SentinelState

AGENT_NODES = {
    "knowledge": "knowledge_node",
    "metrics": "metrics_node",
    "tracing": "tracing_node",
    "change": "change_node",
}


class RouteDecision(BaseModel):
    """Which specialists run next, and whether the evidence is sufficient to draft."""

    # Reasoning first: the model writes its justification before committing to a route, which
    # measurably improves routing quality over emitting the decision first.
    reasoning: str = Field(max_length=400)
    next_agents: list[Literal["knowledge", "metrics", "tracing", "change"]] = Field(
        default_factory=list,
        description="Specialists to run concurrently this round. Empty when ready to draft.",
    )
    ready_to_draft: bool


@lru_cache
def _router():
    """Routing runs on the cheap model.

    Routing is classification, not analysis, and it happens on every loop iteration — so it is the
    single highest-volume call in the system and the obvious place to economise.
    """
    return get_provider("llm").structured(RouteDecision, cheap=True)


def _digest(state: SentinelState) -> str:
    items = state.get("evidence", [])
    if not items:
        return "none yet"
    return "\n".join(f"- [{e.id}] ({e.agent}, conf {e.confidence:.2f}) {e.claim}" for e in items)


async def supervisor_node(state: SentinelState) -> Command:
    # Budget is checked before the routing call so an exhausted run does not spend more to notice.
    reason = budget_exceeded(state)
    if reason:
        log.warning("budget_exhausted", anomaly=state["anomaly_id"], reason=reason)
        return Command(
            goto="writer",
            update={"degraded": True, "errors": [{"node": "supervisor", "kind": reason}]},
        )

    prompt = load_prompt("supervisor", "v7").format(
        anomaly=json.dumps(state.get("anomaly", {}), indent=2, default=str),
        evidence_count=len(state.get("evidence", [])),
        evidence_digest=_digest(state),
        completed=", ".join(state.get("completed_agents", [])) or "none",
        verifier_notes="\n".join(state.get("verifier_notes", [])) or "none",
    )

    try:
        decision: RouteDecision = await structured_with_repair(_router(), prompt)
    except Exception as exc:
        # A router that cannot produce a valid decision must not stall the graph; drafting from
        # whatever evidence exists is more useful than an error.
        log.error("routing_failed", anomaly=state["anomaly_id"], error=type(exc).__name__)
        return Command(
            goto="writer",
            update={
                "degraded": True,
                "errors": [{"node": "supervisor", "kind": "routing_failed"}],
            },
        )

    log.info(
        "routed",
        anomaly=state["anomaly_id"],
        next=decision.next_agents,
        ready=decision.ready_to_draft,
        reasoning=decision.reasoning[:160],
    )

    if decision.ready_to_draft or not decision.next_agents:
        goto = "verifier" if state.get("evidence") else "writer"
        return Command(goto=goto, update={"step_count": state.get("step_count", 0) + 1})

    # Re-running a specialist that already reported wastes budget without adding evidence, unless
    # a reviewer question specifically asked for it — which arrives as a verifier note.
    already_done = set(state.get("completed_agents", []))
    pending = [a for a in decision.next_agents if a not in already_done or state.get("verifier_notes")]

    if not pending:
        return Command(goto="verifier", update={"step_count": state.get("step_count", 0) + 1})

    # Send fans out to several nodes in one superstep; the state reducers merge their writes.
    return Command(
        goto=[Send(AGENT_NODES[a], state) for a in pending],
        update={"step_count": state.get("step_count", 0) + 1, "plan": [*state.get("plan", []), *pending]},
    )
