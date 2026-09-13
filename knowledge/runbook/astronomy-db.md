# astronomy-db runbook

Relational store behind product-catalog. Implemented in SQL / PostgreSQL. Owned by the platform-eng team (PD-PLATFORM, #astroshop-platform).

## When this fires

`astronomy-db` is on the customer-facing critical path. A failure here surfaces on `product-catalog`, `checkout`, `frontend`, `frontend-proxy` as elevated errors or latency, often before a `astronomy-db` alert fires. If you were paged for one of those and the cause is not obvious there, check `astronomy-db` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.9% | error budget 50% burned in 1h |
| Duration p95 | < 20 ms | > 30 ms for 5m |

## Dashboards and queries

Grafana: the `astronomy-db` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="astronomy-db", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="astronomy-db"}[5m]))
# pg_stat_database_xact_commit
pg_stat_database_xact_commit{service_name="astronomy-db"}
# pg_stat_activity_count
pg_stat_activity_count{service_name="astronomy-db"}
```

In Jaeger, filter to `service = astronomy-db` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `astronomy-db`: `pg_stat_database_xact_commit`, `pg_stat_activity_count`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `astronomy-db` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `astronomy-db` back to the previous image tag. `astronomy-db` is stateless and its callers (`product-catalog`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `astronomy-db` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the platform-eng team at **PD-PLATFORM** and post in #astroshop-platform with the trace id and the alert link.

## After the incident

- Confirm `pg_stat_database_xact_commit` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2774** (2026-09-07) — astronomy-db connection pool exhaustion. Cause: no fault flag — see post-mortem.
