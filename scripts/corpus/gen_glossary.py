"""Generate the observability and platform glossary.

    uv run python scripts/corpus/gen_glossary.py

Two outputs from one term list:

* ``knowledge/glossary/observability-glossary.md`` — one term per section. The glossary chunker
  treats each entry as atomic (one term = one chunk), so a lookup for "what is p95" returns just
  that definition, not the six entries around it.
* ``knowledge/reference/metrics-glossary.xlsx`` — the same terms plus a couple of spreadsheet-only
  columns (category, related metric). XLSX is the format where the local parser beats the cloud
  parsers outright — the structure is already in the file — so this is a useful bake-off row.

Terms are the vocabulary the rest of the corpus uses without defining: RED, SLO, error budget,
span, p95, cardinality, consumer lag. If a runbook or a post-mortem uses a term of art, it should
be here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _model import KNOWLEDGE, load, rel, write

sys.path.insert(0, str(Path(__file__).parent))

# (term, category, definition, related)
TERMS: list[tuple[str, str, str, str]] = [
    ("RED metrics", "method",
     "Rate, Errors, Duration — the three signals every request-serving service is measured on. "
     "Rate is requests per second, Errors is the fraction that failed, Duration is how long they "
     "took (usually as a percentile). If you can see all three for a service, you can tell "
     "whether it is healthy without knowing what it does.", "golden signals, p95"),
    ("Golden signals", "method",
     "Latency, traffic, errors, saturation — Google's SRE framing of what to monitor. RED is the "
     "same idea minus saturation; this platform tracks saturation separately per resource "
     "(CPU, memory) where it matters.", "RED metrics, saturation"),
    ("SLO", "reliability",
     "Service Level Objective — a target for a reliability metric over a window, e.g. \"99.9% of "
     "checkout requests succeed over 28 days\". It is the line that, when crossed, means the "
     "service is not meeting its promise. Alerts are tuned to fire before the SLO is missed, not "
     "after.", "SLI, error budget"),
    ("SLI", "reliability",
     "Service Level Indicator — the actual measured quantity an SLO sets a target for. \"Fraction "
     "of successful checkout requests\" is an SLI; \"99.9%\" is the SLO on it.", "SLO"),
    ("Error budget", "reliability",
     "The amount of unreliability an SLO permits. A 99.9% availability SLO over 28 days allows "
     "about 40 minutes of downtime — that 40 minutes is the budget. Burn it slowly and you are "
     "fine; burn half of it in one hour and that is a paging condition, because at that rate the "
     "month's budget is gone by tomorrow.", "SLO, burn rate"),
    ("Burn rate", "reliability",
     "How fast the error budget is being consumed, relative to the rate that would exactly "
     "exhaust it over the SLO window. A burn rate of 1 means you will use exactly the budget; a "
     "burn rate of 14.4 over one hour means 2% of a 30-day budget gone in an hour. Multi-window "
     "burn-rate alerts page on a fast burn and ticket on a slow one.", "error budget"),
    ("Span", "tracing",
     "One timed operation within a trace — a single service handling one request, or one "
     "outbound call it makes. A span has a start time, a duration, a status (ok or error), and a "
     "parent span. The tree of spans for one request is the trace.", "trace, parent span"),
    ("Trace", "tracing",
     "The full tree of spans for a single request as it moves through the system. Opening a "
     "trace for a failing request shows which span carries the error status, or which span "
     "dominates the wall-clock time — that span points at the service to investigate.", "span"),
    ("Trace context propagation", "tracing",
     "Passing the trace id and parent span id along every outbound call (as HTTP or gRPC "
     "headers) so the receiving service's spans attach to the same trace. If a service does not "
     "propagate context, its work shows up as a disconnected trace and the chain breaks.", "span, trace"),
    ("p95 / p99", "method",
     "The 95th and 99th percentile of a distribution, almost always request duration. p95 = 800ms "
     "means 95% of requests finished within 800ms. Percentiles are used instead of the mean "
     "because latency distributions have long tails — the mean hides the slow requests that "
     "actually hurt, the high percentiles show them.", "histogram, RED metrics"),
    ("Histogram", "metrics",
     "A metric type that buckets observations by value — e.g. counts of requests finishing in "
     "0-10ms, 10-25ms, 25-50ms and so on. Percentiles like p95 are computed from the bucket "
     "counts (histogram_quantile in PromQL). The bucket boundaries are fixed when the metric is "
     "defined, so a poorly chosen set of boundaries limits how precisely a percentile can be "
     "estimated.", "p95 / p99, counter"),
    ("Counter", "metrics",
     "A metric that only goes up (until the process restarts), like total requests served. You "
     "never graph a counter directly — you graph its rate() over a window, which gives requests "
     "per second. Forgetting the rate() is the most common PromQL mistake.", "gauge, rate()"),
    ("Gauge", "metrics",
     "A metric that goes up and down — memory in use, queue depth, active connections. Graphed "
     "as-is, no rate().", "counter"),
    ("Cardinality", "metrics",
     "The number of distinct time series a metric produces, which is the product of all its "
     "label values. A request-duration metric labelled by route, status and customer id has "
     "cardinality (routes x statuses x customers) — the customer-id label can make that "
     "millions, which overwhelms the metrics store. Keep labels low-cardinality; put the "
     "high-cardinality dimension in traces instead.", "label, histogram"),
    ("Consumer lag", "streaming",
     "For a Kafka consumer, the number of messages produced to a topic that it has not yet "
     "processed. Zero means caught up; a growing lag means the consumer is falling behind the "
     "producer. On this platform, accounting and fraud-detection consume the orders topic, and "
     "their lag is the freshness SLI for ledger and fraud data.", "Kafka, freshness"),
    ("Freshness", "streaming",
     "How far behind real time an asynchronous data pipeline is. Measured as consumer lag in "
     "time (seconds) rather than in messages. A freshness SLO of 60s means ledger entries are "
     "expected to be written within a minute of the order.", "consumer lag"),
    ("Cascading failure", "reliability",
     "A failure that spreads because the response to it adds load — e.g. a slow dependency causes "
     "callers to pile up retries, which pushes the dependency further over, which slows it more. "
     "The recommendation cache failure on this platform is a mild version: cache misses fan every "
     "request out to product-catalog, and a sustained fault would push product-catalog over too.",
     "blast radius, retry storm"),
    ("Blast radius", "reliability",
     "The set of services affected when a given service fails — everything that transitively "
     "depends on it. The dependency table in the architecture reference lists the blast radius "
     "for every service. It answers \"if X breaks, who gets paged?\".", "cascading failure, tier"),
    ("Readiness probe", "orchestration",
     "A check the orchestrator runs to decide whether a service instance should receive traffic. "
     "A failing readiness probe takes the instance out of the load-balancer rotation while "
     "leaving the process running — so request rate to it drops to zero with no error spike, "
     "which looks different from a crash.", "liveness probe, healthcheck"),
    ("Liveness probe / healthcheck", "orchestration",
     "A check that decides whether a container should be restarted. If it fails, the orchestrator "
     "kills and recreates the container. Plain Docker Compose does not do this: a failing "
     "healthcheck only marks the container unhealthy, so on this platform a wedged cart is "
     "flagged by its grpc_health_probe but never restarted automatically.", "readiness probe"),
    ("Feature flag", "config",
     "A named switch, evaluated at runtime, that changes behaviour without a deploy. On this "
     "platform flagd serves flags from demo.flagd.json, and the fault-injection scenarios are "
     "flags whose variants are failure modes.", "flagd"),
    ("OTLP", "telemetry",
     "OpenTelemetry Protocol — the wire format every service on this platform uses to send "
     "traces, metrics and logs to the collector. One protocol for all three signals.", "span, otel-collector"),
]


def _md() -> str:
    L = ["# Observability and platform glossary", ""]
    L.append(
        "Terms of art used across the runbooks, post-mortems and ADRs. One entry per term. "
        "If a document uses a term without defining it, it should be defined here."
    )
    L.append("")
    for term, _cat, definition, related in TERMS:
        L.append(f"## {term}")
        L.append("")
        L.append(definition)
        if related:
            L.append("")
            L.append(f"*See also: {related}.*")
        L.append("")
    return "\n".join(L)


def _xlsx() -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Glossary"
    headers = ["Term", "Category", "Definition", "See also"]
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="2B3A55")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
    for term, cat, definition, related in TERMS:
        ws.append([term, cat, definition, related])
    widths = [22, 14, 90, 26]
    for i, wdt in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = wdt
    for row in ws.iter_rows(min_row=2):
        row[2].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"

    out = KNOWLEDGE / "reference" / "metrics-glossary.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return rel(out)


def main() -> int:
    load()
    md = rel(write("glossary", "observability-glossary", _md()))
    xlsx = _xlsx()
    print("wrote:")
    print(f"  {md}  ({len(TERMS)} terms)")
    print(f"  {xlsx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
