# ADR-005: Access-control filters are applied inside the vector query, not after it

**Status:** Accepted

## Context

The corpus has role-scoped content: some post-mortems name customers and monetary impact and are
admin-only; some runbooks are scoped to the team that owns the service. The retriever must not
return a chunk the caller's role may not see.

## Decision

The role filter is a clause in the vector search query itself. The store never loads a restricted
chunk into the process. An empty role list raises rather than matching everything
(SearchFilter.require_scope), and a refusal never states that a restricted document exists.

## Alternatives considered

- **Retrieve then filter in application code.** The restricted content has already been read into the process and logged in traces before the filter runs. One logging misconfiguration and it leaks.
- **A separate index per role.** No cross-role retrieval is possible even when it should be, and every document has to be routed to the right index at ingest — a routing bug is a silent leak.

## Consequences

Positive: restricted content is never in the process for an unauthorised caller, so a logging or
serialisation bug cannot leak it. The scope comes from the request context, never a tool argument,
so a prompt injection cannot widen it. Negative: the store adapter must support metadata filtering
in the query (pgvector, Azure AI Search and OpenSearch all do); a store that does not could not be
used without violating this. That constraint is accepted and noted in docs/adapters.md.
