"""Generate agent trajectory-evaluation cases and their recorded tool fixtures.

    uv run python scripts/corpus/gen_agent_cases.py

Reads ``scripts/corpus/_incident_truth.jsonl`` (written by gen_incidents.py) rather than iterating
services.yaml's faults directly, because that file already carries all three incident shapes and
is correct by construction for each. For every truth row this writes:

* one row in ``data/agent_cases.jsonl`` — the anomaly the agent is handed, the tools a competent
  investigation should call (`expected_tools`), and the expected final outcome;
* one file ``data/fixtures/<scenario>.json`` — recorded output for every tool the agent might
  call during that investigation, keyed by tool name, in the shape `sentinel/rca/runtime.fixtures`
  binds. This is what lets `make eval-agent` replay a full investigation with no Prometheus,
  Jaeger or flagd running.

The recorded output is synthesised from the truth row to be *consistent with the ground truth*.
Three shapes, one per `incident_type`:

* **single** — the metrics fixture shows the alerting service's error ratio raised, the trace
  fixture shows the error span in the root-cause service, and the flag fixture shows exactly the
  flag the incident truth says was set.
* **compound** (`row["causes"]` has two entries) — the metrics and trace fixtures show *both*
  services degraded, and `current_flags` reports both flags active simultaneously. A correct
  agent has to notice a second live signal instead of stopping at the first culprit it finds —
  this is the fixture-eval analogue of the "does not survive contact with the signatures"
  disambiguation the compound post-mortems narrate in prose.
* **organic** (`row["root_cause_flag"] is None`) — `current_flags` reports nothing active at all.
  The correct behaviour is to keep investigating past an empty flag check, not to conclude "no
  flag is set, therefore nothing is wrong" — the mistake the post-mortem's own "contributing
  factors" section calls out.

An agent that reads these and still names the wrong service (or the wrong *number* of services,
for a compound case) is wrong for a reason worth surfacing, not because the fixture was ambiguous.

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


def _series(service: str, degraded: bool, is_latency: bool) -> dict:
    if is_latency:
        base, now = (0.12, 0.9) if degraded else (0.11, 0.12)
    else:
        base, now = (0.002, 0.34) if degraded else (0.001, 0.0015)
    return {
        "labels": {"service_name": service},
        "first": base,
        "last": now,
        "min": base,
        "max": now,
        "direction": "rising" if degraded else "flat",
        "from": "2026-06-01T14:00:00+00:00",
        "to": "2026-06-01T14:30:00+00:00",
        "points": 60,
    }


def _metrics_fixture(p: Platform, root: str, surface: str, is_latency: bool) -> dict:
    """A Prometheus range summary consistent with the fault: the surface service degraded,
    the root-cause service degraded harder, healthy services flat."""
    healthy = [d for d in p.services[surface].depends_on if d != root][:2]
    return {
        "ref": "promql:fixture0001",
        "expr": f"error ratio / duration for {surface} and its dependencies",
        "window_minutes": 30,
        "result": {
            "series": [_series(surface, True, is_latency), _series(root, True, is_latency)]
            + [_series(h, False, is_latency) for h in healthy],
            "series_count": 2 + len(healthy),
        },
    }


def _metrics_fixture_compound(root1: str, root2: str, is_latency1: bool, is_latency2: bool) -> dict:
    """Both causes show degraded, independently of each other — no third service is dragged down
    just because two are broken at once, since the two faults are unrelated or narrowly scoped."""
    return {
        "ref": "promql:fixture0001",
        "expr": f"error ratio / duration for {root1} and {root2}",
        "window_minutes": 30,
        "result": {
            "series": [_series(root1, True, is_latency1), _series(root2, True, is_latency2)],
            "series_count": 2,
        },
    }


def _metrics_fixture_organic(surface: str, root: str) -> dict:
    """A latency-shaped degradation with no matching flag — the metrics alone don't say why."""
    return {
        "ref": "promql:fixture0001",
        "expr": f"error ratio / duration for {surface} and its dependencies",
        "window_minutes": 30,
        "result": {
            "series": [_series(surface, True, True), _series(root, True, True)],
            "series_count": 2,
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


def _trace_fixture_compound(root1: str, root2: str, is_latency1: bool, is_latency2: bool) -> dict:
    """One trace per cause, each pointing at its own service — nothing here suggests one caused
    the other, which is the point: the traces are the evidence that splits the incident in two."""

    def trace(trace_id: str, svc: str, is_latency: bool) -> dict:
        return {
            "trace_id": trace_id,
            "found": True,
            "duration_ms": 1840.0 if is_latency else 210.0,
            "span_count": 8,
            "services": [svc],
            "slowest_span": {
                "service": svc,
                "operation": "grpc.execute",
                "duration_ms": 1600.0 if is_latency else 40.0,
                "share_of_trace": 0.87 if is_latency else 0.19,
            },
            "error_spans": (
                []
                if is_latency
                else [{"service": svc, "operation": "grpc.execute", "message": "fault injected via flag"}]
            ),
        }

    return {
        "ref": "trace:fixture0001",
        "service": root1,
        "count": 2,
        "traces": [trace("abc123def456", root1, is_latency1), trace("def456abc123", root2, is_latency2)],
        "instruction": "Cite trace:<id> for any claim about a specific span.",
    }


def _trace_fixture_organic(surface: str, root: str, signature: str) -> dict:
    """No error span, no fault-flag message — a saturation/latency trace with a prose cause,
    which the agent has to read from `signature`-shaped evidence rather than a flag-tagged span."""
    return {
        "ref": "trace:fixture0001",
        "service": surface,
        "count": 1,
        "traces": [
            {
                "trace_id": "abc123def456",
                "found": True,
                "duration_ms": 1600.0,
                "span_count": 10,
                "services": [surface, root],
                "slowest_span": {
                    "service": root,
                    "operation": "grpc.execute",
                    "duration_ms": 1350.0,
                    "share_of_trace": 0.84,
                },
                "error_spans": [],
                "note": signature,
            }
        ],
        "instruction": "Cite trace:<id> for any claim about a specific span.",
    }


def _flag_entry(flag: str, variant: str) -> dict:
    return {"flag": flag, "current_variant": variant, "resting_variant": "off", "active": True, "value": variant}


def _flags_fixture(root_flag: str, variant: str) -> dict:
    return {"ref": "flags:current", "flags": {"flags": [_flag_entry(root_flag, variant)], "any_active": True}}


def _flags_fixture_compound(flag1: str, flag2: str, variant1: str, variant2: str) -> dict:
    return {
        "ref": "flags:current",
        "flags": {"flags": [_flag_entry(flag1, variant1), _flag_entry(flag2, variant2)], "any_active": True},
    }


def _flags_fixture_empty() -> dict:
    """No flag is set — the organic case's whole point. Query for the empty result on purpose."""
    return {"ref": "flags:current", "flags": {"flags": [], "any_active": False}}


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


def _runbook_hit(root: str, flag: str, signature: str, score: float) -> dict:
    return {
        "ref": "doc:runbook-" + root,
        "title": f"{root} runbook > Known failure modes > {flag}",
        "doc_type": "runbook",
        "score": score,
        "text": (
            f"{flag}. Effect: fault injected. Signature: {signature} "
            f"Response: confirm the flag is set by querying flagd, then reset it."
        ),
    }


def _knowledge_fixture_compound(root1: str, root2: str, flag1: str, flag2: str, sig1: str, sig2: str) -> list[dict]:
    """One runbook hit per cause, plus the compound post-mortem itself — the retrieval evidence
    that a competent search turns up both culprits instead of stopping at the first hit."""
    return [
        _runbook_hit(root1, flag1, sig1, 0.81),
        _runbook_hit(root2, flag2, sig2, 0.77),
        {
            "ref": f"doc:incident-{root1}-{root2}",
            "title": f"Past incident: {root1} + {root2} — two faults overlapping",
            "doc_type": "incident",
            "score": 0.79,
            "text": (
                f"Two independent feature flags were active concurrently: {flag1} on {root1} and "
                f"{flag2} on {root2}. They are separate incidents that overlapped in time, not one "
                f"cascading from the other."
            ),
        },
    ]


def _knowledge_fixture_organic(root: str, signature: str, cause_summary: str) -> list[dict]:
    """No flag-tagged runbook entry exists for this service, so the only hit is the post-mortem
    itself — retrieval alone won't hand the agent a flag name to check, by design."""
    return [
        {
            "ref": "doc:incident-" + root,
            "title": f"Past incident: {root} — no fault flag",
            "doc_type": "incident",
            "score": 0.71,
            "text": f"{cause_summary} No fault flag was active. Signature: {signature}",
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


def _incident_history_fixture_compound(root1: str, root2: str, flag1: str, flag2: str) -> dict:
    return {
        "ref": "inclog:10",
        "count": 2,
        "incidents": [
            {
                "ts": "2026-06-01T14:29:00+00:00",
                "scenario": f"{root1}_fault",
                "flag": flag1,
                "event": "incident_start",
                "variant": "on",
                "note": "scheduled fault injection",
            },
            {
                "ts": "2026-06-01T14:33:00+00:00",
                "scenario": f"{root2}_fault",
                "flag": flag2,
                "event": "incident_start",
                "variant": "on",
                "note": "scheduled fault injection, independent of the above",
            },
        ],
    }


def _incident_history_fixture_organic(root: str) -> dict:
    """No scheduled fault-injection entry exists for this service — nothing to find here either."""
    return {"ref": "inclog:10", "count": 0, "incidents": []}


def _is_latency_flag(flag: str) -> bool:
    """latency-shaped faults: slowdowns, resource exhaustion, GC. Error-shaped: *Failure,
    *Unreachable, cache. Decide from the flag, not the signature text (which mentions "latency"
    even when saying "no latency change")."""
    f = flag.lower()
    return any(k in f for k in ("slow", "cpu", "gc", "memory", "leak")) and "failure" not in f


def _flag_change_entry(flag: str, ts: str, note: str = "") -> dict:
    return {"ts": ts, "flag": flag, "event": "incident_start", "variant": "on", "note": note}


def _case_single(p: Platform, row: dict) -> tuple[str, dict, dict]:
    root = row["root_cause_service"]
    surface = row["alert_fires_on"]
    flag = row["root_cause_flag"]
    signature = row["signature"]
    is_latency = _is_latency_flag(flag)
    scenario = f"{root}-{flag.lower()}"
    fixture = {
        "search_knowledge": _knowledge_fixture(p, root, flag, signature),
        "query_prometheus": _metrics_fixture(p, root, surface, is_latency),
        "find_traces": _trace_fixture(p, root, surface, is_latency),
        "current_flags": _flags_fixture(flag, "on"),
        "flag_changes": {
            "ref": "flagchg:6h",
            "count": 1,
            "changes": [_flag_change_entry(flag, "2026-06-01T14:29:00+00:00")],
        },
        "recent_deployments": {"ref": "deploy:all:24h", "count": 0, "events": []},
        "incident_history": _incident_history_fixture(root, flag),
    }
    anomaly = {
        "summary": f"{surface} error ratio / latency crossed its alert threshold",
        "alerting_service": surface,
        "started_at": "2026-06-01T14:31:00Z",
        "signal": "latency_p95" if is_latency else "error_ratio",
    }
    return scenario, fixture, anomaly


def _case_compound(p: Platform, row: dict) -> tuple[str, dict, dict]:
    c1, c2 = row["causes"]
    root1, flag1 = c1["service"], c1["flag"]
    root2, flag2 = c2["service"], c2["flag"]
    surface = row["alert_fires_on"]
    is_lat1, is_lat2 = _is_latency_flag(flag1), _is_latency_flag(flag2)
    full_sig1, full_sig2 = row["signature"].split(" | ", 1)
    scenario = f"{root1}-{root2}-compound"
    fixture = {
        "search_knowledge": _knowledge_fixture_compound(root1, root2, flag1, flag2, full_sig1, full_sig2),
        "query_prometheus": _metrics_fixture_compound(root1, root2, is_lat1, is_lat2),
        "find_traces": _trace_fixture_compound(root1, root2, is_lat1, is_lat2),
        "current_flags": _flags_fixture_compound(flag1, flag2, "on", "on"),
        "flag_changes": {
            "ref": "flagchg:6h",
            "count": 2,
            "changes": [
                _flag_change_entry(flag1, "2026-06-01T14:29:00+00:00"),
                _flag_change_entry(flag2, "2026-06-01T14:33:00+00:00", note="independent of the above"),
            ],
        },
        "recent_deployments": {"ref": "deploy:all:24h", "count": 0, "events": []},
        "incident_history": _incident_history_fixture_compound(root1, root2, flag1, flag2),
    }
    anomaly = {
        "summary": f"{surface} error ratio / latency crossed its alert threshold",
        "alerting_service": surface,
        "started_at": "2026-06-01T14:31:00Z",
        "signal": "latency_p95" if is_lat1 else "error_ratio",
    }
    return scenario, fixture, anomaly


def _case_organic(p: Platform, row: dict) -> tuple[str, dict, dict]:
    root, surface, signature = row["root_cause_service"], row["alert_fires_on"], row["signature"]
    cause_summary = row["causes"][0]["effect"]
    scenario = f"{root}-organic"
    fixture = {
        "search_knowledge": _knowledge_fixture_organic(root, signature, cause_summary),
        "query_prometheus": _metrics_fixture_organic(surface, root),
        "find_traces": _trace_fixture_organic(surface, root, signature),
        "current_flags": _flags_fixture_empty(),
        "flag_changes": {"ref": "flagchg:6h", "count": 0, "changes": []},
        "recent_deployments": {
            "ref": "deploy:all:24h",
            "count": 1,
            "events": [{"ts": "2026-06-01T14:20:00+00:00", "service": root, "kind": "deploy", "note": cause_summary}],
        },
        "incident_history": _incident_history_fixture_organic(root),
    }
    anomaly = {
        "summary": f"{surface} duration_p95 crossed its alert threshold",
        "alerting_service": surface,
        "started_at": "2026-06-01T14:31:00Z",
        "signal": "latency_p95",
    }
    return scenario, fixture, anomaly


_CASE_BUILDERS = {"single": _case_single, "compound": _case_compound, "organic": _case_organic}


def main() -> int:
    if not TRUTH.exists():
        print("scripts/corpus/_incident_truth.jsonl missing — run gen_incidents.py first", file=sys.stderr)
        return 1

    p = load()
    truth = [json.loads(line) for line in TRUTH.read_text().splitlines() if line.strip()]
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    cases: list[dict] = []
    for row in truth:
        incident_type = row.get("incident_type", "single")
        scenario, fixture, anomaly = _CASE_BUILDERS[incident_type](p, row)
        (FIXTURE_DIR / f"{scenario}.json").write_text(json.dumps(fixture, indent=2) + "\n")

        cases.append(
            {
                "id": f"case-{scenario}",
                "fixture": f"data/fixtures/{scenario}.json",
                "anomaly": anomaly,
                "roles": ["admin"],
                "expected_tools": EXPECTED_TOOLS,
                "expected_outcome": "drafted",
                "ground_truth": {
                    "root_cause_service": row["root_cause_service"],
                    "root_cause_flag": row["root_cause_flag"],
                    "causes": row["causes"],
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
