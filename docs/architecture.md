# Architecture

## The shape

```
                    ┌─────────────────────────────────────────────┐
   documents  ──▶   │  INGESTION                                  │
   (pdf, docx,      │                                             │
    pptx, xlsx,     │  parse ─▶ enrich figures ─▶ chunk ─▶ ACL    │
    csv, md, html)  │    │                          │      tag    │
                    │    │                          │       │     │
                    │  hybrid routing         per doc-type  │     │
                    │  local → cloud          strategy      ▼     │
                    │  on quality score                  embed    │
                    └────────────────────────────────────────┬────┘
                                                             ▼
                    ┌─────────────────────────────────────────────┐
                    │  VECTOR STORE (one of three)                │
                    │  pgvector │ Azure AI Search │ OpenSearch    │
                    │  + BM25 index, ACL fields, index_version    │
                    └────────────────────────────────────────┬────┘
                                                             ▼
   query      ──▶   ┌─────────────────────────────────────────────┐
                    │  RETRIEVAL                                  │
                    │  rewrite ─▶ cache ─▶ hybrid ─▶ rerank ─▶    │
                    │                      (k=50)     (n=6)       │
                    │  floor ─▶ dedupe ─▶ MMR ─▶ budget fit       │
                    └────────────────────────────────────────┬────┘
                                                             ▼
                    ┌─────────────────────────────────────────────┐
                    │  RCA AGENT GRAPH                            │
                    │                                             │
                    │  intake ─▶ supervisor ⇄ knowledge           │
                    │                       ⇄ data                │
                    │                       ⇄ change              │
                    │                       ↘ verifier ⇄          │
                    │                       ↘ writer ─▶ approval  │
                    │                                  ─▶ publish │
                    └─────────────────────────────────────────────┘
```

## Three layers, three concerns

**The adapter layer** (`core/registry.py` plus the per-stage `base.py` files) makes providers interchangeable. Every stage has one interface and three implementations. Nothing above this layer knows which cloud is answering.

**The pipeline layer** (`parsing/`, `chunking/`, `embedding/`, `vectorstore/`, `retrieval/`, `reranking/`) composes those adapters into an ingestion path and a retrieval path. Each stage is independently benchmarkable, which is what makes the leaderboards in `docs/` possible.

**The agent layer** (`rca/`) consumes retrieval as one tool among several. It is the only layer that knows about incidents, specialists and human approval; it knows nothing about embeddings or filter syntax.

## Data flow, in detail

### Ingestion

1. **Discover.** `ingest/pipeline.ingest_directory` walks a tree. The parent directory name becomes `doc_type`, which in turn selects the chunking strategy — convention over configuration, and it keeps the corpus layout self-documenting.
2. **Parse.** `parsing/hybrid.HybridParser` routes per document. Formats that already carry structure (markdown, CSV, XLSX) never leave the local parser. Scans go straight to cloud OCR. Everything else is parsed locally, scored, and escalated only if it falls below threshold.
3. **Enrich figures.** Optional. Diagrams are described by a vision model and the description is indexed, with the image URI retained so an answer can link to the picture. Decorative images are dropped rather than becoming empty chunks.
4. **Chunk.** `chunking/strategies.chunker_for_doc` picks by document type. Every chunk gets a content-derived id, a heading path, and both text fields.
5. **Tag.** `ingest/acl.AclResolver` assigns `allowed_roles` and `components` from a reviewed YAML file, deny-by-default.
6. **Embed.** Only chunks whose ids are absent from the index — a one-paragraph edit to a 90-page PDF re-embeds one chunk, not four hundred.
7. **Write.** Upsert new chunks, tombstone orphans. Failures go to a dead-letter queue rather than being silently skipped.

### Retrieval

`retrieval/pipeline.RetrievalPipeline.retrieve` is the single entry point. It builds a `SearchFilter` from the caller's authenticated scope, checks the role-namespaced cache, runs hybrid search wide, reranks narrow, applies a relevance floor, deduplicates, optionally diversifies, and fits the result to a token budget.

Every step has a degraded path, and every degradation increments a counter.

### Investigation

The supervisor holds no tools. It reads accumulated evidence and decides which specialists run next, usually several concurrently. Specialists write structured evidence to shared state and a one-paragraph summary to the supervisor's messages. The verifier reviews adversarially and can send questions back. The writer drafts, and `interrupt()` hands the draft to a human before anything is published.

## Boundaries: what does *not* go through RAG

Knowing what to exclude is as much of the design as what to include.

| Question type | Handled by | Why not RAG |
|---|---|---|
| "Is checkout healthy right now?" | `query_prometheus` (live) | seconds-fresh; a cached answer is an incident |
| "What's payment's p99 latency this hour?" | `query_prometheus` (PromQL) | exact, always current, cheaper |
| "Show errors from checkout in the last hour" | `find_traces` (Jaeger) | traces are high-volume and ephemeral |
| "What does a circuit breaker opening mean?" | RAG | stable prose, months of validity |
| "How do I restart the checkout service?" | RAG | procedure, versioned, needs whole-document context |

Indexing logs is the most common mistake in this space: they are enormous, they change constantly, and the useful signal is aggregate rather than semantic. What *is* worth indexing is error signatures extracted from closed incidents — small, stable, and exactly the mapping from symptom to cause that RAG is good at.

## Observability

Three principles, each traceable to a class of incident that is otherwise undiagnosable:

- **Log chunk ids and scores, never content or raw queries.** Without ids, no bug report about a wrong answer is falsifiable — you cannot tell a retrieval failure from a generation failure. With content, you have copied sensitive documents into a telemetry store with a wider audience.
- **Latency per stage, not per request.** A p95 of four seconds is not actionable; four seconds with 3.6 in generation is.
- **Cost as a distribution, not a daily total.** Alerting on daily spend catches the problem a day late. Alerting on p99 cost per turn catches the pathological query while it is still one query.

## Failure model

Two categories, handled differently. Conflating them is the most common design mistake in agent systems.

**Infrastructure failures** — 429, 503, timeout, connection refused. Handled by code: retry with jittered backoff, trip a circuit breaker, degrade, alert. These raise.

**Semantic failures** — no results, access denied, malformed arguments. Handled by the model: returned as a `ToolResult` carrying an *instruction* about what to do next, because the reader is a language model deciding its next action, not a human reading a stack trace.

Raising on "no documents matched" kills a conversation the agent could have recovered from by rephrasing. Feeding a 503 back to the model as text makes it retry a dead service until the step cap trips.
