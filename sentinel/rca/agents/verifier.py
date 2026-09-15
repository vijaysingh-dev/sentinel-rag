"""Adversarial review before a human sees the draft.

The verifier exists because the failure that matters is not an obviously wrong answer — it is a
fluent, well-cited, confidently wrong one. A separate pass with an adversarial prompt catches a
meaningful share of those, and the ones it does not catch it at least flags as low confidence.

Bounded to two rounds. Unbounded review loops are a cost sink and, past the second pass, the
verifier starts inventing objections rather than finding real ones.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.types import Command
from pydantic import BaseModel, Field

from sentinel.core.observability import log
from sentinel.core.registry import get_provider
from sentinel.core.resilience import with_retry
from sentinel.rca.prompts import load_prompt
from sentinel.rca.state import SentinelState

MAX_ROUNDS = 2


class VerificationResult(BaseModel):
    # Rationale before verdict, for the same reason as everywhere else in this codebase.
    rationale: str = Field(max_length=600)
    all_claims_grounded: bool
    causation_overstated: bool = Field(description="True when a correlation is presented as a cause.")
    action_specific_enough: bool
    confidence_justified: bool
    questions: list[str] = Field(
        default_factory=list, max_length=5, description="Specific gaps the specialists should close."
    )
    approved: bool


@lru_cache
def _verifier():
    """Verification runs on the strong model.

    This is the last automated check before a human, and the one place where using the cheap model
    is a false economy — a weak verifier approves exactly the confident-wrong drafts it exists to
    catch.
    """
    return get_provider("llm").structured(VerificationResult)


async def verifier_node(state: SentinelState) -> Command:
    rounds = state.get("verifier_rounds", 0)

    if rounds >= MAX_ROUNDS:
        log.info("verifier_rounds_exhausted", anomaly=state["anomaly_id"])
        return Command(
            goto="writer",
            update={
                "verifier_notes": [
                    *state.get("verifier_notes", []),
                    "review rounds exhausted; drafted with outstanding questions",
                ],
            },
        )

    evidence = state.get("evidence", [])
    evidence_text = (
        "\n".join(
            f"[{e.id}] ({e.source_tool} → {e.source_ref}, conf {e.confidence:.2f}) {e.claim}\n"
            f"    excerpt: {e.raw_excerpt[:300]}"
            for e in evidence
        )
        or "no evidence gathered"
    )

    draft = state.get("draft")
    draft_text = draft.model_dump_json(indent=2) if draft else "no draft yet"

    prompt = (
        f"{load_prompt('verifier', 'v3')}\n\n"
        f"Evidence:\n{evidence_text}\n\n"
        f"Draft:\n{draft_text}\n\n"
        f"Run was degraded: {state.get('degraded', False)}\n"
        f"Retrieval mode: {state.get('retrieval_mode', 'full')}"
    )

    try:
        result: VerificationResult = await with_retry(
            lambda: _verifier().ainvoke(prompt), operation="verifier"
        )
    except Exception as exc:
        # A failed verification must not block the run, but it must be visible: the draft proceeds
        # marked degraded rather than silently unreviewed.
        log.error("verification_failed", anomaly=state["anomaly_id"], error=type(exc).__name__)
        return Command(
            goto="writer",
            update={
                "degraded": True,
                "errors": [{"node": "verifier", "kind": "verification_failed"}],
            },
        )

    log.info(
        "verified",
        anomaly=state["anomaly_id"],
        approved=result.approved,
        grounded=result.all_claims_grounded,
        questions=len(result.questions),
    )

    if result.approved:
        return Command(goto="writer", update={"verifier_rounds": rounds + 1})

    # Unapproved: send the questions back to the supervisor, which decides who can answer them.
    return Command(
        goto="supervisor",
        update={
            "verifier_rounds": rounds + 1,
            "verifier_notes": [*state.get("verifier_notes", []), *result.questions],
        },
    )
