# INC-2684: cart taken out of rotation by a failing readiness probe

**Date:** 2026-06-13  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** cart, and checkout, frontend, frontend-proxy downstream
**Responders:** checkout (PD-CHECKOUT)

## Summary

The `failedReadinessProbe` fault was active for 16 minutes. Readiness probe fails; orchestrator takes the pod out of rotation while the process keeps running. This surfaced first on `checkout`, which is a direct caller of `cart`, and the first several minutes of the response were spent on `checkout` before the cause was localised to `cart`.

## Customer impact

Add-to-cart and cart retrieval failed for the configured fraction of requests. Affected customers saw an empty or errored cart and a share of checkouts failed early.

## Timeline

- 15:00 — Fault injected: flag `failedReadinessProbe` set to its fault variant. No alert yet.
- 15:02 — First alert: `checkout` error ratio crosses its page threshold. checkout and the checkout team are paged.
- 15:04 — Responder confirms the alert on `checkout` and begins checking its own dependencies rather than `checkout` itself, since `checkout` code has not changed.
- 15:07 — Trace inspection: a `checkout` request trace shows the error status originating in the `cart` span, not in `checkout`.
- 15:09 — `cart` RED panels match the known signature — cart request_rate drops to zero with no error spike; "no healthy upstream" from the proxy.
- 15:11 — flagd checked: `failedReadinessProbe` is active, set at 15:00. Cause confirmed.
- 15:12 — Flag reset to resting variant. `cart` recovers on the next flag evaluation.
- 15:16 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `failedReadinessProbe` feature flag was set to its fault variant. Readiness probe fails; orchestrator takes the pod out of rotation while the process keeps running. `cart` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `checkout` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 15:11 flagd check was manual and late.

## What went well

- Once a trace was pulled (15:07), the cause was clear within two minutes.
- The `cart` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `checkout` runbook. *(owner: checkout)* — done, see `knowledge/runbook/checkout.md`.
3. Confirm the `cart` runbook's known-failure-modes section lists `failedReadinessProbe` with this signature. *(owner: checkout)*

## Detection signature

```
cart request_rate drops to zero with no error spike; "no healthy upstream" from the proxy.
```
