# INC-2607: payment returning errors

**Date:** 2026-07-26  
**Severity:** P1  
**Duration:** 16 minutes  
**Services affected:** payment, and checkout, frontend, frontend-proxy downstream
**Responders:** checkout (PD-CHECKOUT)

## Summary

The `paymentFailure` fault was active for 16 minutes. charge() throws for n% of requests. This surfaced first on `checkout`, which is a direct caller of `payment`, and the first several minutes of the response were spent on `checkout` before the cause was localised to `payment`.

## Customer impact

Checkout attempts failed at the payment step. Customers who reached the payment page saw an error and could not complete an order for the duration. Carts were preserved; no orders were lost or double-charged (payment is a stub with no real PSP).

## Timeline

- 13:00 — Fault injected: flag `paymentFailure` set to its fault variant. No alert yet.
- 13:02 — First alert: `checkout` error ratio crosses its page threshold. checkout and the checkout team are paged.
- 13:04 — Responder confirms the alert on `checkout` and begins checking its own dependencies rather than `checkout` itself, since `checkout` code has not changed.
- 13:07 — Trace inspection: a `checkout` request trace shows the error status originating in the `payment` span, not in `checkout`.
- 13:09 — `payment` RED panels match the known signature — payment error_rate = n% (span status ERROR on charge); checkout error_rate up in step; no latency change.
- 13:11 — flagd checked: `paymentFailure` is active, set at 13:00. Cause confirmed.
- 13:12 — Flag reset to resting variant. `payment` recovers on the next flag evaluation.
- 13:16 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `paymentFailure` feature flag was set to its fault variant. charge() throws for n% of requests. `payment` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `checkout` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 13:11 flagd check was manual and late.

## What went well

- Once a trace was pulled (13:07), the cause was clear within two minutes.
- The `payment` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `checkout` runbook. *(owner: checkout)* — done, see `knowledge/runbook/checkout.md`.
3. Confirm the `payment` runbook's known-failure-modes section lists `paymentFailure` with this signature. *(owner: checkout)*

## Detection signature

```
payment error_rate = n% (span status ERROR on charge); checkout error_rate up in step; no latency change.
```
