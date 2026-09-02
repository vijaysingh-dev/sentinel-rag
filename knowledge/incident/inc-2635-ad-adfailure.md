# INC-2635: ad returning errors

**Date:** 2026-06-08  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** ad, and frontend, frontend-proxy downstream
**Responders:** catalog (PD-CATALOG)

## Summary

The `adFailure` fault was active for 16 minutes. GetAds throws for every request. This surfaced first on `frontend`, which is a direct caller of `ad`, and the first several minutes of the response were spent on `frontend` before the cause was localised to `ad`.

## Customer impact

Minimal. The ad slot on the product page rendered empty; browse and checkout were unaffected.

## Timeline

- 13:00 — Fault injected: flag `adFailure` set to its fault variant. No alert yet.
- 13:02 — First alert: `frontend` error ratio crosses its page threshold. catalog and the storefront team are paged.
- 13:04 — Responder confirms the alert on `frontend` and begins checking its own dependencies rather than `frontend` itself, since `frontend` code has not changed.
- 13:07 — Trace inspection: a `frontend` request trace shows the error status originating in the `ad` span, not in `frontend`.
- 13:09 — `ad` RED panels match the known signature — ad error_rate 100%; frontend degrades gracefully (ad slot empty), no cascade past frontend.
- 13:11 — flagd checked: `adFailure` is active, set at 13:00. Cause confirmed.
- 13:12 — Flag reset to resting variant. `ad` recovers on the next flag evaluation.
- 13:16 — `frontend` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `adFailure` feature flag was set to its fault variant. GetAds throws for every request. `ad` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `frontend` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 13:11 flagd check was manual and late.

## What went well

- Once a trace was pulled (13:07), the cause was clear within two minutes.
- The `ad` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `frontend` runbook. *(owner: storefront)* — done, see `knowledge/runbook/frontend.md`.
3. Confirm the `ad` runbook's known-failure-modes section lists `adFailure` with this signature. *(owner: catalog)*

## Detection signature

```
ad error_rate 100%; frontend degrades gracefully (ad slot empty), no cascade past frontend.
```
