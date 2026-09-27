# INC-2714: two faults active — recommendation and product-catalog

**Date:** 2026-08-26  
**Severity:** P1  
**Duration:** 20 minutes  
**Services affected:** recommendation, product-catalog, and checkout, frontend, frontend-proxy downstream
**Responders:** catalog

## Summary

Two alerts fired minutes apart: `recommendation` (`recommendationCacheFailure`) and `product-catalog` (`productCatalogFailure`). `recommendation` calls `product-catalog` in its normal operation, so the first hypothesis was a single cascading failure — `recommendation`'s fault driving `product-catalog` into distress. That hypothesis does not survive contact with the signatures: `productCatalogFailure` only affects one specific request shape (GetProduct fails for one specific product id; ListProducts is unaffected), not the traffic volume `recommendationCacheFailure` would have added. **These are two independent faults that happened to overlap**, not one causing the other.

## Customer impact

- **recommendation:** The "you may also like" carousel was slow or empty. No checkout impact. Secondary effect: `product-catalog` load rose sharply, which risked a wider incident if sustained.
- **product-catalog:** One product's detail page returned an error; the rest of the catalog was fine. Listing and search were unaffected. Narrow blast radius despite being a shared dependency.

## Timeline

- 11:00 — `recommendationCacheFailure` set to its fault variant on `recommendation`. No alert yet.
- 11:02 — First alert: `recommendation` signature matches `recommendationCacheFailure` — product-catalog request_rate up sharply (every recommendation fans out); recommendation process_memory_usage_bytes climbing.
- 11:04 — `productCatalogFailure` set to its fault variant on `product-catalog`, independently of the above.
- 11:06 — Second alert: `product-catalog` signature matches `productCatalogFailure` — product-catalog error_rate low but non-zero; errors correlate with one product; frontend PDP errors for that product only.
- 11:09 — Responders initially treat this as one incident given the timing overlap; `recommendation` calls `product-catalog`, so cascade is the natural first guess.
- 11:13 — flagd checked: both `recommendationCacheFailure` and `productCatalogFailure` are active independently, set 4 minutes apart. Two incidents, tracked and resolved separately from this point.
- 11:16 — `recommendationCacheFailure` reset. `recommendation` recovers.
- 11:20 — `productCatalogFailure` reset. `product-catalog` recovers.
- 11:24 — Both services confirmed back at baseline. Incident closed.

## Root cause

Two independent feature flags were active concurrently: `recommendationCacheFailure` on `recommendation` (The cache never hits, so every request fans out to product-catalog; memory also grows unbounded.) and `productCatalogFailure` on `product-catalog` (GetProduct fails for one specific product id; ListProducts is unaffected.). The dependency between the two services made this look like one cascading fault; it was two.

## Contributing factors

- No alert correlates flag changes with each other, so two flags active in the same window present as one confusing incident instead of two clear ones.
- `product-catalog` genuinely is a dependency of `recommendation`, which made the wrong hypothesis (single cascade) a reasonable first guess, not a careless one. The signature — a single-product failure rather than a load-shaped one — is what separates them.

## What went well

- Comparing each service's actual failure *signature* against its known failure modes (rather than assuming shared timing implies a shared cause) correctly split this into two incidents within minutes of pulling both flag states.

## Action items

1. When two alerts overlap, check flagd for *all* active flags, not just the one for the service that paged — a second unrelated flag is easy to miss if the search stops at the first explanation found. *(owner: platform-eng)*

## Detection signatures

```
recommendation (recommendationCacheFailure): product-catalog request_rate up sharply (every recommendation fans out); recommendation process_memory_usage_bytes climbing.
product-catalog (productCatalogFailure): product-catalog error_rate low but non-zero; errors correlate with one product; frontend PDP errors for that product only.
```
