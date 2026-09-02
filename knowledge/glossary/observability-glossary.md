# Observability and platform glossary

Terms of art used across the runbooks, post-mortems and ADRs. One entry per term. If a document uses a term without defining it, it should be defined here.

## RED metrics

Rate, Errors, Duration — the three signals every request-serving service is measured on. Rate is requests per second, Errors is the fraction that failed, Duration is how long they took (usually as a percentile). If you can see all three for a service, you can tell whether it is healthy without knowing what it does.

*See also: golden signals, p95.*

## Golden signals

Latency, traffic, errors, saturation — Google's SRE framing of what to monitor. RED is the same idea minus saturation; this platform tracks saturation separately per resource (CPU, memory) where it matters.

*See also: RED metrics, saturation.*

## SLO

Service Level Objective — a target for a reliability metric over a window, e.g. "99.9% of checkout requests succeed over 28 days". It is the line that, when crossed, means the service is not meeting its promise. Alerts are tuned to fire before the SLO is missed, not after.

*See also: SLI, error budget.*

## SLI

Service Level Indicator — the actual measured quantity an SLO sets a target for. "Fraction of successful checkout requests" is an SLI; "99.9%" is the SLO on it.

*See also: SLO.*

## Error budget

The amount of unreliability an SLO permits. A 99.9% availability SLO over 28 days allows about 40 minutes of downtime — that 40 minutes is the budget. Burn it slowly and you are fine; burn half of it in one hour and that is a paging condition, because at that rate the month's budget is gone by tomorrow.

*See also: SLO, burn rate.*

## Burn rate

How fast the error budget is being consumed, relative to the rate that would exactly exhaust it over the SLO window. A burn rate of 1 means you will use exactly the budget; a burn rate of 14.4 over one hour means 2% of a 30-day budget gone in an hour. Multi-window burn-rate alerts page on a fast burn and ticket on a slow one.

*See also: error budget.*

## Span

One timed operation within a trace — a single service handling one request, or one outbound call it makes. A span has a start time, a duration, a status (ok or error), and a parent span. The tree of spans for one request is the trace.

*See also: trace, parent span.*

## Trace

The full tree of spans for a single request as it moves through the system. Opening a trace for a failing request shows which span carries the error status, or which span dominates the wall-clock time — that span points at the service to investigate.

*See also: span.*

## Trace context propagation

Passing the trace id and parent span id along every outbound call (as HTTP or gRPC headers) so the receiving service's spans attach to the same trace. If a service does not propagate context, its work shows up as a disconnected trace and the chain breaks.

*See also: span, trace.*

## p95 / p99

The 95th and 99th percentile of a distribution, almost always request duration. p95 = 800ms means 95% of requests finished within 800ms. Percentiles are used instead of the mean because latency distributions have long tails — the mean hides the slow requests that actually hurt, the high percentiles show them.

*See also: histogram, RED metrics.*

## Histogram

A metric type that buckets observations by value — e.g. counts of requests finishing in 0-10ms, 10-25ms, 25-50ms and so on. Percentiles like p95 are computed from the bucket counts (histogram_quantile in PromQL). The bucket boundaries are fixed when the metric is defined, so a poorly chosen set of boundaries limits how precisely a percentile can be estimated.

*See also: p95 / p99, counter.*

## Counter

A metric that only goes up (until the process restarts), like total requests served. You never graph a counter directly — you graph its rate() over a window, which gives requests per second. Forgetting the rate() is the most common PromQL mistake.

*See also: gauge, rate().*

## Gauge

A metric that goes up and down — memory in use, queue depth, active connections. Graphed as-is, no rate().

*See also: counter.*

## Cardinality

The number of distinct time series a metric produces, which is the product of all its label values. A request-duration metric labelled by route, status and customer id has cardinality (routes x statuses x customers) — the customer-id label can make that millions, which overwhelms the metrics store. Keep labels low-cardinality; put the high-cardinality dimension in traces instead.

*See also: label, histogram.*

## Consumer lag

For a Kafka consumer, the number of messages produced to a topic that it has not yet processed. Zero means caught up; a growing lag means the consumer is falling behind the producer. On this platform, accounting and fraud-detection consume the orders topic, and their lag is the freshness SLI for ledger and fraud data.

*See also: Kafka, freshness.*

## Freshness

How far behind real time an asynchronous data pipeline is. Measured as consumer lag in time (seconds) rather than in messages. A freshness SLO of 60s means ledger entries are expected to be written within a minute of the order.

*See also: consumer lag.*

## Cascading failure

A failure that spreads because the response to it adds load — e.g. a slow dependency causes callers to pile up retries, which pushes the dependency further over, which slows it more. The recommendation cache failure on this platform is a mild version: cache misses fan every request out to product-catalog, and a sustained fault would push product-catalog over too.

*See also: blast radius, retry storm.*

## Blast radius

The set of services affected when a given service fails — everything that transitively depends on it. The dependency table in the architecture reference lists the blast radius for every service. It answers "if X breaks, who gets paged?".

*See also: cascading failure, tier.*

## Readiness probe

A check the orchestrator runs to decide whether a service instance should receive traffic. A failing readiness probe takes the instance out of the load-balancer rotation while leaving the process running — so request rate to it drops to zero with no error spike, which looks different from a crash.

*See also: liveness probe, healthcheck.*

## Liveness probe / healthcheck

A check that decides whether a container should be restarted. If it fails, the orchestrator kills and recreates the container. cart has no container healthcheck, so a wedged cart process is never auto-restarted — only a clean exit triggers recreation.

*See also: readiness probe.*

## Feature flag

A named switch, evaluated at runtime, that changes behaviour without a deploy. On this platform flagd serves flags from demo.flagd.json, and the fault-injection scenarios are flags whose variants are failure modes.

*See also: flagd.*

## OTLP

OpenTelemetry Protocol — the wire format every service on this platform uses to send traces, metrics and logs to the collector. One protocol for all three signals.

*See also: span, otel-collector.*
