# INC-2628: product-catalog returning errors

**Date:** 2026-06-16  
**Severity:** P1  
**Duration:** 16 minutes  
**Services affected:** product-catalog, and checkout, frontend, recommendation, frontend-proxy downstream
**Responders:** catalog (PD-CATALOG)

## Summary

The `productCatalogFailure` fault was active for 16 minutes. GetProduct fails for one specific product id; ListProducts is unaffected. This surfaced first on `checkout`, which is a direct caller of `product-catalog`, and the first several minutes of the response were spent on `checkout` before the cause was localised to `product-catalog`.

## Customer impact

One product's detail page returned an error; the rest of the catalog was fine. Listing and search were unaffected. Narrow blast radius despite being a shared dependency.

## Timeline

- 10:41 — Fault injected: flag `productCatalogFailure` set to its fault variant. No alert yet.
- 10:43 — First alert: `checkout` error ratio crosses its page threshold. catalog and the checkout team are paged.
- 10:45 — Responder confirms the alert on `checkout` and begins checking its own dependencies rather than `checkout` itself, since `checkout` code has not changed.
- 10:48 — Trace inspection: a `checkout` request trace shows the error status originating in the `product-catalog` span, not in `checkout`.
- 10:50 — `product-catalog` RED panels match the known signature — product-catalog error_rate low but non-zero; errors correlate with one product; frontend PDP errors for that product only.
- 10:52 — flagd checked: `productCatalogFailure` is active, set at 10:41. Cause confirmed.
- 10:53 — Flag reset to resting variant. `product-catalog` recovers on the next flag evaluation.
- 10:57 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The `productCatalogFailure` feature flag was set to its fault variant. GetProduct fails for one specific product id; ListProducts is unaffected. `product-catalog` itself was healthy — correct version, no resource pressure, dependencies up. The failure was entirely injected at the flag layer.

## Contributing factors

- The alert fires on `checkout` but the cause is one hop away. Without a dependency-aware first response, the natural move is to investigate the service that paged, which is the wrong one here.
- No alert correlates flag changes with incident start. flagd emits `flagd_flag_evaluations_total` but nothing pages on a flip, so the 10:52 flagd check was manual and late.

## What went well

- Once a trace was pulled (10:48), the cause was clear within two minutes.
- The `product-catalog` runbook's documented signature matched exactly, so no time was lost confirming.

## Action items

1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` deltas, so a flag flip is visible on every dashboard. *(owner: platform-eng)*
2. Add "check the dependency graph before the service that paged" as step 1 of the `checkout` runbook. *(owner: checkout)* — done, see `knowledge/runbook/checkout.md`.
3. Confirm the `product-catalog` runbook's known-failure-modes section lists `productCatalogFailure` with this signature. *(owner: catalog)*

## Detection signature

```
product-catalog error_rate low but non-zero; errors correlate with one product; frontend PDP errors for that product only.
```
