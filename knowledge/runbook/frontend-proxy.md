# frontend-proxy runbook

Edge proxy. All external traffic and the flagd-ui and telemetry-docs routes pass through it. Implemented in C++ / Envoy. Owned by the storefront team (PD-STOREFRONT, #astroshop-storefront).

## When this fires

`frontend-proxy` is tier 0. A failure here degrades no other service directly, but does not by itself take the storefront down.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.95% | error budget 50% burned in 1h |
| Duration p95 | < 900 ms | > 1350 ms for 5m |

## Dashboards and queries

Grafana: the `frontend-proxy` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="frontend-proxy", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="frontend-proxy"}[5m]))
# envoy_http_downstream_rq_total rate
sum(rate(envoy_http_downstream_rq_total{service_name="frontend-proxy"}[5m]))
# envoy_http_downstream_rq_time
envoy_http_downstream_rq_time{service_name="frontend-proxy"}
```

In Jaeger, filter to `service = frontend-proxy` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. **Rule out upstream first.** `frontend-proxy` calls `frontend`. Check each one's error and latency panels for the same window. If an upstream is unhealthy, work that runbook instead — a healthy `frontend-proxy` failing every call to a sick dependency is not a `frontend-proxy` incident.
2. Pull the RED panels for `frontend-proxy`: `envoy_http_downstream_rq_total`, `envoy_http_downstream_rq_time`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
3. Open a trace for a failing `frontend-proxy` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `frontend-proxy` back to the previous image tag. `frontend-proxy` is stateless, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `frontend-proxy` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the storefront team at **PD-STOREFRONT** and post in #astroshop-storefront with the trace id and the alert link.

## After the incident

- Confirm `envoy_http_downstream_rq_total` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.
