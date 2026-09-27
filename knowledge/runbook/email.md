# email runbook

Sends the order-confirmation email. Called by checkout after the order is committed, best-effort. Implemented in Ruby. Owned by the checkout team (PD-CHECKOUT, #astroshop-checkout).

## When this fires

`email` is on the customer-facing critical path. A failure here surfaces on `checkout`, `frontend`, `frontend-proxy` as elevated errors or latency, often before a `email` alert fires. If you were paged for one of those and the cause is not obvious there, check `email` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99% | error budget 50% burned in 1h |
| Duration p95 | < 1000 ms | > 1500 ms for 5m |

## Dashboards and queries

Grafana: the `email` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio (span metrics)
sum(rate(traces_span_metrics_calls_total{service_name="email", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
  / sum(rate(traces_span_metrics_calls_total{service_name="email", span_kind="SPAN_KIND_SERVER"}[5m]))
# p95 duration, ms (span metrics)
histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="email", span_kind="SPAN_KIND_SERVER"}[5m])))
# demo_notification_confirmations_total rate
sum(rate(demo_notification_confirmations_total{service_name="email"}[5m]))
# container_memory_usage_total_bytes
container_memory_usage_total_bytes{container_name="email"}
```

In Jaeger, filter to `service = email` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `email`: `demo_notification_confirmations_total`, `container_memory_usage_total_bytes`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `email` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
3. Check whether a fault flag is active. `email` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `emailMemoryLeak`

**Effect.** RSS climbs steadily until the runtime OOMs and restarts.

**Signature.** email container_memory_usage_total_bytes ramps linearly toward its 100M limit; OOM restarts; checkout unaffected (call is best-effort).

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `demo_notification_confirmations_total` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `email` back to the previous image tag. `email` is stateless and its callers (`checkout`) retry, so a rollback is safe mid-traffic.
3. **If memory is the symptom**, a restart buys time but is not a fix — capture a heap profile first if the incident is not customer-impacting, then restart.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the checkout team at **PD-CHECKOUT** and post in #astroshop-checkout with the trace id and the alert link.

## After the incident

- Confirm `demo_notification_confirmations_total` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2677** (2026-08-13) — email memory growth and restarts. Cause: `emailMemoryLeak` fault flag active.
- **INC-2707** (2026-08-23) — kafka + email — two faults overlapping. Cause: compound — see INC-2707.
