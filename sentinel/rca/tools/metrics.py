"""Read-only metrics and trace access for the metrics and tracing specialists.

A query the model composes and runs against a production observability stack is the highest-risk
tool in the system, so `query_prometheus` is defended in four independent layers rather than by
prompt instruction alone. This mirrors the SQL warehouse tool it replaces (see ADR-027):

1. **Metric-name allowlist.** PromQL has no parser in the standard library, so instead of parsing
   an AST the guard extracts every identifier that looks like a metric name and requires each to
   be one the platform actually exposes (`ALLOWED_METRIC_PREFIXES`). An expression naming an
   unknown metric is rejected unrun. A regex looking for bad *keywords* would be the weak version
   of this; an allowlist of good *names* is the strong one.
2. **Forbidden functions.** A short list of PromQL functions that are expensive or exfiltration-
   shaped (`label_replace` rewriting series, subqueries nesting range selectors) is rejected.
3. **Forced, bounded time range.** A range query with no window, or a window wider than
   `settings.promql_max_range_hours`, is rejected. An unbounded range query is a self-inflicted
   outage on a busy Prometheus.
4. **Read-only HTTP endpoint.** The one that actually guarantees it. Prometheus' query API is
   read-only by construction; the first three layers are defence in depth, this is the guarantee.
   Prompt instructions are not a security control.

`find_traces` is lower risk (Jaeger's query API is read-only and paginated) but still returns a
`ref` per trace so a claim about a span can be traced back to the exact trace id.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from langchain_core.tools import tool

from sentinel.core.config import settings
from sentinel.core.observability import TOOL_CALLS, log
from sentinel.rca.runtime import current_tenant, fixture_lookup

# Metric families the otel-demo exposes. A metric name in a query must start with one of these.
# Kept as prefixes, not exact names, because histograms fan out to _bucket / _count / _sum and
# the OTel SDKs add a unit suffix. Extend this when services.yaml gains a metric, not ad hoc.
ALLOWED_METRIC_PREFIXES: tuple[str, ...] = (
    "http_server_",
    "http_client_",
    "rpc_server_",
    "rpc_client_",
    "app_",
    "process_",
    "jvm_",
    "runtime_",
    "system_",
    "kafka_",
    "redis_",
    "pg_",
    "nginx_",
    "envoy_",
    "otelcol_",
    "flagd_",
    "up",
    "scrape_",
    "target_",
)

FORBIDDEN_FUNCS = re.compile(
    r"\b(label_replace|label_join|absent_over_time|holt_winters)\s*\(", re.IGNORECASE
)

# an identifier that is used like a metric selector: a bare name followed by { or [ or whitespace
# then an operator, at the start or after a ( , or binary operator.
_METRIC_TOKEN = re.compile(r"(?<![\w:])([a-z_][a-z0-9_]*)(?=\s*[\{\[\(]|\s|$)", re.IGNORECASE)

# PromQL keywords and function names that are not metric names — excluded from the allowlist
# check. Grouped by kind for review; the flat frozenset is what the validator uses.
_NOT_METRICS: frozenset[str] = frozenset(
    # aggregation operators and modifiers
    ("sum", "avg", "min", "max", "count", "count_values", "stddev", "stdvar", "topk", "bottomk",
     "quantile", "group", "by", "without", "on", "ignoring", "group_left", "group_right", "offset",
     "bool", "and", "or", "unless")
    # rate / counter functions
    + ("rate", "irate", "increase", "delta", "idelta", "deriv", "predict_linear", "resets", "changes")
    # histogram functions
    + ("histogram_quantile", "histogram_count", "histogram_sum", "histogram_fraction")
    # math and misc
    + ("time", "timestamp", "vector", "scalar", "abs", "ceil", "floor", "round", "exp", "ln",
       "log2", "log10", "sqrt", "clamp", "clamp_max", "clamp_min", "sgn", "sort", "sort_desc")
    # *_over_time range aggregations
    + ("avg_over_time", "min_over_time", "max_over_time", "sum_over_time", "count_over_time",
       "quantile_over_time", "stddev_over_time", "stdvar_over_time", "last_over_time",
       "present_over_time")
    # date functions
    + ("day_of_month", "day_of_week", "days_in_month", "hour", "minute", "month", "year")
)


class UnsafeQuery(ValueError):
    """Raised when a composed PromQL expression fails validation. Returned to the model, never run."""


def validate_promql(expr: str, *, range_hours: float | None) -> str:
    """Rejects anything outside the metric allowlist, a forbidden function, or an unbounded range."""
    stripped = expr.strip()
    if not stripped:
        raise UnsafeQuery("empty expression")
    if "\n" in stripped or ";" in stripped:
        raise UnsafeQuery("a single expression only — no newlines or statement separators")
    if FORBIDDEN_FUNCS.search(stripped):
        raise UnsafeQuery("expression uses a function that is not permitted from this tool")

    names = {m.group(1) for m in _METRIC_TOKEN.finditer(stripped)}
    metric_names = {n for n in names if n not in _NOT_METRICS and not n.isupper()}
    if not metric_names:
        raise UnsafeQuery("no metric name found — the expression must select at least one metric")
    for name in sorted(metric_names):
        if not name.startswith(ALLOWED_METRIC_PREFIXES):
            raise UnsafeQuery(
                f"metric '{name}' is not on the platform allowlist; "
                f"use a metric named in the service's runbook"
            )

    if range_hours is not None and range_hours > settings.promql_max_range_hours:
        raise UnsafeQuery(
            f"range of {range_hours}h exceeds the {settings.promql_max_range_hours}h limit — "
            f"narrow the window"
        )
    return stripped


def _ref(kind: str, key: str) -> str:
    """Content-derived citation handle, so a claim can be traced to the exact query or trace."""
    return f"{kind}:" + hashlib.sha256(key.encode()).hexdigest()[:12]


@tool
async def query_prometheus(expr: str, purpose: str, lookback_minutes: int = 30) -> dict:
    """Run a single read-only PromQL expression against the platform's Prometheus.

    Use this to see which service's RED signals moved and when: error ratio, request rate,
    duration percentiles, resource saturation. Compare a service against its own recent baseline.

    The expression must select at least one metric named in the target service's runbook
    (`http_server_*`, `rpc_server_*`, `app_*`, `process_*`, `kafka_*`, ...). It runs as a range
    query over the last `lookback_minutes`; keep the window under six hours.

    Args:
        expr: One PromQL expression. No newlines, no `label_replace`, no unbounded ranges.
        purpose: One line on what this establishes. Recorded in the audit log.
        lookback_minutes: How far back the range query reaches. Default 30.
    """
    recorded = fixture_lookup("query_prometheus")
    if recorded is not None:
        TOOL_CALLS.labels("query_prometheus", "fixture").inc()
        return recorded

    try:
        safe = validate_promql(expr, range_hours=lookback_minutes / 60)
    except UnsafeQuery as exc:
        TOOL_CALLS.labels("query_prometheus", "rejected").inc()
        log.warning("promql_rejected", reason=str(exc))
        return {
            "error": "query rejected by validation",
            "reason": str(exc),
            "instruction": "Rewrite the expression to select a metric from the runbook, over a "
            "window under six hours, and try again.",
        }

    try:
        from sentinel.rca.clients import metrics_client

        end = datetime.utcnow()
        start = end - timedelta(minutes=lookback_minutes)
        result = await metrics_client().query_range(safe, start=start, end=end, step_s=30)
    except Exception as exc:
        TOOL_CALLS.labels("query_prometheus", "error").inc()
        log.warning("prometheus_unavailable", error=type(exc).__name__)
        return {
            "error": "metrics backend unavailable",
            "instruction": "Continue with other tools and state that live metrics could not be read.",
        }

    TOOL_CALLS.labels("query_prometheus", "ok").inc()
    return {
        "ref": _ref("promql", safe),
        "expr": safe,
        "window_minutes": lookback_minutes,
        "result": result,
    }


@tool
async def find_traces(
    service: str, only_errors: bool = True, since_minutes: int = 30, limit: int = 5
) -> dict:
    """Fetch recent traces for a service from Jaeger, newest first.

    Use this after `query_prometheus` has told you *which* service moved: open a failing or a slow
    trace and read which span carries the error status, or which span dominates wall-clock time.
    That span points at the subsystem — often a downstream call, not the service itself.

    Args:
        service: The service name, e.g. 'checkout'.
        only_errors: Restrict to traces with an error span. Default true.
        since_minutes: How far back to search. Default 30.
        limit: Maximum traces to return. Default 5.
    """
    recorded = fixture_lookup("find_traces")
    if recorded is not None:
        TOOL_CALLS.labels("find_traces", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import tracing_client

        since = datetime.utcnow() - timedelta(minutes=since_minutes)
        traces = await tracing_client().find_traces(
            service=service, since=since, only_errors=only_errors, limit=min(limit, 20)
        )
    except Exception as exc:
        TOOL_CALLS.labels("find_traces", "error").inc()
        log.warning("jaeger_unavailable", error=type(exc).__name__)
        return {
            "error": "tracing backend unavailable",
            "instruction": "Continue with other tools and state that traces could not be read.",
        }

    TOOL_CALLS.labels("find_traces", "ok").inc()
    return {
        "ref": _ref("trace", f"{service}:{only_errors}:{since_minutes}"),
        "service": service,
        "count": len(traces),
        "traces": traces[:limit],
        "instruction": "Cite trace:<id> for any claim about a specific span.",
        "tenant": current_tenant(),
    }
