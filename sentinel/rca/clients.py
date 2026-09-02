"""Clients for the platform services the RCA agent reads from.

These call the observability stack that ships with the OpenTelemetry Demo (`app/otel-demo/`):
Prometheus for metrics, Jaeger for traces, flagd for the current feature-flag configuration, and
`app/incident-generator` for the fault-scenario log. The endpoints come from `settings`
(`prometheus_url`, `jaeger_url`, `flagd_config_path`, `incident_log_url`), so a different
deployment is a config change, not a code change.

The shape every method keeps, and why it matters:

* async, returning plain data — dicts and lists the model can read, never SDK objects;
* the *expected* empty case (no traces, no flag changes) returns an empty result, not an error;
* an infrastructure failure (connection refused, 5xx, timeout) raises, so `guarded()` trips the
  breaker and the tool layer degrades with an instruction rather than a stack trace;
* nothing here writes. The only write in the system is `itsm_client().attach_rca`, behind the
  human interrupt.

The tool layer's `fixture_lookup` short-circuits all of this during evaluation, so `make eval`
never needs a running Prometheus.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from sentinel.core.config import settings
from sentinel.core.resilience import guarded

# One shared async client per process. httpx pools connections, and creating a client per call is
# the classic way to exhaust file descriptors under load.
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


@lru_cache
def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class MetricsClient:
    """Read-only Prometheus access: range and instant queries.

    Everything here is live state, so `core/cache.LIVE_TOOLS` excludes it from every cache layer.
    A cached error rate is worse than no error rate.

    The response is trimmed before it reaches the model: Prometheus returns every sample in the
    window, which for a 30-minute range at a 30-second step is 60 points per series across
    however many series the query fans out to. The model needs the shape — first value, last
    value, min, max, and whether it is trending — not 60 raw floats. `_summarise_matrix` does that.
    """

    @guarded("prometheus", timeout_s=10.0)
    async def query_range(self, expr: str, start: datetime, end: datetime, step_s: int) -> dict:
        resp = await _http().get(
            f"{settings.prometheus_url}/api/v1/query_range",
            params={
                "query": expr,
                "start": _utc(start).timestamp(),
                "end": _utc(end).timestamp(),
                "step": f"{step_s}s",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prometheus error: {body.get('error', 'unknown')}")
        return _summarise_matrix(body["data"]["result"])

    @guarded("prometheus", timeout_s=10.0)
    async def query_instant(self, expr: str, at: datetime | None = None) -> dict:
        params: dict[str, Any] = {"query": expr}
        if at is not None:
            params["time"] = _utc(at).timestamp()
        resp = await _http().get(f"{settings.prometheus_url}/api/v1/query", params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prometheus error: {body.get('error', 'unknown')}")
        return {
            "series": [
                {"labels": _trim_labels(r["metric"]), "value": float(r["value"][1])}
                for r in body["data"]["result"][:20]
            ]
        }


class TracingClient:
    """Read-only Jaeger access.

    Jaeger's query API returns a trace as a flat list of spans with parent references; the model
    wants "which span errored" and "which span took the time", so `_summarise_trace` reduces each
    trace to its error spans, its slowest span, and the service call graph.
    """

    @guarded("jaeger", timeout_s=15.0)
    async def get_trace(self, trace_id: str) -> dict:
        resp = await _http().get(f"{settings.jaeger_url}/api/traces/{trace_id}")
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return {"trace_id": trace_id, "found": False}
        return _summarise_trace(data[0])

    @guarded("jaeger", timeout_s=15.0)
    async def find_traces(
        self, service: str, since: datetime, only_errors: bool, limit: int
    ) -> list[dict]:
        params: dict[str, Any] = {
            "service": service,
            "start": int(_utc(since).timestamp() * 1_000_000),  # Jaeger wants microseconds
            "end": int(datetime.now(UTC).timestamp() * 1_000_000),
            "limit": max(limit * 3, 20),  # over-fetch so the error filter still leaves `limit`
        }
        if only_errors:
            params["tags"] = json.dumps({"error": "true"})
        resp = await _http().get(f"{settings.jaeger_url}/api/traces", params=params)
        resp.raise_for_status()
        traces = [_summarise_trace(t) for t in resp.json().get("data", [])]
        traces.sort(key=lambda t: t.get("duration_ms", 0), reverse=True)
        return traces[:limit]


class FlagClient:
    """Current feature-flag configuration and its recent change history.

    On the otel-demo faults are variants in `demo.flagd.json`, which flagd watches and reloads
    live. This reads that file directly — the same file flagd-ui and the incident-generator write
    — because it is the source of truth and needs no running flagd. `settings.flagd_config_path`
    must point at it; when unset the tools say so rather than guessing.

    flagd has no built-in change audit log, so `flag_changes` derives changes by diffing against
    a sidecar snapshot the incident-generator maintains, falling back to "not available" when
    that snapshot is absent. The real platform would read its config-management history here.
    """

    def _config_path(self) -> Path:
        if not settings.flagd_config_path:
            raise RuntimeError("flagd_config_path is not set")
        return Path(settings.flagd_config_path)

    @guarded("flagd", timeout_s=8.0)
    async def current_flags(self, only_non_default: bool) -> dict:
        raw = json.loads(self._config_path().read_text())
        flags = raw.get("flags", {})
        out = []
        for name, spec in flags.items():
            default = spec.get("defaultVariant")
            variants = spec.get("variants", {})
            resting = _resting_variant(name, variants)
            is_active = resting is not None and default != resting
            if only_non_default and not is_active:
                continue
            out.append(
                {
                    "flag": name,
                    "current_variant": default,
                    "resting_variant": resting,
                    "active": is_active,
                    "value": variants.get(default),
                }
            )
        return {"flags": out, "any_active": any(f["active"] for f in out)}

    @guarded("flagd", timeout_s=8.0)
    async def flag_changes(self, since: datetime) -> list[dict]:
        # flagd has no change-audit API. On the otel-demo the incident-generator is the only thing
        # that flips flags, and it serves a timestamped log of every flip at /incidents — so that
        # is the flag change history. The real platform reads its config-management audit here.
        resp = await _http().get(f"{settings.incident_log_url}/incidents", params={"limit": 100})
        resp.raise_for_status()
        cutoff = _utc(since).timestamp()
        return [
            {
                "ts": datetime.fromtimestamp(rec["ts"], UTC).isoformat(),
                "flag": rec.get("flag"),
                "event": rec.get("event"),
                "variant": rec.get("fault_variant"),
                "note": rec.get("note", ""),
            }
            for rec in resp.json().get("incidents", [])
            if rec.get("ts", 0) >= cutoff
        ]


class DeployClient:
    """Deployment history for a service.

    The otel-demo has no CD pipeline — everything is `docker compose up`. There is no deploy
    history to read, so this returns an empty list and the tool reports "no deployment history
    available in this environment". The real platform wires this to its CD system's API.
    """

    @guarded("deploy_service", timeout_s=15.0)
    async def list_deploys(self, service: str | None, since: datetime) -> list[dict]:
        return []


class IncidentLogClient:
    """The incident-generator's event log — the scheduled fault scenarios it has run.

    Exact ground truth: the generator records the flag it set and the wall-clock time. In
    production this is the incident-management system's timeline.
    """

    @guarded("incident_log", timeout_s=10.0)
    async def recent_incidents(self, limit: int) -> list[dict]:
        resp = await _http().get(
            f"{settings.incident_log_url}/incidents", params={"limit": limit}
        )
        resp.raise_for_status()
        incidents = resp.json().get("incidents", [])
        return [
            {
                "ts": datetime.fromtimestamp(i["ts"], UTC).isoformat() if "ts" in i else None,
                "scenario": i.get("scenario"),
                "flag": i.get("flag"),
                "event": i.get("event"),
                "variant": i.get("fault_variant"),
                "note": i.get("note", ""),
            }
            for i in incidents[-limit:]
        ]


class ITSMClient:
    """The single write path, reached only after human approval."""

    @guarded("itsm", timeout_s=20.0)
    async def attach_rca(self, anomaly_id: str, tenant_id: str, draft: dict, approved_by: str) -> str:
        raise NotImplementedError("wire to the incident management system")


# --------------------------------------------------------------------------- response shaping


def _trim_labels(labels: dict[str, str]) -> dict[str, str]:
    """Keep the labels a human reads; drop the OTel scope noise that bloats every series."""
    keep = ("service_name", "job", "instance", "http_route", "rpc_method", "rpc_service",
            "rpc_grpc_status_code", "http_response_status_code", "le", "operation", "span_name")
    return {k: v for k, v in labels.items() if k in keep}


def _summarise_matrix(result: list[dict]) -> dict:
    """Reduce a Prometheus range result to per-series shape, not raw samples.

    The model is deciding "did this move and when", so first/last/min/max and a direction beat
    sixty floats it then has to eyeball. The raw first and last timestamps are kept so it can
    state *when* precisely.
    """
    series = []
    for r in result[:20]:
        values = [(float(ts), float(v)) for ts, v in r.get("values", [])]
        if not values:
            continue
        ys = [v for _, v in values]
        first_y, last_y = ys[0], ys[-1]
        direction = "flat"
        if last_y > first_y * 1.15:
            direction = "rising"
        elif last_y < first_y * 0.85:
            direction = "falling"
        series.append(
            {
                "labels": _trim_labels(r["metric"]),
                "first": round(first_y, 5),
                "last": round(last_y, 5),
                "min": round(min(ys), 5),
                "max": round(max(ys), 5),
                "direction": direction,
                "from": datetime.fromtimestamp(values[0][0], UTC).isoformat(),
                "to": datetime.fromtimestamp(values[-1][0], UTC).isoformat(),
                "points": len(values),
            }
        )
    return {"series": series, "series_count": len(result)}


def _summarise_trace(trace: dict) -> dict:
    """Reduce a Jaeger trace to what the tracing specialist actually reports on."""
    spans = trace.get("spans", [])
    procs = trace.get("processes", {})

    def svc_of(span: dict) -> str:
        return procs.get(span.get("processID", ""), {}).get("serviceName", "?")

    def is_error(span: dict) -> bool:
        return any(
            t.get("key") == "error" and t.get("value") in (True, "true")
            for t in span.get("tags", [])
        ) or any(
            t.get("key") == "otel.status_code" and t.get("value") == "ERROR"
            for t in span.get("tags", [])
        )

    if not spans:
        return {"trace_id": trace.get("traceID"), "found": False}

    total_us = max((s["startTime"] + s["duration"]) for s in spans) - min(s["startTime"] for s in spans)

    # The root span wraps the whole request, so it is always the longest by wall-clock and tells
    # you nothing. "Slowest span" means the slowest one that has a parent — the actual hotspot.
    has_parent = [s for s in spans if any(r.get("refType") == "CHILD_OF" for r in s.get("references", []))]
    slowest = max(has_parent or spans, key=lambda s: s["duration"])
    error_spans = [s for s in spans if is_error(s)]

    return {
        "trace_id": trace.get("traceID"),
        "found": True,
        "duration_ms": round(total_us / 1000, 1),
        "span_count": len(spans),
        "services": sorted({svc_of(s) for s in spans}),
        "slowest_span": {
            "service": svc_of(slowest),
            "operation": slowest.get("operationName"),
            "duration_ms": round(slowest["duration"] / 1000, 1),
            "share_of_trace": round(slowest["duration"] / total_us, 2) if total_us else None,
        },
        "error_spans": [
            {
                "service": svc_of(s),
                "operation": s.get("operationName"),
                "message": _span_error_message(s),
            }
            for s in error_spans[:5]
        ],
    }


def _span_error_message(span: dict) -> str | None:
    keys = ("error.message", "exception.message", "otel.status_description")
    return next((t.get("value") for t in span.get("tags", []) if t.get("key") in keys), None)


def _resting_variant(flag: str, variants: dict[str, Any]) -> str | None:
    """The variant a flag sits at when no fault is injected.

    flagd config does not record this, so infer it: the demo convention is that the resting
    variant is 'off', or 'false', or the numerically smallest, or the first declared.
    """
    for candidate in ("off", "false"):
        if candidate in variants:
            return candidate
    numeric = {k: v for k, v in variants.items() if isinstance(v, (int, float))}
    if numeric:
        return min(numeric, key=numeric.get)
    return next(iter(variants), None)


# --------------------------------------------------------------------------- singletons


@lru_cache
def metrics_client() -> MetricsClient:
    return MetricsClient()


@lru_cache
def tracing_client() -> TracingClient:
    return TracingClient()


@lru_cache
def flag_client() -> FlagClient:
    return FlagClient()


@lru_cache
def deploy_client() -> DeployClient:
    return DeployClient()


@lru_cache
def incident_log_client() -> IncidentLogClient:
    return IncidentLogClient()


@lru_cache
def itsm_client() -> ITSMClient:
    return ITSMClient()
