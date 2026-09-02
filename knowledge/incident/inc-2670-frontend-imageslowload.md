# INC-2670: frontend elevated latency

**Date:** 2026-06-24  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** frontend, and frontend-proxy downstream
**Responders:** storefront (PD-STOREFRONT)

## Summary

The `imageSlowLoad` fault was active for 16 minutes. Product images take seconds to load; page render blocks on them. This surfaced first on `frontend-proxy`, which is a direct caller of `frontend`, and the first several minutes of the response were spent on `frontend-proxy` before the cause was localised to `frontend`.

## Customer impact

Degradation on frontend-proxy for the duration.

## Timeline

- 08:41 — Fault injected: flag `imageSlowLoad` set to its fault variant. No alert yet.
- 08:43 — First alert: `frontend-proxy` duration p95 crosses its page threshold. storefront and the storefront team are paged.
- 08:45 — Responder confirms the alert on `frontend-proxy` and begins checking its own dependencies rather than `frontend-proxy` itself, since `frontend-proxy` code has not changed.
- 08:48 — Trace inspection: a `frontend-proxy` request trace shows the time originating in the `frontend` span, not in `frontend-proxy`.
- 08:50 — `frontend` RED panels match the known signature — frontend duration_p95 up, downstream services healthy, image-provider latency up.
- 08:52 — flagd checked: `imageSlowLoad` is active, set at 08:41. Cause confirmed.
- 08:53 — Flag reset to resting variant. `frontend` recovers on the next flag evaluation.
- 08:57 — `frontend-proxy` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `imageSlowLoad` feature flag was set to its fault variant. Product images take seconds to load; page render blocks on them. `frontend` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `frontend-proxy` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 08:52 flagd check was manual and late.

## What went well

- Once a trace was pulled (08:48), the cause was clear within two minutes.
- The `frontend` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `frontend-proxy` runbook. *(owner: storefront)* — done, see `knowledge/runbook/frontend-proxy.md`.
3. Confirm the `frontend` runbook's known-failure-modes section lists `imageSlowLoad` with this signature. *(owner: storefront)*

## Detection signature

```
frontend duration_p95 up, downstream services healthy, image-provider latency up.
```
