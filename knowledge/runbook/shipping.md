# shipping runbook

Quotes a shipping cost and books the shipment. Called by checkout. Implemented in Rust. Owned by the checkout team (PD-CHECKOUT, #astroshop-checkout).

## When this fires

`shipping` is on the customer-facing critical path. A failure here surfaces on `checkout`, `frontend`, `frontend-proxy` as elevated errors or latency, often before a `shipping` alert fires. If you were paged for one of those and the cause is not obvious there, check `shipping` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.5% | error budget 50% burned in 1h |
| Duration p95 | < 500 ms | > 750 ms for 5m |

## Dashboards and queries

Grafana: the `shipping` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="shipping", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="shipping"}[5m]))
# rpc_server_duration_milliseconds p95
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="shipping"}[5m])))
```

In Jaeger, filter to `service = shipping` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. **Rule out upstream first.** `shipping` calls `quote`. Check each one's error and latency panels for the same window. If an upstream is unhealthy, work that runbook instead — a healthy `shipping` failing every call to a sick dependency is not a `shipping` incident.
2. Pull the RED panels for `shipping`: `rpc_server_duration_milliseconds`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
3. Open a trace for a failing `shipping` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
4. Check whether a fault flag is active. `shipping` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `intlShippingSlowdown`

**Effect.** International destinations get a multi-second artificial delay.

**Signature.** shipping duration_p99 up only for international orders; checkout p99 up; error rates flat.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `rpc_server_duration_milliseconds` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `shipping` back to the previous image tag. `shipping` is stateless and its callers (`checkout`, `frontend`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `shipping` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the checkout team at **PD-CHECKOUT** and post in #astroshop-checkout with the trace id and the alert link.

## After the incident

- Confirm `rpc_server_duration_milliseconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2691** (2026-08-12) — shipping elevated latency. Cause: `intlShippingSlowdown` fault flag active.
