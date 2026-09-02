# ADR-002: Exclude otel-collector and flagd from the dependency graph and health rollup

**Status:** Accepted

## Context

app/system-monitor computes a health status for each service and propagates it up the dependency
graph, so a single red leaf shows as degraded on every service that depends on it. The graph is
derived from the deployment's own depends_on declarations. Almost every service declares a
dependency on otel-collector (for telemetry) and flagd (for config).

## Decision

otel-collector, flagd and the load-generator are excluded from the graph the rollup walks. The
graph contains only business dependencies — the calls a service makes to do its job.

## Alternatives considered

- **Include them.** Truthful to the compose file, but the rollup would then say "every service's parent is otel- collector": one collector blip turns the whole board red and hides which business service actually failed.
- **Include the collector but weight it lower.** Adds a tuning knob and a second concept (dependency strength) for one special case.

## Consequences

Positive: the health board reflects business impact — a red checkout means orders are failing, not
that a sidecar restarted. The same reasoning is applied consistently in the RCA agent's scope
boundary and in the architecture deck's dependency slide. Negative: a genuine collector outage
(telemetry stops) is invisible to the rollup and has to be caught by a separate direct check on
the collector itself. This is an accepted blind spot, documented here so it is a decision and not
a surprise.
