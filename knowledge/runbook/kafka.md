# kafka runbook

The order event bus. checkout produces to it; accounting and fraud-detection consume. Implemented in Java. Owned by the data team (PD-DATA, #astroshop-data).

## When this fires

`kafka` is tier 1. A failure here degrades `accounting`, `fraud-detection`, but does not by itself take the storefront down.

## Thresholds

| Signal | Objective | Page at |
| --- | --- | --- |
| Availability (28d) | 99.9% | error budget 50% burned in 1h |
| Produce p95 | < 100 ms | > 300 ms |

## Dashboards and queries

Grafana: the `kafka` row on the Astroshop service dashboard. The queries behind that row, for pasting straight into Prometheus:

```promql
# error ratio
sum(rate(rpc_server_duration_milliseconds_count{service_name="kafka", rpc_grpc_status_code!="0"}[5m]))
  / sum(rate(rpc_server_duration_milliseconds_count{service_name="kafka"}[5m]))
# kafka_server_brokertopicmetrics_messagesin_total rate
sum(rate(kafka_server_brokertopicmetrics_messagesin_total{service_name="kafka"}[5m]))
# kafka_consumergroup_lag
kafka_consumergroup_lag{service_name="kafka"}
```

In Jaeger, filter to `service = kafka` and sort by duration for the latency case, or add `error = true` for the error case.

## Diagnose

1. Pull the RED panels for `kafka`: `kafka_server_brokertopicmetrics_messagesin_total`, `kafka_consumergroup_lag`. Note whether this is an **error** spike (count of failed requests up, latency flat) or a **latency** spike (duration up, error count flat). They have different causes and the next step depends on which.
2. Open a trace for a failing `kafka` request in Jaeger. The span that carries the error status, or the span that dominates wall-clock time, points at the subsystem.
3. Check whether a fault flag is active. `kafka` has injectable faults; a flag flip is by far the most common cause in this environment. Query flagd for the flags below and compare the timestamp to when the alert started.

## Known failure modes

### `kafkaQueueProblems`

**Effect.** Broker is overloaded and consumers are given an added delay, so lag spikes.

**Signature.** kafka_consumergroup_lag climbing for accounting and fraud-detection; checkout produce latency up; storefront unaffected.

**Response.** Confirm the flag is set by querying flagd. Set it back to its resting variant (`flagd-ui`, or the incident-generator reset). Recovery is immediate on the next flag evaluation; no restart. Confirm `kafka_server_brokertopicmetrics_messagesin_total` returns to baseline before closing.

## Mitigate

1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on the next flag evaluation with no restart.
2. **If a recent deploy correlates**, roll `kafka` back to the previous image tag. `kafka` is stateless and its callers (`accounting`, `fraud-detection`) retry, so a rollback is safe mid-traffic.
3. **If an upstream is the cause**, there is nothing to do on `kafka` itself — follow that service's runbook and let this alert clear on its own.

## Escalate

If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the data team at **PD-DATA** and post in #astroshop-data with the trace id and the alert link.

## After the incident

- Confirm `kafka_server_brokertopicmetrics_messagesin_total` and the error ratio have held at baseline for 15 minutes.
- Note the flag, deploy or upstream cause in the incident channel before closing.
- If this was a new failure mode, add it to the Known failure modes section of this runbook and to `scripts/corpus/services.yaml`.

## Past incidents

- **INC-2621** (2026-07-25) — kafka degraded. Cause: `kafkaQueueProblems` fault flag active.
- **INC-2707** (2026-08-23) — kafka + email — two faults overlapping. Cause: compound — see INC-2707.
