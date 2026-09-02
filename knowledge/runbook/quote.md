# quote runbook

Computes a shipping quote (weight/size -> price). Called by shipping. Implemented in PHP. Owned by the catalog team (PD-CATALOG, #astroshop-catalog).

## When this fires

`quote` is on the customer-facing critical path. A failure here surfaces on `frontend`, `checkout`, `frontend-proxy` as elevated errors or latency, often before a `quote` alert fires. If you were paged for one of those and the cause is not obvious there, check `quote` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.5% | error budget 50% burned in 1h |
| Duration p95 | < 200 ms | > 300 ms for 5m |

## Dashboards and queries

Grafana: the `quote` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="quote", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="quote"}[5m]))
# rpc_server_duration_milliseconds p95
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="quote"}[5m])))
```

In Jaeger, filter to `service = quote` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `quote`: `rpc_server_duration_milliseconds`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `quote` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `quote` back to the previous image tag. `quote` is stateless and its callers (`frontend`, `shipping`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `quote` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the catalog team at **PD-CATALOG** and post in #astroshop-catalog with the trace id and the alert link.

## After the incident

- Confirm `rpc_server_duration_milliseconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.
