# Feed Staleness Response

Upstream price feeds arrive on a predictable schedule. Late or incomplete data produces index
values calculated from stale prices, which is worse than no value at all because it looks correct.

## Thresholds

| Condition | Threshold | Action |
| --- | --- | --- |
| Warning | 90 seconds late | Alert operations, continue publishing |
| Critical | 300 seconds late | Suspend affected indices from publication |
| Incomplete | Under 95% of expected constituents | Suspend affected indices |

Suspension is automatic at the critical threshold. Manual override requires two approvals.

## Steps

1. Identify the affected feed and the indices that depend on it. One feed typically serves many
   indices; the blast radius is rarely a single index.
2. Check whether the delay is upstream (the vendor has not sent data) or internal (the data
   arrived and the ingestion pipeline is backed up). These have completely different remedies and
   confusing them wastes the critical first minutes.
3. If upstream, contact the vendor and check their status page. Record the incident reference.
4. If internal, check Kafka consumer lag on the feed topic and the ingestion service health.
5. If the feed recovers within the critical window, confirm that back-filled prices were applied
   in timestamp order. Out-of-order application produces a correct final value with an incorrect
   intraday path.
6. If suspension occurred, resume publication only after confirming a full snapshot has been
   received and the first recalculated value is continuous with the last published one.

## Communication

Exchanges must be notified of any suspension exceeding 5 minutes. The notification template is in
the exchange delivery runbook.
