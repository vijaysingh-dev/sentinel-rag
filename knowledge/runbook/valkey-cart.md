# valkey-cart runbook

In-memory store behind cart. Redis wire protocol. Implemented in C / Valkey. Owned by the platform-eng team (PD-PLATFORM, #astroshop-platform).

## When this fires

`valkey-cart` is on the customer-facing critical path. A failure here surfaces on `cart`, `checkout`, `frontend`, `frontend-proxy` as elevated errors or latency, often before a `valkey-cart` alert fires. If you were paged for one of those and the cause is not obvious there, check `valkey-cart` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.95% | error budget 50% burned in 1h |
| Duration p95 | < 5 ms | > 7 ms for 5m |

## Dashboards and queries

Grafana: the `valkey-cart` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# valkey-cart emits no spans, so there is no span-metrics error ratio; use the signals below
# redis_memory_used_bytes
redis_memory_used_bytes
# redis_commands_processed_total rate
sum(rate(redis_commands_processed_total[5m]))
```

In Jaeger, filter to `service = valkey-cart` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `valkey-cart`: `redis_memory_used_bytes`, `redis_commands_processed_total`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `valkey-cart` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `valkey-cart` back to the previous image tag. `valkey-cart` is stateless and its callers (`cart`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `valkey-cart` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the platform-eng team at **PD-PLATFORM** and post in #astroshop-platform with the trace id and the alert link.

## After the incident

- Confirm `redis_memory_used_bytes` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.
