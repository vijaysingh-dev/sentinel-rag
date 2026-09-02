# ADR-004: Live state is never embedded into the knowledge index

**Status:** Accepted

## Context

It is tempting to index everything: current metrics, recent deploys, the current flag
configuration, open incidents. It would make the retriever a single entry point for every
question.

## Decision

The index contains only stable knowledge — runbooks, post-mortems, ADRs, the glossary, the
architecture deck. Anything that changes minute to minute is fetched by an agent tool at
investigation time and is never embedded. Live-state tools are additionally excluded from every
cache layer (core/cache.LIVE_TOOLS).

## Alternatives considered

- **Index live state with a short TTL and frequent re-ingestion.** A vector built from a metric value is stale the moment it is embedded; a short TTL just means constant re-indexing and a race between the index and reality.
- **Index deploy history only (it changes slowly).** Reasonable for deploys, but it blurs the boundary — the next person adds 'and current flag state, it also changes slowly' and the principle is gone.

## Consequences

Positive: a retrieved chunk is always something that was true when written and is still meaningful
now. The boundary is bright: if it has a timestamp that matters, it is a tool call, not a
document. Negative: answering "why is checkout slow right now" requires the agent to combine a
retrieved runbook with a live trace — neither alone is enough — so the agent cannot be a pure RAG
system. That combination is the whole point of the agent layer.
