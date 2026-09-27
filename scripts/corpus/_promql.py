"""Turn a service's key-metric list into copy-pasteable PromQL.

The runbooks and the metrics catalog both need "the actual query an on-call would paste into
Prometheus", not just a metric name. That mapping — histogram -> p95 via histogram_quantile,
_total -> rate() over 5m, a gauge left alone — is small but easy to get subtly wrong (forgetting
`rate()` on a counter is the single most common PromQL mistake), so it lives in one place.

Two facts about the live otel-demo Prometheus shape everything below, both checked against a
running stack rather than assumed:

* **Not every metric is keyed by `service_name`.** SDK metrics pushed over OTLP carry it (the
  resource attribute is promoted to a label), but the collector's own receivers don't:
  `container_*` (docker_stats) is keyed by `container_name`, Kafka consumer lag by consumer
  `group`, and the redis / postgresql / nginx / httpcheck receivers describe one shared target
  with no service label at all. A `{service_name="email"}` selector on `container_memory_*`
  silently matches nothing — an empty graph, not an error — so the selector is chosen per family.
* **RED comes from span metrics.** The per-language request metrics differ in name and unit
  (Go/.NET emit `rpc_server_call_duration_seconds`, the Java ad service still emits
  `rpc_server_duration_milliseconds`, currency and email emit none), so error ratio and p95 use
  the collector's span-metrics connector, which produces the same two series for every service.
"""

from __future__ import annotations

RANGE = "5m"

# Families whose series are not labelled with service_name, and what to select on instead.
# None means the metric describes one shared target: no selector, the series is the answer.
_SELECTOR_BY_PREFIX: dict[str, str | None] = {
    "container_": "container_name",
    "kafka_consumer_group_": "group",
    "redis_": None,
    "postgresql_": None,
    "nginx_": None,
    "httpcheck_": None,
    "otelcol_": None,
    "feature_flag_": None,
}


def selector(metric: str, service: str) -> str:
    for prefix, label in _SELECTOR_BY_PREFIX.items():
        if metric.startswith(prefix):
            return f'{{{label}="{service}"}}' if label else ""
    return f'{{service_name="{service}"}}'


def _is_histogram(metric: str) -> bool:
    # The *_p50/_p99 names (nodejs_eventloop_delay_p99_seconds, kafka_request_time_99p_*) are
    # pre-computed gauges, not histograms, even though they carry a duration unit — and so is the
    # httpcheck receiver's duration, which is one probe's timing, not a distribution.
    if any(tag in metric for tag in ("_p50_", "_p90_", "_p99_", "_50p_", "_99p_", "_max_", "_mean_", "_min_")):
        return False
    if metric.startswith("httpcheck_"):
        return False
    return metric.endswith(("_seconds", "_milliseconds")) and ("duration" in metric or "latency" in metric)


def _is_counter(metric: str) -> bool:
    return metric.endswith("_total")


def query_for(metric: str, service: str) -> tuple[str, str]:
    """Return (label, promql) for a single metric in the context of one service."""
    sel = selector(metric, service)
    if _is_histogram(metric):
        return (
            f"{metric} p95",
            f"histogram_quantile(0.95, sum by (le) (rate({metric}_bucket{sel}[{RANGE}])))",
        )
    if _is_counter(metric):
        return (f"{metric} rate", f"sum(rate({metric}{sel}[{RANGE}]))")
    return (metric, f"{metric}{sel}")


# Which span kind carries a service's own work. Kafka consumers have no server spans — their unit
# of work is a CONSUMER span per message. The broker, the two data stores and the collector emit
# no spans at all, so RED-from-traces does not apply to them; their runbooks use key metrics only.
_CONSUMERS = {"accounting", "fraud-detection"}
_NO_SPANS = {"kafka", "valkey-cart", "astronomy-db", "otel-collector"}


def span_kind_for(service: str) -> str | None:
    if service in _NO_SPANS:
        return None
    return "SPAN_KIND_CONSUMER" if service in _CONSUMERS else "SPAN_KIND_SERVER"


def _span_sel(service: str, kind: str, extra: str = "") -> str:
    return f'service_name="{service}", span_kind="{kind}"{extra}'


def error_ratio(service: str) -> str | None:
    """Fraction of the service's own spans that ended with an error status; None if it emits none."""
    kind = span_kind_for(service)
    if kind is None:
        return None
    err = _span_sel(service, kind, ', status_code="STATUS_CODE_ERROR"')
    return (
        f"sum(rate(traces_span_metrics_calls_total{{{err}}}[{RANGE}]))\n"
        f"  / sum(rate(traces_span_metrics_calls_total{{{_span_sel(service, kind)}}}[{RANGE}]))"
    )


def latency_p95(service: str) -> str | None:
    """p95 duration of the service's own spans, in milliseconds; None if it emits none."""
    kind = span_kind_for(service)
    if kind is None:
        return None
    return (
        "histogram_quantile(0.95, sum by (le) "
        f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{_span_sel(service, kind)}}}[{RANGE}])))"
    )
