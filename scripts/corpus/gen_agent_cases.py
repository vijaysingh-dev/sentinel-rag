"""Generate agent trajectory-evaluation cases and their recorded tool fixtures.

    uv run python scripts/corpus/gen_agent_cases.py

For each fault in scripts/corpus/services.yaml this writes:

* one row in ``data/agent_cases.jsonl`` — the anomaly the agent is handed, the tools a competent
  investigation should call (`expected_tools`), and the expected final outcome;
* one file ``data/fixtures/<scenario>.json`` — recorded output for every tool the agent might
  call during that investigation, keyed by tool name, in the shape `sentinel/rca/runtime.fixtures`
  binds. This is what lets `make eval-agent` replay a full investigation with no Prometheus,
  Jaeger or flagd running.

The recorded output is synthesised from the fault model to be *consistent with the ground truth*:
the metrics fixture shows the alerting service's error ratio raised, the trace fixture shows the
error span in the root-cause service, and the flag fixture shows exactly the flag the incident
truth says was set. An agent that reads these and still names the wrong service is wrong for a
reason worth surfacing, not because the fixture was ambiguous.

This is the reproducible-fixture form of a recorded investigation. Real trajectory eval also
captures live runs against a real incident and adds the interesting failures to the case set —
`data/fixtures/` is where those would live too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _model import REPO_ROOT, Platform, load

sys.path.insert(0, str(Path(__file__).parent))

TRUTH = Path(__file__).parent / "_incident_truth.jsonl"
CASES_OUT = REPO_ROOT / "data" / "agent_cases.jsonl"
FIXTURE_DIR = REPO_ROOT / "data" / "fixtures"

# The tools a sound investigation of a flag-injected fault touches. knowledge to find the runbook,
# metrics to see which service moved, tracing to localise the span, change to find the flag.
EXPECTED_TOOLS = ["search_knowledge", "query_prometheus", "find_traces", "current_flags"]


def _metrics_fixture(p: Platform, root: str, surface: str, is_latency: bool) -> dict:
    """A Prometheus range summary consistent with the fault: the surface service degraded,
    the root-cause service degraded harder, healthy services flat."""

    def series(service: str, degraded: bool) -> dict:
        if is_latency:
            base, now = (0.12, 0.9) if degraded else (0.11, 0.12)
            labels = {"service_name": service}
        else:
            base, now = (0.002, 0.34) if degraded else (0.001, 0.0015)
            labels = {"service_name": service}
        return {
            "labels": labels,
            "first": base,
            "last": now,
            "min": base,
            "max": now,
            "direction": "rising" if degraded else "flat",
            "from": "2026-06-01T14:00:00+00:00",
            "to": "2026-06-01T14:30:00+00:00",
            "points": 60,
        }

    healthy = [d for d in p.services[surface].depends_on if d != root][:2]
    return {
        "ref": "promql:fixture0001",
        "expr": f'error ratio / duration for {surface} and its dependencies',
        "window_minutes": 30,
        "result": {
            "series": [series(surface, True), series(root, True)]
            + [series(h, False) for h in healthy],
            "series_count": 2 + len(healthy),
        },
    }


def _trace_fixture(p: Platform, root: str, surface: str, is_latency: bool) -> dict:
    op = "grpc.execute" if root != surface else "checkout"
    return {
        "ref": "trace:fixture0001",
        "service": surface,
        "count": 3,
        "traces": [
            {
                "trace_id": "abc123def456",
                "found": True,
                "duration_ms": 1840.0 if is_latency else 210.0,
                "span_count": 12,
                "services": sorted({surface, root, *p.services[surface].depends_on}),
                "slowest_span": {
                    "service": root,
                    "operation": op,
                    "duration_ms": 1600.0 if is_latency else 40.0,
                    "share_of_trace": 0.87 if is_latency else 0.19,
                },
                "error_spans": (
                    []
                    if is_latency
                    else [{"service": root, "operation": op, "message": "fault injected via flag"}]
                ),
            }
        ],
        "instruction": "Cite trace:<id> for any claim about a specific span.",
    }


def _flags_fixture(root_flag: str, variant: str) -> dict:
    return {
        "ref": "flags:current",
        "flags": {
            "flags": [
                {
                    "flag": root_flag,
                    "current_variant": variant,
                    "resting_variant": "off",
                    "active": True,
                    "value": variant,
                }
            ],
            "any_active": True,
        },
    }


def _knowledge_fixture(p: Platform, root: str, root_flag: str, signature: str) -> list[dict]:
    svc = p.services[root]
    return [
        {
            "ref": "doc:runbook-" + root,
            "title": f"{root} runbook > Known failure modes > {root_flag}",
            "doc_type": "runbook",
            "score": 0.82,
            "text": (
                f"{root_flag}. Effect: fault injected. Signature: {signature} "
                f"Response: confirm the flag is set by querying flagd, then reset it. "
                f"Recovery is immediate on the next flag evaluation."
            ),
        },
        {
            "ref": "doc:incident-" + root,
            "title": f"Past incident: {root} {root_flag}",
            "doc_type": "incident",
            "score": 0.74,
            "text": (
                f"The {root_flag} feature flag was set to its fault variant. {svc.name} was "
                f"healthy; the failure was injected at the flag layer. Alert fired first on a "
                f"downstream service."
            ),
        },
    ]


def _incident_history_fixture(root: str, root_flag: str) -> dict:
    return {
        "ref": "inclog:10",
        "count": 1,
        "incidents": [
            {
                "ts": "2026-06-01T14:29:00+00:00",
                "scenario": f"{root}_fault",
                "flag": root_flag,
                "event": "incident_start",
                "variant": "on",
                "note": "scheduled fault injection",
            }
        ],
    }


def main() -> int:
    if not TRUTH.exists():
        print("scripts/corpus/_incident_truth.jsonl missing — run gen_incidents.py first", file=sys.stderr)
        return 1

    p = load()
    truth = [json.loads(line) for line in TRUTH.read_text().splitlines() if line.strip()]
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    cases: list[dict] = []
    for row in truth:
        root = row["root_cause_service"]
        surface = row["alert_fires_on"]
        flag = row["root_cause_flag"]
        signature = row["signature"]
        # latency-shaped faults: slowdowns, resource exhaustion, GC. Error-shaped: *Failure,
        # *Unreachable, cache. Decide from the flag, not the signature text (which mentions
        # "latency" even when saying "no latency change").
        f = flag.lower()
        is_latency = any(k in f for k in ("slow", "cpu", "gc", "memory", "leak")) and "failure" not in f
        scenario = f"{root}-{flag.lower()}"

        fixture = {
            "search_knowledge": _knowledge_fixture(p, root, flag, signature),
            "query_prometheus": _metrics_fixture(p, root, surface, is_latency),
            "find_traces": _trace_fixture(p, root, surface, is_latency),
            "current_flags": _flags_fixture(flag, "on"),
            "flag_changes": {
                "ref": "flagchg:6h",
                "count": 1,
                "changes": [
                    {
                        "ts": "2026-06-01T14:29:00+00:00",
                        "flag": flag,
                        "event": "incident_start",
                        "variant": "on",
                        "note": "",
                    }
                ],
            },
            "recent_deployments": {"ref": "deploy:all:24h", "count": 0, "events": []},
            "incident_history": _incident_history_fixture(root, flag),
        }
        (FIXTURE_DIR / f"{scenario}.json").write_text(json.dumps(fixture, indent=2) + "\n")

        cases.append(
            {
                "id": f"case-{scenario}",
                "fixture": f"data/fixtures/{scenario}.json",
                "anomaly": {
                    "summary": f"{surface} error ratio / latency crossed its alert threshold",
                    "alerting_service": surface,
                    "started_at": "2026-06-01T14:31:00Z",
                    "signal": "latency_p95" if is_latency else "error_ratio",
                },
                "roles": ["admin"],
                "expected_tools": EXPECTED_TOOLS,
                "expected_outcome": "drafted",
                "ground_truth": {
                    "root_cause_service": root,
                    "root_cause_flag": flag,
                    "expected_finding": row["expected_finding"],
                },
            }
        )

    CASES_OUT.write_text("\n".join(json.dumps(c) for c in cases) + "\n")
    print(f"wrote {len(cases)} cases to {CASES_OUT.relative_to(REPO_ROOT)}")
    print(f"wrote {len(cases)} fixtures to {FIXTURE_DIR.relative_to(REPO_ROOT)}/")
    print("run: uv run python scripts/evaluation/02_evaluate_agent.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
