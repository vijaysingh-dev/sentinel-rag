# recommendation runbook

Returns "you may also like" products. Calls product-catalog. Has an in-process cache. Implemented in Python. Owned by the catalog team (PD-CATALOG, #astroshop-catalog).

## When this fires

`recommendation` is on the customer-facing critical path. A failure here surfaces on `frontend`, `frontend-proxy` as elevated errors or latency, often before a `recommendation` alert fires. If you were paged for one of those and the cause is not obvious there, check `recommendation` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99% | error budget 50% burned in 1h |
| Duration p95 | < 300 ms | > 450 ms for 5m |

## Dashboards and queries

Grafana: the `recommendation` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="recommendation", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="recommendation"}[5m]))
# rpc_server_duration_milliseconds p95
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="recommendation"}[5m])))
# app_recommendations_cache_hit_ratio
app_recommendations_cache_hit_ratio{service_name="recommendation"}
```

In Jaeger, filter to `service = recommendation` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. **Rule out upstream first.** `recommendation` calls `product-catalog`. Check each one's error and latency panels for the same window. If an upstream is unhealthy, work that runbook instead — a healthy `recommendation` failing every call to a sick dependency is not a `recommendation` incident.
2. Pull the RED panels for `recommendation`: `rpc_server_duration_milliseconds`, `app_recommendations_cache_hit_ratio`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
3. Open a trace for a failing `recommendation` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
4. Check whether a fault flag is active. `recommendation` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `recommendationCacheFailure`

**Effect.** The cache never hits, so every request fans out to product-catalog; memory also grows unbounded.

**Signature.** recommendation cache_hit_ratio -> 0; product-catalog request_rate up sharply; recommendation RSS climbing.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `rpc_server_duration_milliseconds` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `recommendation` back to the previous image tag. `recommendation` is stateless and its callers (`frontend`) retry, so a rollback is safe mid-traffic.
3. **If memory is the symptom**, a restart buys time but is not a fix — capture a heap profile first if the incident is not customer-impacting, then restart.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the catalog team at **PD-CATALOG** and post in #astroshop-catalog with the trace id and the alert link.

## After the incident

- Confirm `rpc_server_duration_milliseconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2656** (2026-06-19) — recommendation cache disabled, upstream fan-out spike. Cause: `recommendationCacheFailure` fault flag active.
- **INC-2714** (2026-08-26) — recommendation + product-catalog — two faults overlapping. Cause: compound — see INC-2714.
