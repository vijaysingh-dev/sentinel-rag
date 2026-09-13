# INC-2767: currency elevated latency after a deploy

**Date:** 2026-09-03  
**Severity:** P2  
**Duration:** 25 minutes  
**Services affected:** currency, and checkout, frontend, frontend-proxy downstream
**Responders:** checkout (PD-CHECKOUT)

## Summary

The 14:20 deploy added a synchronous call to an external exchange-rate refresh on every conversion request, instead of reading the rate cached at startup. No feature flag was involved — flagd showed nothing active for the entire window, which cost several minutes early on: the on-call habit here is to check flagd first, and an empty result was read as 'nothing to see' rather than as a real answer to keep investigating past. The first alert fired on `checkout`, not `currency` directly.

## Customer impact

Degradation on currency, checkout, frontend, frontend-proxy for the duration.

## Timeline

- 13:00 — First alert: `checkout` duration_p95 crosses its page threshold.
- 13:03 — On-call queries flagd: no fault flag active anywhere on the platform. Investigation continues past the usual first check.
- 13:08 — Trace inspection points at `currency`: currency duration_p95 up ~6x immediately after the 14:20 deploy; error rate flat; no fault flag active.
- 13:14 — checkout confirms: the 14:20 deploy added a synchronous call to an external exchange-rate refresh on every conversion request, instead of reading the rate cached at startup.
- 13:18 — Mitigation applied: Rolled back to the previous image tag. Follow-up: cache the exchange rate and refresh it on a timer, not per-request.
- 13:25 — `checkout` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

The 14:20 deploy added a synchronous call to an external exchange-rate refresh on every conversion request, instead of reading the rate cached at startup. No fault flag was active; this was not an injected failure.

## Contributing factors

- This environment's incidents are overwhelmingly flag-driven, which made the flagd check feel like the whole investigation instead of the first step of it. An empty flagd result is evidence, not an answer.
- The alert surfaced on `checkout`, one hop from the actual cause at `currency`.

## What went well

- Once flagd came back empty, the team moved to traces and deploy history rather than re-checking flagd repeatedly.

## Action items

1. Rolled back to the previous image tag. Follow-up: cache the exchange rate and refresh it on a timer, not per-request. *(owner: checkout)*

## Detection signature

```
currency duration_p95 up ~6x immediately after the 14:20 deploy; error rate flat; no fault flag active.
```
