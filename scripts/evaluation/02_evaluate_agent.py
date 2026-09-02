"""Run the RCA agent against recorded incident fixtures and score its trajectory.

    python scripts/evaluation/02_evaluate_agent.py
    python scripts/evaluation/02_evaluate_agent.py --cases data/agent_cases.jsonl --llm local

Each case in data/agent_cases.jsonl binds a fixture from data/fixtures/ (recorded tool output for
one fault scenario) and runs the compiled graph up to the human interrupt. The fixtures make this
deterministic in the *tools* — every tool returns exactly what the fault model says it should —
so what is being measured is the agent's reasoning: did it call the right specialists, did it
avoid loops, did it draft a finding, and did the finding name the service the incident truth says
was the cause.

This is the agent-layer counterpart to `01_evaluate_stages.py`. It still needs an LLM (the
specialists reason), so it is not part of the deterministic CI gate — run it nightly, or on a
branch that touches `rca/`.

Why fixtures rather than a live otel-demo: a live run is not reproducible (the incident-generator
picks scenarios at random) and not a CI artifact. Live runs are still valuable — capture the
interesting failures and add them here as new cases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from sentinel.core.observability import configure_logging, log
from sentinel.evaluation.agent_eval import evaluate_agent
from sentinel.rca.graph import build_graph
from sentinel.rca.runtime import fixtures, request_context
from sentinel.rca.state import new_state

REPO_ROOT = Path(__file__).resolve().parents[2]


def _collect_tool_calls(messages: list) -> tuple[list[dict], set[str]]:
    """Pull (name, args) for every tool call, and the set of refs the tool results carried."""
    calls: list[dict] = []
    refs: set[str] = set()
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls.append({"name": tc["name"], "args": tc.get("args", {})})
        content = getattr(m, "content", "")
        if isinstance(content, str) and '"ref"' in content:
            try:
                payload = json.loads(content)
            except ValueError:
                continue
            for item in payload if isinstance(payload, list) else [payload]:
                if isinstance(item, dict) and isinstance(item.get("ref"), str):
                    refs.add(item["ref"])
    return calls, refs


def _make_runner(graph):
    async def run(case: dict):
        fixture_path = REPO_ROOT / case["fixture"]
        anomaly_id = case["id"]
        config = {"configurable": {"thread_id": anomaly_id}}

        with fixtures(fixture_path), request_context("default", case.get("roles", ["admin"])):
            state = new_state(anomaly_id, "default", case["anomaly"], case.get("roles", ["admin"]))
            # ainvoke runs until the graph ends or hits the approval interrupt; either way the
            # draft and the full message history are in the returned state.
            final = await graph.ainvoke(state, config=config)

        messages = final.get("messages", [])
        tool_calls, refs = _collect_tool_calls(messages)

        # Normalise the outcome for the evaluator: reaching the interrupt with a draft is success.
        if final.get("draft") is not None and final.get("outcome") in (None, "running"):
            final = {**final, "outcome": "drafted"}
        return final, tool_calls, refs

    return run


def _finding_names_root_cause(state: dict, ground_truth: dict) -> bool:
    draft = state.get("draft")
    if draft is None:
        return False
    text = " ".join(
        str(getattr(draft, f, "") or "") for f in ("summary", "root_cause", "cause", "recommendation")
    ).lower()
    return ground_truth["root_cause_service"] in text or ground_truth["root_cause_flag"].lower() in text


async def run(args: argparse.Namespace) -> int:
    configure_logging()
    cases = [json.loads(line) for line in Path(args.cases).read_text().splitlines() if line.strip()]
    print(f"{len(cases)} agent cases from {args.cases}\n")

    graph = build_graph(MemorySaver())
    result = await evaluate_agent(cases, _make_runner(graph))

    # the root-cause-accuracy check is specific to these fixtures, so score it here
    correct_cause = 0
    graph2 = build_graph(MemorySaver())
    runner = _make_runner(graph2)
    for case in cases:
        try:
            state, _, _ = await runner(case)
        except Exception:
            continue
        if _finding_names_root_cause(state, case["ground_truth"]):
            correct_cause += 1

    print("=" * 60)
    print("trajectory")
    print("=" * 60)
    t = result.trajectory
    print(f"  route accuracy      {t.route_accuracy:.3f}   (did it call the right specialists)")
    print(f"  tool precision      {t.tool_precision:.3f}   (did it avoid wandering)")
    print(f"  avg steps           {t.step_count}")
    print(f"  repeated calls      {t.repeated_calls}")
    print(f"  outcome score       {result.outcome_score:.3f}   (reached a draft)")
    print(f"  root-cause accuracy {correct_cause / max(len(cases), 1):.3f}   (draft names the true cause)")

    if result.safety_violations:
        print(f"\n  SAFETY VIOLATIONS: {len(result.safety_violations)}")
        for v in result.safety_violations[:10]:
            print(f"    {v.get('case')}: {v['kind']} — {v.get('detail', '')}")
    else:
        print("\n  no safety violations")

    print("\n  per case:")
    for row in result.per_case:
        flag = " !" if row.get("violations") else ""
        print(
            f"    {row.get('id', '?'):40s} route={row.get('route_accuracy', 0):.2f} "
            f"steps={row.get('steps', 0):2d} outcome={row.get('outcome', 0):.0f}{flag}"
        )

    log.info(
        "agent_eval_summary",
        route=round(t.route_accuracy, 3),
        outcome=round(result.outcome_score, 3),
        root_cause=round(correct_cause / max(len(cases), 1), 3),
        violations=len(result.safety_violations),
    )

    # fail if the agent drafts an unsafe finding or never reaches a draft
    ok = not result.safety_violations and result.outcome_score >= 0.6
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="data/agent_cases.jsonl")
    parser.add_argument("--llm", default=None, help="override llm_provider for this run")
    args = parser.parse_args()

    if args.llm:
        import os

        os.environ["LLM_PROVIDER"] = args.llm

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
