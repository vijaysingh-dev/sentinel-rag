# accounting runbook

Consumes the orders topic from Kafka and writes ledger entries. Fully asynchronous. Implemented in C# / .NET. Owned by the data team (PD-DATA, #astroshop-data).

## When this fires

`accounting` is tier 2. A failure here degrades no other service directly, but does not by itself take the storefront down.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99% | error budget 50% burned in 1h |
| Consumer lag | < 60 s | > 180 s for 10m |

## Dashboards and queries

Grafana: the `accounting` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="accounting", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="accounting"}[5m]))
# kafka_consumer_lag
kafka_consumer_lag{service_name="accounting"}
# messages_processed_total rate
sum(rate(messages_processed_total{service_name="accounting"}[5m]))
```

In Jaeger, filter to `service = accounting` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. **Rule out upstream first.** `accounting` calls `kafka`. Check each one's error and latency panels for the same window. If an upstream is unhealthy, work that runbook instead — a healthy `accounting` failing every call to a sick dependency is not a `accounting` incident.
2. Pull the RED panels for `accounting`: `kafka_consumer_lag`, `messages_processed_total`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
3. Open a trace for a failing `accounting` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `accounting` back to the previous image tag. `accounting` is stateless, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `accounting` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the data team at **PD-DATA** and post in #astroshop-data with the trace id and the alert link.

## After the incident

- Confirm `kafka_consumer_lag` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.
