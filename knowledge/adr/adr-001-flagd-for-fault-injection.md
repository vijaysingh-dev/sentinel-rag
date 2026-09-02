# ADR-001: Inject faults by flag file, not by an API or a chaos tool

**Status:** Accepted

## Context

The platform needs a way to produce realistic failures on demand, both for the incident-
generator's scheduled scenarios and for a human running one by hand. The options were a dedicated
chaos-engineering tool (Chaos Mesh, Litmus), a fault-injection API added to each service, or
reusing the feature-flag daemon (flagd) that is already in the deployment for demo toggles.

## Decision

Faults are flag flips. Each fault is a variant on a flag in demo.flagd.json; flagd watches that
file and reloads on change, so setting a fault is a single-file edit with no restart and no API
call. The incident-generator writes the same file on a schedule; flagd-ui edits it from a browser;
a human can edit it directly. One mechanism, three front ends.

## Alternatives considered

- **A chaos tool.** More realistic failure modes (network partitions, pod kills) but a second control plane to run and secure, and its actions are not trivially correlated with a ground-truth log.
- **A fault API per service.** Precise, but N services to modify and keep in sync, and the fault code ships in the production image.

## Consequences

Positive: the incident-generator's incidents.jsonl is exact ground truth — it records the flag it
set and when, so the RCA agent can be scored against a known cause. Recovery is instant (next flag
evaluation) so scenarios are cheap to run repeatedly. Negative: the fault surface is limited to
what a flag can express — application-level errors, latency and resource pressure, but not
infrastructure failures like a network partition or a disk filling. Those have to be tested
another way. Also: nothing pages on a flag change, so every runbook carries a manual "check flagd"
step and most post-mortems list the missing flag-change alert as a contributing factor.
