# Astroshop security review — 2026 H1

**Classification:** Internal — restricted. Admin access only.

## Scope

Threat model review of the checkout path and the flag control plane. Covers the synchronous order
pipeline (cart, currency, product-catalog, shipping, payment) and flagd's configuration source.

## Findings

### SEC-1 (High) — flagd config is writable by any process that mounts the shared volume

The incident-generator and flagd-ui both write demo.flagd.json directly. Any workload with that
mount can flip a fault flag, including one that should not. There is no authentication on the
write path and no audit of who changed a flag.

**Remediation.** Move flag writes behind flagd's own API with a token; emit an audit event per change. Not yet
scheduled.

### SEC-2 (Medium) — Generated SQL in the RCA warehouse tool relies on parse-time checks plus a read-only role

The read-only database role is the real control; the sqlglot parse and single-statement check are
defence in depth. If the role were ever misconfigured with write access, the parser checks alone
would not stop a crafted statement that parses as a SELECT but has side effects via a function
call.

**Remediation.** Add a periodic assertion that the role cannot write, alerting if it can. Open.

### SEC-3 (Medium) — Restricted-document figures are enriched at ingest by an external vision model

The figure-description path sends images to a vision model at ingest time. A restricted document's
diagram is described and that description becomes a chunk. The chunk inherits the document's ACL,
so retrieval is safe, but the image itself transited an external service during ingest.

**Remediation.** Confirm the vision endpoint is in-tenant; document the data flow in docs/adapters.md. Done.

### SEC-4 (Low) — No rate limit on the RCA investigation API

POST /v1/investigations has no per-caller quota. A misbehaving client could start unbounded
investigations, each of which runs LLM calls and warehouse queries.

**Remediation.** Add a token-bucket limit per tenant. Backlog.

## Sign-off

Two findings (SEC-1, SEC-2) remain open at the time of writing and are the reason this document is
restricted: they describe an exploitable gap that is not yet closed. Review again once SEC-1 is
scheduled.
