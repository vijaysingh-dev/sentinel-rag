# ADR-003: The RCA agent reads metrics and traces, not logs

**Status:** Accepted

## Context

The RCA agent needs live signal about a running incident. The three telemetry signals available
are metrics (Prometheus), traces (Jaeger) and logs. An agent with a log-search tool is
straightforward to build.

## Decision

The agent's live-state tools read Prometheus (which service's RED signals moved, and when) and
Jaeger (which span in a failing request carries the error or the latency). There is no log-search
tool.

## Alternatives considered

- **Add a log-search tool.** High recall, low precision. An agent tends to anchor on the first plausible-looking log line and build a narrative around it. Log volume also makes context-window management hard.
- **Logs as a fallback only.** Still ships the failure mode above; the agent will reach for the fallback whenever metrics and traces are ambiguous, which is exactly when anchoring is most costly.

## Consequences

Positive: metrics and traces are low-cardinality, structured signals an agent can reason over
without being flooded. The investigation stays on "which service, which span, what changed".
Negative: some root causes are only visible in a log line (a specific exception message, a config
value printed at startup). For those the agent produces a well-scoped hypothesis and hands off to
a human with the trace id, rather than confirming it itself. This is a deliberate ceiling on the
agent's autonomy.
