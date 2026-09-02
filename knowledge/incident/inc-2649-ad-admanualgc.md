# INC-2649: ad CPU/GC saturation, elevated latency

**Date:** 2026-06-18  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** ad, and frontend, frontend-proxy downstream
**Responders:** catalog (PD-CATALOG)

## Summary

The `adManualGc` fault was active for 16 minutes. Forced full GC pauses on a loop. This surfaced first on `frontend`, which is a direct caller of `ad`, and the first several minutes of the response were spent on `frontend` before the cause was localised to `ad`.

## Customer impact

Minimal. The ad slot on the product page rendered empty; browse and checkout were unaffected.

## Timeline

- 11:22 — Fault injected: flag `adManualGc` set to its fault variant. No alert yet.
- 11:24 — First alert: `frontend` duration p95 crosses its page threshold. catalog and the storefront team are paged.
- 11:26 — Responder confirms the alert on `frontend` and begins checking its own dependencies rather than `frontend` itself, since `frontend` code has not changed.
- 11:29 — Trace inspection: a `frontend` request trace shows the time originating in the `ad` span, not in `frontend`.
- 11:31 — `ad` RED panels match the known signature — ad jvm_gc_duration_seconds spikes periodically; sawtooth latency; correlates with GC, not load.
- 11:33 — flagd checked: `adManualGc` is active, set at 11:22. Cause confirmed.
- 11:34 — Flag reset to resting variant. `ad` recovers on the next flag evaluation.
- 11:38 — `frontend` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `adManualGc` feature flag was set to its fault variant. Forced full GC pauses on a loop. `ad` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `frontend` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 11:33 flagd check was manual and late.
- This is a resource/latency fault, not a hard error. It does not trip an error-rate alert, so detection depends on a latency SLO being set and paged on.

## What went well

- Once a trace was pulled (11:29), the cause was clear within two minutes.
- The `ad` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `frontend` runbook. *(owner: storefront)* — done, see `knowledge/runbook/frontend.md`.
3. Confirm the `ad` runbook's known-failure-modes section lists `adManualGc` with this signature. *(owner: catalog)*

## Detection signature

```
ad jvm_gc_duration_seconds spikes periodically; sawtooth latency; correlates with GC, not load.
```
