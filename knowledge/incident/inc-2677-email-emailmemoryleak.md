# INC-2677: email memory growth and restarts

**Date:** 2026-08-13  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** email, and checkout, frontend, frontend-proxy downstream
**Responders:** checkout (PD-CHECKOUT)

## Summary

The `emailMemoryLeak` fault was active for 16 minutes. RSS climbs steadily until the runtime OOMs and restarts. This surfaced first on `checkout`, which is a direct caller of `email`, and the first several minutes of the response were spent on `checkout` before the cause was localised to `email`.

## Customer impact

Order-confirmation emails were delayed or dropped during the restarts. Orders themselves completed normally — the email call is best-effort and does not gate checkout.

## Timeline

- 10:13 — Fault injected: flag `emailMemoryLeak` set to its fault variant. No alert yet.
- 10:15 — First alert: `checkout` duration p95 crosses its page threshold. checkout and the checkout team are paged.
- 10:17 — Responder confirms the alert on `checkout` and begins checking its own dependencies rather than `checkout` itself, since `checkout` code has not changed.
- 10:20 — Trace inspection: a `checkout` request trace shows the time originating in the `email` span, not in `checkout`.
- 10:22 — `email` RED panels match the known signature — email process_resident_memory_bytes ramps linearly; periodic restarts; checkout unaffected (call is best-effort).
- 10:24 — flagd checked: `emailMemoryLeak` is active, set at 10:13. Cause confirmed.
- 10:25 — Flag reset to resting variant. `email` recovers on the next flag evaluation.
- 10:29 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `emailMemoryLeak` feature flag was set to its fault variant. RSS climbs steadily until the runtime OOMs and restarts. `email` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `checkout` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 10:24 flagd check was manual and late.

## What went well

- Once a trace was pulled (10:20), the cause was clear within two minutes.
- The `email` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `checkout` runbook. *(owner: checkout)* — done, see `knowledge/runbook/checkout.md`.
3. Confirm the `email` runbook's known-failure-modes section lists `emailMemoryLeak` with this signature. *(owner: checkout)*

## Detection signature

```
email process_resident_memory_bytes ramps linearly; periodic restarts; checkout unaffected (call is best-effort).
```
