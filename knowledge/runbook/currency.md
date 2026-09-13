# currency runbook

Converts prices between currencies. Called by frontend and checkout. Pure compute, no dependencies. Implemented in C++. Owned by the checkout team (PD-CHECKOUT, #astroshop-checkout).

## When this fires

`currency` is on the customer-facing critical path. A failure here surfaces on `checkout`, `frontend`, `frontend-proxy` as elevated errors or latency, often before a `currency` alert fires. If you were paged for one of those and the cause is not obvious there, check `currency` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.95% | error budget 50% burned in 1h |
| Duration p95 | < 50 ms | > 75 ms for 5m |

## Dashboards and queries

Grafana: the `currency` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="currency", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="currency"}[5m]))
# rpc_server_duration_milliseconds p95
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="currency"}[5m])))
```

In Jaeger, filter to `service = currency` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `currency`: `rpc_server_duration_milliseconds`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `currency` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `currency` back to the previous image tag. `currency` is stateless and its callers (`checkout`, `frontend`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `currency` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the checkout team at **PD-CHECKOUT** and post in #astroshop-checkout with the trace id and the alert link.

## After the incident

- Confirm `rpc_server_duration_milliseconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2767** (2026-09-03) — currency elevated latency after a deploy. Cause: no fault flag — see post-mortem.
