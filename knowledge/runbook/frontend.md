# frontend runbook

Serves the web storefront and the JSON API the browser calls. Fans out to every product path. Implemented in TypeScript / Next.js. Owned by the storefront team (PD-STOREFRONT, #astroshop-storefront).

## When this fires

`frontend` is on the customer-facing critical path. A failure here surfaces on `frontend-proxy` as elevated errors or latency, often before a `frontend` alert fires. If you were paged for one of those and the cause is not obvious there, check `frontend` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.9% | error budget 50% burned in 1h |
| Duration p95 | < 800 ms | > 1200 ms for 5m |
| Duration p99 | < 1500 ms | > 2250 ms for 5m |

## Dashboards and queries

Grafana: the `frontend` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="frontend", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="frontend"}[5m]))
# http_server_request_duration_seconds p95
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="frontend"}[5m])))
# http_server_active_requests
http_server_active_requests{service_name="frontend"}
```

In Jaeger, filter to `service = frontend` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. **Rule out upstream first.** `frontend` calls `ad`, `cart`, `checkout`, `currency`, `product-catalog`, `quote`, `recommendation`, `shipping`, `image-provider`. Check each one's error and latency panels for the same window. If an upstream is unhealthy, work that runbook instead — a healthy `frontend` failing every call to a sick dependency is not a `frontend` incident.
2. Pull the RED panels for `frontend`: `http_server_request_duration_seconds`, `http_server_active_requests`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
3. Open a trace for a failing `frontend` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
4. Check whether a fault flag is active. `frontend` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `imageSlowLoad`

**Effect.** Product images take seconds to load; page render blocks on them.

**Signature.** frontend duration_p95 up, downstream services healthy, image-provider latency up.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `http_server_request_duration_seconds` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `frontend` back to the previous image tag. `frontend` is stateless and its callers (`frontend-proxy`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `frontend` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the storefront team at **PD-STOREFRONT** and post in #astroshop-storefront with the trace id and the alert link.

## After the incident

- Confirm `http_server_request_duration_seconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2670** (2026-06-24) — frontend elevated latency. Cause: `imageSlowLoad` fault flag active.
