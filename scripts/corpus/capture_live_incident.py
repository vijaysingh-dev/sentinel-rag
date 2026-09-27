"""Capture a live incident as an agent-eval case: real tool output, real ground truth.

    uv run python scripts/corpus/capture_live_incident.py payment_degraded
    uv run python scripts/corpus/capture_live_incident.py --all          # every scenario, in turn
    uv run python scripts/corpus/capture_live_incident.py cart_outage --no-rca

Needs the full stack up (`make otel-demo-up`) and, unless `--no-rca`, the chat API
(`make chat-api`). For each scenario it:

1. triggers it on the incident-generator and waits `--settle` seconds for the fault to show in
   Prometheus (metrics arrive by OTLP push on a 60s export interval, and `rate(...[5m])` needs a
   couple of those before it moves);
2. calls every RCA agent tool once, live, with the arguments a sound investigation would use, and
   writes their real output to ``data/fixtures/live/<scenario>-<stamp>.json`` — the same
   one-response-per-tool shape `sentinel.rca.runtime.fixtures` binds;
3. asks Sentinel's chat API to investigate, the way a person would ("why is checkout failing"),
   and records what the live agent concluded;
4. appends a case to ``data/agent_cases_live.jsonl`` with the ground truth the incident-generator
   itself asserts (the flag it flipped and the service that flag breaks).

Why this exists next to gen_agent_cases.py: the generated fixtures are consistent with the fault
model by construction, which is exactly their weakness — a live stack produces baseline noise,
slow-moving rates, error spans in services that aren't the cause, and metrics that simply don't
move for some faults. An agent that only ever sees clean synthetic fixtures has never been tested
on any of that. `gen_agent_cases.py` folds these live cases into ``data/agent_cases.jsonl`` on
every `make corpus`, so they are replayed by `make eval-agent` alongside the synthetic ones.

The captured `evidence` block (the peak per-service value the capture query saw) is there so a
reviewer can check the fault was actually visible before trusting the case: a case whose fault
never showed in the data tests nothing, and should be deleted rather than kept.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from _model import load  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "data" / "fixtures" / "live"
CASES_OUT = REPO_ROOT / "data" / "agent_cases_live.jsonl"
INCIDENTS = "http://localhost:8092"
CHAT = "http://localhost:8093"
EXPECTED_TOOLS = ["search_knowledge", "query_prometheus", "find_traces", "current_flags"]

# Where each scenario's symptom surfaces and which signal an on-call would call it, matching the
# `alert_fires_on` in _incident_truth.jsonl so live and synthetic cases ask the same question.
SURFACE: dict[str, tuple[str, str]] = {
    "payment_degraded": ("checkout", "error_ratio"),
    "payment_outage": ("checkout", "error_ratio"),
    "cart_outage": ("checkout", "error_ratio"),
    "ad_service_down": ("frontend", "error_ratio"),
    "ad_high_cpu": ("frontend", "latency_p95"),
    "recommendation_cache_failure": ("frontend", "latency_p95"),
    "product_catalog_failure": ("checkout", "error_ratio"),
    "shipping_slowdown": ("checkout", "latency_p95"),
    "email_memory_leak": ("checkout", "latency_p95"),
}

_SERVER = 'span_kind="SPAN_KIND_SERVER"'


def _capture_query(scenario: str, signal: str) -> str:
    """One query that shows every service side by side, so the recording contains the culprit and
    the bystanders — the fixture returns this for whatever PromQL the agent writes."""
    if scenario == "email_memory_leak":
        return "sum by (container_name) (container_memory_usage_total_bytes)"
    if scenario == "ad_high_cpu":
        return "sum by (service_name) (jvm_cpu_recent_utilization_ratio)"
    if signal == "latency_p95":
        return (
            "histogram_quantile(0.95, sum by (service_name, le) "
            f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{_SERVER}}}[2m])))"
        )
    errors = f'{_SERVER}, status_code="STATUS_CODE_ERROR"'
    return (
        f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{errors}}}[2m]))"
        f" / sum by (service_name) (rate(traces_span_metrics_calls_total{{{_SERVER}}}[2m]))"
    )


async def _record_tools(scenario: str, flag: str, service: str, signal: str) -> dict:
    from sentinel.rca.runtime import request_context
    from sentinel.rca.tools.change import current_flags, flag_changes, incident_history, recent_deployments
    from sentinel.rca.tools.metrics import find_traces, query_prometheus
    from sentinel.rca.tools.retrieval import search_knowledge

    symptom = "errors" if signal == "error_ratio" else "high latency"
    with request_context("default", ["admin"]):
        return {
            "search_knowledge": await search_knowledge.ainvoke({"query": f"{service} {symptom} runbook"}),
            "query_prometheus": await query_prometheus.ainvoke(
                {"expr": _capture_query(scenario, signal), "purpose": f"{signal} by service", "lookback_minutes": 15}
            ),
            "find_traces": await find_traces.ainvoke(
                {"service": service, "only_errors": signal == "error_ratio", "since_minutes": 10, "limit": 5}
            ),
            "current_flags": await current_flags.ainvoke({"only_changed": True}),
            "flag_changes": await flag_changes.ainvoke({"hours": 1}),
            "recent_deployments": await recent_deployments.ainvoke({"hours": 24}),
            "incident_history": await incident_history.ainvoke({"limit": 5}),
        }


def _peak_by_series(metrics: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in (metrics.get("result") or {}).get("series", []):
        labels = s.get("labels", {})
        key = labels.get("service_name") or labels.get("container_name") or json.dumps(labels)
        if isinstance(s.get("max"), (int, float)):
            out[key] = round(s["max"], 4)
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:8])


def _live_rca(service: str, signal: str) -> dict:
    """Ask the chat API the way a person would, and keep what the agent concluded."""
    message = f"why is {service} {'failing' if signal == 'error_ratio' else 'slow'}"
    out: dict = {"message": message, "route": None, "draft": None, "error": None, "steps": []}
    t0 = time.time()
    with httpx.stream("POST", f"{CHAT}/chat/message", json={"role": "admin", "message": message}, timeout=900) as r:
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                e = json.loads(line[5:])
            except ValueError:
                continue
            if e.get("type") == "route":
                out["route"] = e["route"]
            elif e.get("type") == "step":
                out["steps"].append(e.get("node"))
            elif e.get("type") == "awaiting_approval":
                out["draft"] = e.get("draft")
            elif e.get("type") == "error":
                out["error"] = e.get("message")
    out["seconds"] = round(time.time() - t0, 1)
    return out


def _wait_idle(client: httpx.Client, cooldown_s: int) -> None:
    """Wait for no active scenario *and* `cooldown_s` since the last one ended.

    Without the cooldown, back-to-back captures bleed into each other: the capture query's
    lookback still holds the previous fault's error rate, so the product_catalog_failure capture
    recorded ad at 35% errors — the tail of ad_service_down, minutes earlier.
    """
    while True:
        status = client.get(f"{INCIDENTS}/status").json()
        if status.get("active"):
            time.sleep(5)
            continue
        ends = [r["end_ts"] for r in status.get("recent", []) if r.get("end_ts")]
        wait = (max(ends) + cooldown_s - time.time()) if ends else 0
        if wait <= 0:
            return
        print(f"cooling down {int(wait)}s after the previous scenario")
        time.sleep(wait)


async def capture(name: str, settle_s: int, run_rca: bool, cooldown_s: int) -> dict:
    p = load()
    scenarios = {s["name"]: s for s in httpx.get(f"{INCIDENTS}/scenarios").json()["scenarios"]}
    spec = scenarios[name]
    flag = spec["flag"]
    root = p.faults[flag].service
    service, signal = SURFACE[name]

    with httpx.Client(timeout=30) as client:
        _wait_idle(client, cooldown_s)
        client.post(f"{INCIDENTS}/trigger/{name}").raise_for_status()
        started = datetime.now(UTC)
        print(f"[{name}] triggered {flag}={spec['fault_variant']} for {spec['duration_s']}s; settling {settle_s}s")
        time.sleep(settle_s)

        # Tools first, while the fault is certainly still on (the shortest scenario lasts 120s);
        # the live agent run takes minutes and queries a 30-minute window, so it still sees it.
        print(f"[{name}] recording tool output")
        fixture = await _record_tools(name, flag, service, signal)
        still_active = bool(client.get(f"{INCIDENTS}/status").json().get("active"))

        rca = None
        if run_rca:
            print(f"[{name}] live RCA via chat API")
            rca = await asyncio.to_thread(_live_rca, service, signal)

    stamp = started.strftime("%Y%m%d-%H%M")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fixture_path = FIXTURE_DIR / f"{name}-{stamp}.json"
    fixture_path.write_text(json.dumps(fixture, indent=2, default=str) + "\n")

    draft = (rca or {}).get("draft") or {}
    symptom = "error ratio" if signal == "error_ratio" else "latency"
    case = {
        "id": f"live-{name}-{stamp}",
        "fixture": str(fixture_path.relative_to(REPO_ROOT)),
        "anomaly": {
            "summary": f"{service} {symptom} crossed its alert threshold",
            "alerting_service": service,
            "started_at": started.isoformat(timespec="seconds"),
            "signal": signal,
        },
        "roles": ["admin"],
        "expected_tools": EXPECTED_TOOLS,
        "expected_outcome": "drafted",
        "ground_truth": {
            "root_cause_service": root,
            "root_cause_flag": flag,
            "causes": [{"service": root, "flag": flag, "effect": p.faults[flag].effect}],
            "incident_type": "live",
        },
        "meta": {
            "source": "live",
            "scenario": name,
            "fault_variant": spec["fault_variant"],
            "fault_still_active_at_capture": still_active,
            "evidence": {
                "capture_query": fixture["query_prometheus"].get("expr"),
                "peak_by_series": _peak_by_series(fixture["query_prometheus"]),
                "error_traces_on_surface": fixture["find_traces"].get("count"),
                "flags_active": [f["flag"] for f in fixture["current_flags"].get("flags", {}).get("flags", [])],
            },
            "live_rca": {
                "route": (rca or {}).get("route"),
                "seconds": (rca or {}).get("seconds"),
                "error": (rca or {}).get("error"),
                "root_cause": draft.get("root_cause"),
                "confidence": draft.get("confidence"),
                "named_root_service": bool(draft) and root in json.dumps(draft).lower(),
            }
            if run_rca
            else None,
        },
    }
    with CASES_OUT.open("a") as f:
        f.write(json.dumps(case) + "\n")
    print(f"[{name}] wrote {fixture_path.relative_to(REPO_ROOT)} and a case in {CASES_OUT.relative_to(REPO_ROOT)}")
    print(f"[{name}] peak: {case['meta']['evidence']['peak_by_series']}")
    if run_rca:
        print(f"[{name}] live RCA: {case['meta']['live_rca']}")
    return case


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?", choices=sorted(SURFACE))
    ap.add_argument("--all", action="store_true", help="capture every scenario in turn")
    ap.add_argument("--settle", type=int, default=100, help="seconds between trigger and capture")
    ap.add_argument("--no-rca", action="store_true", help="record tool output only, skip the live agent run")
    ap.add_argument("--cooldown", type=int, default=300, help="seconds after the previous scenario ends")
    args = ap.parse_args()
    if not args.scenario and not args.all:
        ap.error("name a scenario or pass --all")

    from sentinel.core.observability import configure_logging

    configure_logging()
    names = sorted(SURFACE) if args.all else [args.scenario]
    for name in names:
        asyncio.run(capture(name, args.settle, not args.no_rca, args.cooldown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
