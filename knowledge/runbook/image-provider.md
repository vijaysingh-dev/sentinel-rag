# image-provider runbook

Static image host for product photos. Served through frontend-proxy. Implemented in Nginx. Owned by the storefront team (PD-STOREFRONT, #astroshop-storefront).

## When this fires

`image-provider` is on the customer-facing critical path. A failure here surfaces on `frontend`, `frontend-proxy` as elevated errors or latency, often before a `image-provider` alert fires. If you were paged for one of those and the cause is not obvious there, check `image-provider` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.9% | error budget 50% burned in 1h |
| Duration p95 | < 100 ms | > 150 ms for 5m |

## Dashboards and queries

Grafana: the `image-provider` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio (span metrics)
sum(rate(traces_span_metrics_calls_total{service_name="image-provider", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
  / sum(rate(traces_span_metrics_calls_total{service_name="image-provider", span_kind="SPAN_KIND_SERVER"}[5m]))
# p95 duration, ms (span metrics)
histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="image-provider", span_kind="SPAN_KIND_SERVER"}[5m])))
# nginx_requests_total rate
sum(rate(nginx_requests_total[5m]))
# nginx_connections_current
nginx_connections_current
```

In Jaeger, filter to `service = image-provider` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `image-provider`: `nginx_requests_total`, `nginx_connections_current`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `image-provider` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `image-provider` back to the previous image tag. `image-provider` is stateless and its callers (`frontend`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `image-provider` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the storefront team at **PD-STOREFRONT** and post in #astroshop-storefront with the trace id and the alert link.

## After the incident

- Confirm `nginx_requests_total` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.
