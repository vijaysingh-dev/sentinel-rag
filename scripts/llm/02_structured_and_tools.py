"""Structured output and tool calling, compared across providers.

    python scripts/llm/02_structured_and_tools.py --provider anthropic
    python scripts/llm/02_structured_and_tools.py --provider local

Worth running on a local model before you assume provider parity. Small local models will produce
the structured output most of the time and pick the wrong tool a meaningful fraction of the time,
which is exactly the failure that makes an agent graph loop. Frontier hosted models are noticeably
more reliable here, and knowing the size of that gap on your own schemas is the difference between
a considered choice and an assumption.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider


class Triage(BaseModel):
    """Structured triage of an incoming anomaly alert."""

    # Rationale first so the model reasons before committing to a label — the same ordering used
    # throughout the evaluation and verification code, for the same reason.
    reasoning: str = Field(max_length=300)
    severity: Literal["p1", "p2", "p3", "noise"]
    likely_area: Literal["checkout", "payment", "kafka", "infra", "unknown"]
    needs_human: bool
    confidence: float = Field(ge=0, le=1)


@tool
def search_knowledge(query: str, doc_type: str | None = None) -> str:
    """Search runbooks, methodology documents and past incidents.

    Args:
        query: What you need to find.
        doc_type: Optional filter: runbook, methodology or incident.
    """
    return "stub"


@tool
def query_prometheus(expr: str, purpose: str, lookback_minutes: int = 30) -> str:
    """Run a single PromQL query against the metrics backend.

    Args:
        expr: A PromQL expression.
        purpose: What this query establishes.
        lookback_minutes: How far back the query window extends.
    """
    return "stub"


@tool
def recent_deployments(service: str | None = None, hours: int = 24) -> str:
    """List deployments and configuration changes in a recent window.

    Args:
        service: Optional service filter.
        hours: Lookback window.
    """
    return "stub"


ALERT = """checkout error ratio crossed its alert threshold at 09:14:30, rising to 4.2% against a
0.5% baseline. Trace inspection shows the error status originating in the payment span, not in
checkout itself."""

TOOL_CASES = [
    ("Has this happened to payment before?", "search_knowledge"),
    ("Compare today's checkout error rate against the last five days.", "query_prometheus"),
    ("Did anything ship to payment this morning?", "recent_deployments"),
]


async def run(provider: str | None) -> int:
    llm = get_provider("llm", provider)
    print(f"provider: {llm.describe()}\n")

    print("=" * 66)
    print("structured output")
    print("=" * 66)
    try:
        triage: Triage = await llm.structured(Triage, cheap=True).ainvoke(f"Triage this alert.\n\n{ALERT}")
        print(f"  severity     {triage.severity}")
        print(f"  area         {triage.likely_area}")
        print(f"  needs human  {triage.needs_human}")
        print(f"  confidence   {triage.confidence:.2f}")
        print(f"  reasoning    {triage.reasoning[:200]}")
    except Exception as exc:
        print(f"  FAILED — {type(exc).__name__}: {str(exc)[:200]}")
        print("  A model that cannot hold a schema will make the agent graph loop on repair.")

    print("\n" + "=" * 66)
    print("tool selection")
    print("=" * 66)
    bound = llm.chat.bind_tools([search_knowledge, query_prometheus, recent_deployments])

    correct = 0
    for question, expected in TOOL_CASES:
        response = await bound.ainvoke(question)
        calls = [c["name"] for c in (response.tool_calls or [])]
        chosen = calls[0] if calls else "(none)"
        ok = chosen == expected
        correct += ok
        print(f"  {'ok ' if ok else 'MISS'} {question[:48]:50s} -> {chosen}")

    print(f"\n  routing accuracy: {correct}/{len(TOOL_CASES)}")
    print("  This is the number that decides whether a multi-agent graph is worth building on this model.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=None, choices=["local", "openai", "anthropic", "google"])
    args = parser.parse_args()

    configure_logging()
    return asyncio.run(run(args.provider))


if __name__ == "__main__":
    sys.exit(main())
