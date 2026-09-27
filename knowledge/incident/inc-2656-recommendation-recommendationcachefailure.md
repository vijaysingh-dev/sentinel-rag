# INC-2656: recommendation cache disabled, upstream fan-out spike

**Date:** 2026-06-19  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** recommendation, and frontend, frontend-proxy downstream
**Responders:** catalog (PD-CATALOG)

## Summary

The `recommendationCacheFailure` fault was active for 16 minutes. The cache never hits, so every request fans out to product-catalog; memory also grows unbounded. This surfaced first on `frontend`, which is a direct caller of `recommendation`, and the first several minutes of the response were spent on `frontend` before the cause was localised to `recommendation`.

## Customer impact

The "you may also like" carousel was slow or empty. No checkout impact. Secondary effect: `product-catalog` load rose sharply, which risked a wider incident if sustained.

## Timeline

- 15:00 — Fault injected: flag `recommendationCacheFailure` set to its fault variant. No alert yet.
- 15:02 — First alert: `frontend` error ratio crosses its page threshold. catalog and the storefront team are paged.
- 15:04 — Responder confirms the alert on `frontend` and begins checking its own dependencies rather than `frontend` itself, since `frontend` code has not changed.
- 15:07 — Trace inspection: a `frontend` request trace shows the error status originating in the `recommendation` span, not in `frontend`.
- 15:09 — `recommendation` RED panels match the known signature — product-catalog request_rate up sharply (every recommendation fans out); recommendation process_memory_usage_bytes climbing.
- 15:11 — flagd checked: `recommendationCacheFailure` is active, set at 15:00. Cause confirmed.
- 15:12 — Flag reset to resting variant. `recommendation` recovers on the next flag evaluation.
- 15:16 — `frontend` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `recommendationCacheFailure` feature flag was set to its fault variant. The cache never hits, so every request fans out to product-catalog; memory also grows unbounded. `recommendation` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `frontend` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 15:11 flagd check was manual and late.
- This is a resource/latency fault, not a hard error. It does not trip an error-rate alert, so detection depends on a latency SLO being set and paged on.

## What went well

- Once a trace was pulled (15:07), the cause was clear within two minutes.
- The `recommendation` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `frontend` runbook. *(owner: storefront)* — done, see `knowledge/runbook/frontend.md`.
3. Confirm the `recommendation` runbook's known-failure-modes section lists `recommendationCacheFailure` with this signature. *(owner: catalog)*

## Detection signature

```
product-catalog request_rate up sharply (every recommendation fans out); recommendation process_memory_usage_bytes climbing.
```
