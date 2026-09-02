# INC-2663: cart returning errors

**Date:** 2026-08-13  
**Severity:** P1  
**Duration:** 16 minutes  
**Services affected:** cart, and checkout, frontend, frontend-proxy downstream
**Responders:** checkout (PD-CHECKOUT)

## Summary

The `cartFailure` fault was active for 16 minutes. AddItem and GetCart fail for a configurable percentage of calls. This surfaced first on `checkout`, which is a direct caller of `cart`, and the first several minutes of the response were spent on `checkout` before the cause was localised to `cart`.

## Customer impact

Add-to-cart and cart retrieval failed for the configured fraction of requests. Affected customers saw an empty or errored cart and a share of checkouts failed early.

## Timeline

- 11:41 — Fault injected: flag `cartFailure` set to its fault variant. No alert yet.
- 11:43 — First alert: `checkout` error ratio crosses its page threshold. checkout and the checkout team are paged.
- 11:45 — Responder confirms the alert on `checkout` and begins checking its own dependencies rather than `checkout` itself, since `checkout` code has not changed.
- 11:48 — Trace inspection: a `checkout` request trace shows the error status originating in the `cart` span, not in `checkout`.
- 11:50 — `cart` RED panels match the known signature — cart error_rate up by the configured fraction; checkout error_rate up in step; valkey-cart healthy.
- 11:52 — flagd checked: `cartFailure` is active, set at 11:41. Cause confirmed.
- 11:53 — Flag reset to resting variant. `cart` recovers on the next flag evaluation.
- 11:57 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `cartFailure` feature flag was set to its fault variant. AddItem and GetCart fail for a configurable percentage of calls. `cart` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `checkout` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 11:52 flagd check was manual and late.

## What went well

- Once a trace was pulled (11:48), the cause was clear within two minutes.
- The `cart` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `checkout` runbook. *(owner: checkout)* — done, see `knowledge/runbook/checkout.md`.
3. Confirm the `cart` runbook's known-failure-modes section lists `cartFailure` with this signature. *(owner: checkout)*

## Detection signature

```
cart error_rate up by the configured fraction; checkout error_rate up in step; valkey-cart healthy.
```
