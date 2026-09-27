"""Shared specialist machinery.

The design decision that makes the multi-agent split pay for itself: **specialists return
structured evidence to shared state, but only a short prose summary to the supervisor's message
list.** Keeping full tool transcripts out of the orchestrator's context is the entire point —
otherwise the supervisor drowns in the same tool output the specialists were meant to absorb, and
you have paid for extra hops to get a worse single-agent system.

`extract_evidence` is the mechanical half of grounding. A claim citing a reference that never
appeared in a real tool result is dropped here rather than being left for the verifier to argue
with, which turns "did the model make this up?" from a judgement call into a set membership test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from sentinel.core.observability import estimate_cost, log, trace_config
from sentinel.rca.guard import with_node_guard
from sentinel.rca.state import Evidence, SentinelState

# Prefixes of citation handles the tools emit. A ref outside this set never came from a tool.
#   doc      search_knowledge          promql    query_prometheus
#   trace    find_traces               flags     current_flags
#   flagchg  flag_changes              deploy    recent_deployments
#   inclog   incident_history
REF_PREFIXES = {"doc", "promql", "trace", "flags", "flagchg", "deploy", "inclog"}


class ExtractedClaim(BaseModel):
    claim: str = Field(max_length=400, description="One factual finding, stated precisely.")
    source_tool: str
    source_ref: str = Field(description="The `ref` value returned by the tool call.")
    raw_excerpt: str = Field(max_length=1200, description="Verbatim slice of the tool output.")
    confidence: float = Field(ge=0, le=1)


class ExtractionResult(BaseModel):
    """Findings distilled from a specialist's transcript, each tied to a tool reference."""

    claims: list[ExtractedClaim] = []
    summary: str = Field(max_length=900)


def build_task_brief(state: SentinelState, focus: str) -> str:
    """Assembles the specialist's instruction.

    Prior evidence is included so specialists do not redo each other's work, but capped at the most
    recent items — an uncapped digest grows until it crowds out the actual task.
    """
    anomaly = state.get("anomaly", {})
    prior = state.get("evidence", [])
    prior_digest = "\n".join(f"- [{e.id}] ({e.agent}) {e.claim}" for e in prior[-12:]) or "none yet"
    notes = "\n".join(state.get("verifier_notes", [])) or "none"

    return (
        f"Anomaly {state['anomaly_id']}:\n{json.dumps(anomaly, indent=2, default=str)}\n\n"
        f"Your focus for this round: {focus}\n\n"
        f"Evidence already gathered by other specialists:\n{prior_digest}\n\n"
        f"Outstanding reviewer questions:\n{notes}\n\n"
        "Investigate, then state what you established and what you could not."
    )


def observed_refs(messages: list) -> set[str]:
    """Every `ref` that actually appeared in a tool result during this specialist's run.

    Scanning the raw ToolMessage content rather than trusting the model's report is the point: this
    set is ground truth about what the tools returned, and any claimed ref outside it is fabricated.
    """
    refs: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        content = message.content if isinstance(message.content, str) else json.dumps(message.content)
        for token in content.replace('"', " ").replace(",", " ").split():
            cleaned = token.strip("}{ '[]")
            if ":" in cleaned and cleaned.split(":")[0] in REF_PREFIXES:
                refs.add(cleaned)
    return refs


def usage_of(messages: list, model: str) -> tuple[int, float]:
    """Sums token usage and estimated cost across a specialist's messages."""
    tokens = 0
    usd = 0.0
    for message in messages:
        meta = getattr(message, "usage_metadata", None)
        if not meta:
            continue
        tokens += meta.get("total_tokens", 0)
        usd += estimate_cost(model, meta.get("input_tokens", 0), meta.get("output_tokens", 0))
    return tokens, usd


async def extract_evidence(messages: list, agent: str) -> tuple[list[Evidence], str]:
    """Distils a transcript into referenced claims, dropping any whose ref was never observed."""
    from sentinel.core.registry import get_provider

    transcript = "\n\n".join(f"{type(m).__name__}: {m.content}"[:4000] for m in messages if getattr(m, "content", None))

    extractor = get_provider("llm").structured(ExtractionResult, cheap=True)
    result: ExtractionResult = await extractor.ainvoke(
        "Extract the verifiable findings from this investigation transcript. Every claim must "
        "cite the `ref` returned by the tool call that produced it, and include a verbatim "
        "excerpt. Omit anything the tools did not actually show. Keep only the findings that "
        "bear on the root cause — at most 6 claims, each excerpt under 300 characters.\n\n" + transcript
    )

    valid_refs = observed_refs(messages)
    evidence: list[Evidence] = []

    for i, claim in enumerate(result.claims):
        if valid_refs and claim.source_ref not in valid_refs:
            log.warning("dropped_unreferenced_claim", agent=agent, ref=claim.source_ref)
            continue
        digest = hashlib.sha256(claim.claim.encode()).hexdigest()[:6]
        evidence.append(
            Evidence(
                id=f"{agent}-{i}-{digest}",
                claim=claim.claim,
                source_tool=claim.source_tool,
                source_ref=claim.source_ref,
                raw_excerpt=claim.raw_excerpt,
                agent=agent,
                confidence=claim.confidence,
            )
        )

    return evidence, result.summary


def make_specialist(name: str, agent_factory: Callable[[], Any], focus: str):
    """Builds a guarded graph node from a specialist agent factory.

    Takes a factory rather than a compiled agent so the LLM provider is resolved on first
    invocation, not at import. That keeps `graph.py` importable — and therefore testable — without
    a reachable model endpoint.

    The guard supplies the timeout, circuit breaker and degradation path, so a specialist that
    hangs or whose dependency is down hands control back to the supervisor with the run marked
    degraded rather than killing the investigation.
    """

    @with_node_guard(name)
    async def node(state: SentinelState) -> Command:
        result = await agent_factory().ainvoke(
            {"messages": [("user", build_task_brief(state, focus))]},
            config={
                "recursion_limit": 8,
                **trace_config(dict(state), node=name),
            },
        )
        messages = result["messages"]
        evidence, summary = await extract_evidence(messages, agent=name)
        # Priced as the chat model that produced the transcript. This used to pass the *embedding*
        # model's name, which is not in PRICING, so every run cost $0 and the max_usd_per_run
        # guard in rca/guard.py could never trip.
        from sentinel.core.registry import get_provider

        tokens, usd = usage_of(messages, get_provider("llm").describe().get("chat", "local"))

        log.info(
            "specialist_done",
            node=name,
            anomaly=state["anomaly_id"],
            evidence=len(evidence),
            tokens=tokens,
            usd=round(usd, 4),
        )

        return Command(
            goto="supervisor",
            update={
                "evidence": evidence,
                "completed_agents": [name],
                # Only the summary reaches the supervisor's context — the transcript stays here.
                "messages": [("assistant", f"[{name}] {summary}")],
                "tokens_used": tokens,
                "usd_spent": usd,
            },
        )

    return node
