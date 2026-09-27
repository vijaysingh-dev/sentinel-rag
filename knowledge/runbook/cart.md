# cart runbook

Holds the customer's cart. Backed by valkey-cart (Redis-compatible). Health-checked with grpc_health_probe; a failing check marks it unhealthy but never restarts it. Implemented in C# / .NET. Owned by the checkout team (PD-CHECKOUT, #astroshop-checkout).

## When this fires

`cart` is on the customer-facing critical path. A failure here surfaces on `checkout`, `frontend`, `frontend-proxy` as elevated errors or latency, often before a `cart` alert fires. If you were paged for one of those and the cause is not obvious there, check `cart` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.9% | error budget 50% burned in 1h |
| Duration p95 | < 150 ms | > 225 ms for 5m |

## Dashboards and queries

Grafana: the `cart` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio (span metrics)
sum(rate(traces_span_metrics_calls_total{service_name="cart", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
  / sum(rate(traces_span_metrics_calls_total{service_name="cart", span_kind="SPAN_KIND_SERVER"}[5m]))
# p95 duration, ms (span metrics)
histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="cart", span_kind="SPAN_KIND_SERVER"}[5m])))
# http_server_request_duration_seconds p95
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="cart"}[5m])))
# demo_cart_add_item_latency_seconds p95
histogram_quantile(0.95, sum by (le) (rate(demo_cart_add_item_latency_seconds_bucket{service_name="cart"}[5m])))
# dotnet_exceptions_total rate
sum(rate(dotnet_exceptions_total{service_name="cart"}[5m]))
```

In Jaeger, filter to `service = cart` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. **Rule out upstream first.** `cart` calls `valkey-cart`. Check each one's error and latency panels for the same window. If an upstream is unhealthy, work that runbook instead — a healthy `cart` failing every call to a sick dependency is not a `cart` incident.
2. Pull the RED panels for `cart`: `http_server_request_duration_seconds`, `demo_cart_add_item_latency_seconds`, `dotnet_exceptions_total`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
3. Open a trace for a failing `cart` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
4. Check whether a fault flag is active. `cart` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `cartFailure`

**Effect.** AddItem and GetCart fail for a configurable percentage of calls.

**Signature.** cart error_rate up by the configured fraction; checkout error_rate up in step; valkey-cart healthy.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `http_server_request_duration_seconds` returns to baseline before closing.

### `failedReadinessProbe`

**Effect.** Readiness probe fails; orchestrator takes the pod out of rotation while the process keeps running.

**Signature.** cart request_rate drops to zero with no error spike; "no healthy upstream" from the proxy.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `http_server_request_duration_seconds` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `cart` back to the previous image tag. `cart` is stateless and its callers (`checkout`, `frontend`) retry, so a rollback is safe mid-traffic.
3. **If the process is wedged** (latency flat-lined, no logs), restart it by hand with `docker restart cart`. `cart`'s `grpc_health_probe` healthcheck will mark a hung process unhealthy (red in system-monitor), but nothing restarts it: Docker never restarts a container for failing its healthcheck, and the restart policy is `no`.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the checkout team at **PD-CHECKOUT** and post in #astroshop-checkout with the trace id and the alert link.

## After the incident

- Confirm `http_server_request_duration_seconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2663** (2026-08-13) — cart returning errors. Cause: `cartFailure` fault flag active.
- **INC-2684** (2026-06-13) — cart taken out of rotation by a failing readiness probe. Cause: `failedReadinessProbe` fault flag active.
