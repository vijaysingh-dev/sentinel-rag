"""Turn a service's key-metric list into copy-pasteable PromQL.

The runbooks and the metrics catalog both need "the actual query an on-call would paste into
Prometheus", not just a metric name. That mapping — histogram -> p95 via histogram_quantile,
_total -> rate() over 5m, a gauge left alone — is small but easy to get subtly wrong (forgetting
`rate()` on a counter is the single most common PromQL mistake), so it lives in one place.
"""

from __future__ import annotations

RANGE = "5m"


def _is_histogram(metric: str) -> bool:
    return metric.endswith(("_seconds", "_milliseconds", "_bytes")) and "duration" in metric or metric.endswith(
        ("_duration_seconds", "_duration_milliseconds")
    )


def _is_counter(metric: str) -> bool:
    return metric.endswith("_total")


def query_for(metric: str, service: str) -> tuple[str, str]:
    """Return (label, promql) for a single metric in the context of one service."""
    sel = f'{{service_name="{service}"}}'
    if _is_histogram(metric):
        return (
            f"{metric} p95",
            f'histogram_quantile(0.95, sum by (le) (rate({metric}_bucket{sel}[{RANGE}])))',
        )
    if _is_counter(metric):
        if "fail" in metric or "error" in metric:
            return (f"{metric} rate", f"sum(rate({metric}{sel}[{RANGE}]))")
        return (f"{metric} rate", f"sum(rate({metric}{sel}[{RANGE}]))")
    return (metric, f"{metric}{sel}")


def error_ratio(service: str) -> str:
    """A generic RED error-ratio query keyed on the OTel RPC/HTTP status convention."""
    sel = f'service_name="{service}"'
    return (
        f'sum(rate(rpc_server_duration_milliseconds_count{{{sel}, rpc_grpc_status_code!="0"}}[{RANGE}]))\n'
        f'  / sum(rate(rpc_server_duration_milliseconds_count{{{sel}}}[{RANGE}]))'
    )
