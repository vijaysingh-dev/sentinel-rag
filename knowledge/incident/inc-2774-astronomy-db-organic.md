# INC-2774: astronomy-db connection pool exhaustion

**Date:** 2026-09-07  
**Severity:** P2  
**Duration:** 25 minutes  
**Services affected:** astronomy-db, and product-catalog, checkout, frontend, recommendation, frontend-proxy downstream
**Responders:** platform-eng (PD-PLATFORM)

## Summary

product-catalog's connection pool to astronomy-db was sized for steady-state traffic; a routine traffic ramp from the load generator exceeded it, and requests queued for a free connection instead of failing fast. No feature flag was involved — flagd showed nothing active for the entire window, which cost several minutes early on: the on-call habit here is to check flagd first, and an empty result was read as 'nothing to see' rather than as a real answer to keep investigating past. The first alert fired on `product-catalog`, not `astronomy-db` directly.

## Customer impact

Degradation on astronomy-db, product-catalog, checkout, frontend, recommendation, frontend-proxy for the duration.

## Timeline

- 12:00 — First alert: `product-catalog` duration_p95 crosses its page threshold.
- 12:03 — On-call queries flagd: no fault flag active anywhere on the platform. Investigation continues past the usual first check.
- 12:08 — Trace inspection points at `astronomy-db`: postgresql_backends pinned at postgresql_connection_max; product-catalog duration_p95 up; astronomy-db itself shows no errors, only saturation.
- 12:14 — platform-eng confirms: product-catalog's connection pool to astronomy-db was sized for steady-state traffic; a routine traffic ramp from the load generator exceeded it, and requests queued for a free connection instead of failing fast.
- 12:18 — Mitigation applied: Raised the pool size and added a queue-wait timeout so callers fail fast instead of queuing indefinitely. No flag to reset — this was capacity, not injection.
- 12:25 — `product-catalog` metrics back at baseline for 3 minutes. Incident closed.

## Root cause

product-catalog's connection pool to astronomy-db was sized for steady-state traffic; a routine traffic ramp from the load generator exceeded it, and requests queued for a free connection instead of failing fast. No fault flag was active; this was not an injected failure.

## Contributing factors

- This environment's incidents are overwhelmingly flag-driven, which made the flagd check feel like the whole investigation instead of the first step of it. An empty flagd result is evidence, not an answer.
- The alert surfaced on `product-catalog`, one hop from the actual cause at `astronomy-db`.

## What went well

- Once flagd came back empty, the team moved to traces and deploy history rather than re-checking flagd repeatedly.

## Action items

1. Raised the pool size and added a queue-wait timeout so callers fail fast instead of queuing indefinitely. No flag to reset — this was capacity, not injection. *(owner: platform-eng)*

## Detection signature

```
postgresql_backends pinned at postgresql_connection_max; product-catalog duration_p95 up; astronomy-db itself shows no errors, only saturation.
```
