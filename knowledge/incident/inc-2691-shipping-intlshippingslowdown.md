# INC-2691: shipping elevated latency

**Date:** 2026-08-12  
**Severity:** P2  
**Duration:** 16 minutes  
**Services affected:** shipping, and checkout, frontend, frontend-proxy downstream
**Responders:** checkout (PD-CHECKOUT)

## Summary

The `intlShippingSlowdown` fault was active for 16 minutes. International destinations get a multi-second artificial delay. This surfaced first on `checkout`, which is a direct caller of `shipping`, and the first several minutes of the response were spent on `checkout` before the cause was localised to `shipping`.

## Customer impact

International orders saw multi-second checkout delays. Domestic checkout was unaffected.

## Timeline

- 07:41 — Fault injected: flag `intlShippingSlowdown` set to its fault variant. No alert yet.
- 07:43 — First alert: `checkout` error ratio crosses its page threshold. checkout and the checkout team are paged.
- 07:45 — Responder confirms the alert on `checkout` and begins checking its own dependencies rather than `checkout` itself, since `checkout` code has not changed.
- 07:48 — Trace inspection: a `checkout` request trace shows the error status originating in the `shipping` span, not in `checkout`.
- 07:50 — `shipping` RED panels match the known signature — shipping duration_p99 up only for international orders; checkout p99 up; error rates flat.
- 07:52 — flagd checked: `intlShippingSlowdown` is active, set at 07:41. Cause confirmed.
- 07:53 — Flag reset to resting variant. `shipping` recovers on the next flag evaluation.
- 07:57 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `intlShippingSlowdown` feature flag was set to its fault variant. International destinations get a multi-second artificial delay. `shipping` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `checkout` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 07:52 flagd check was manual and late.

## What went well

- Once a trace was pulled (07:48), the cause was clear within two minutes.
- The `shipping` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `checkout` runbook. *(owner: checkout)* — done, see `knowledge/runbook/checkout.md`.
3. Confirm the `shipping` runbook's known-failure-modes section lists `intlShippingSlowdown` with this signature. *(owner: checkout)*

## Detection signature

```
shipping duration_p99 up only for international orders; checkout p99 up; error rates flat.
```
