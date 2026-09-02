# ad runbook

Serves a contextual ad on the product page. Leaf service under frontend. Implemented in Java. Owned by the catalog team (PD-CATALOG, #astroshop-catalog).

## When this fires

`ad` is on the customer-facing critical path. A failure here surfaces on `frontend`, `frontend-proxy` as elevated errors or latency, often before a `ad` alert fires. If you were paged for one of those and the cause is not obvious there, check `ad` early.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99% | error budget 50% burned in 1h |
| Duration p95 | < 250 ms | > 375 ms for 5m |

## Dashboards and queries

Grafana: the `ad` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="ad", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="ad"}[5m]))
# rpc_server_duration_milliseconds p95
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="ad"}[5m])))
# jvm_gc_duration_seconds p95
histogram_quantile(0.95, sum by (le) (rate(jvm_gc_duration_seconds_bucket{service_name="ad"}[5m])))
# process_cpu_utilization
process_cpu_utilization{service_name="ad"}
```

In Jaeger, filter to `service = ad` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `ad`: `rpc_server_duration_milliseconds`, `jvm_gc_duration_seconds`, `process_cpu_utilization`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `ad` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
3. Check whether a fault flag is active. `ad` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `adFailure`

**Effect.** GetAds throws for every request.

**Signature.** ad error_rate 100%; frontend degrades gracefully (ad slot empty), no cascade past frontend.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `rpc_server_duration_milliseconds` returns to baseline before closing.

### `adHighCpu`

**Effect.** A background thread pins CPU.

**Signature.** ad process_cpu_utilization saturated; ad duration_p95 up; error rate flat; may not alert if only latency-gated.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `rpc_server_duration_milliseconds` returns to baseline before closing.

### `adManualGc`

**Effect.** Forced full GC pauses on a loop.

**Signature.** ad jvm_gc_duration_seconds spikes periodically; sawtooth latency; correlates with GC, not load.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `rpc_server_duration_milliseconds` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `ad` back to the previous image tag. `ad` is stateless and its callers (`frontend`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `ad` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the catalog team at **PD-CATALOG** and post in #astroshop-catalog with the trace id and the alert link.

## After the incident

- Confirm `rpc_server_duration_milliseconds` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2614** (2026-07-01) — ad CPU/GC saturation, elevated latency. Cause: `adHighCpu` fault flag active.
- **INC-2635** (2026-06-08) — ad returning errors. Cause: `adFailure` fault flag active.
- **INC-2649** (2026-06-18) — ad CPU/GC saturation, elevated latency. Cause: `adManualGc` fault flag active.
