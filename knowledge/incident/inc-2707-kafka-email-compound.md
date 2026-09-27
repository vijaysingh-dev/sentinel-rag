# INC-2707: two faults active — kafka and email

**Date:** 2026-08-23  
**Severity:** P2  
**Duration:** 20 minutes  
**Services affected:** kafka, email, and accounting, checkout, fraud-detection, frontend, frontend-proxy downstream
**Responders:** data, checkout

## Summary

Two unrelated alerts fired within the same window: `kafka` (`kafkaQueueProblems`) and `email` (`emailMemoryLeak`). Neither service appears in the other's dependency graph in either direction. **The overlap is coincidence, not cascade** — two separately-scheduled fault injections landed close together.

## Customer impact

- **kafka:** No customer-facing impact. `accounting` and `fraud-detection` fell behind on the orders topic; ledger entries and fraud scores were delayed, not lost, and caught up after recovery.
- **email:** Order-confirmation emails were delayed or dropped during the restarts. Orders themselves completed normally — the email call is best-effort and does not gate checkout.

## Timeline

- 09:00 — `kafkaQueueProblems` set to its fault variant on `kafka`. No alert yet.
- 09:02 — First alert: `kafka` signature matches `kafkaQueueProblems` — kafka_consumer_group_lag_ratio climbing for the accounting and fraud-detection groups; checkout produce latency up; storefront unaffected.
- 09:04 — `emailMemoryLeak` set to its fault variant on `email`, independently of the above.
- 09:06 — Second alert: `email` signature matches `emailMemoryLeak` — email container_memory_usage_total_bytes ramps linearly toward its 100M limit; OOM restarts; checkout unaffected (call is best-effort).
- 09:09 — Responders initially treat this as one incident given the timing overlap.
- 09:13 — flagd checked: both `kafkaQueueProblems` and `emailMemoryLeak` are active independently, set 4 minutes apart. Two incidents, tracked and resolved separately from this point.
- 09:16 — `kafkaQueueProblems` reset. `kafka` recovers.
- 09:20 — `emailMemoryLeak` reset. `email` recovers.
- 09:24 — Both services confirmed back at baseline. Incident closed.

## Root cause

Two independent feature flags were active concurrently: `kafkaQueueProblems` on `kafka` (Broker is overloaded and consumers are given an added delay, so lag spikes.) and `emailMemoryLeak` on `email` (RSS climbs steadily until the runtime OOMs and restarts.). There is no dependency relationship between the two services; the timing overlap was coincidental.

## Contributing factors

- No alert correlates flag changes with each other, so two flags active in the same window present as one confusing incident instead of two clear ones.

## What went well

- Comparing each service's actual failure *signature* against its known failure modes (rather than assuming shared timing implies a shared cause) correctly split this into two incidents within minutes of pulling both flag states.

## Action items

1. When two alerts overlap, check flagd for *all* active flags, not just the one for the service that paged — a second unrelated flag is easy to miss if the search stops at the first explanation found. *(owner: platform-eng)*

## Detection signatures

```
kafka (kafkaQueueProblems): kafka_consumer_group_lag_ratio climbing for the accounting and fraud-detection groups; checkout produce latency up; storefront unaffected.
email (emailMemoryLeak): email container_memory_usage_total_bytes ramps linearly toward its 100M limit; OOM restarts; checkout unaffected (call is best-effort).
```
