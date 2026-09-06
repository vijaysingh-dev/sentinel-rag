# Stage overview

One section per pipeline stage. For each: **what we used**, **why**, **what the alternatives were**, **what the result was**, and **what breaks in production** — at a level you can hold in your head.

The deep version of each stage — the reasoning behind every constant, the per-provider mechanics, the traces — lives in `docs/stages/<stage>.md`. This file is the map; those are the territory. Read this before an interview; read the stage file before working on that stage.

Every claim here is backed by code in `sentinel/` and by a runnable script in `scripts/`, so nothing in it is an assertion you cannot demonstrate. Result numbers marked _(illustrative)_ are placeholders showing the shape of the output — run the benchmark named in each section against your own corpus to replace them.

---

## Contents

| Stage | Detail | Code | Script | Benchmark |
|---|---|---|---|---|
| 1. Parsing | [`stages/parsing.md`](stages/parsing.md) | `parsing/` | `scripts/parsing/` | `benchmarks/parser_bakeoff.py` |
| 2. Chunking | _(pending)_ | `chunking/` | — | `benchmarks/chunking_sweep.py` |
| 3. Embedding | _(pending)_ | `embedding/` | `scripts/embedding/` | `benchmarks/embedding_bakeoff.py` |
| 4. Vector store | _(pending)_ | `vectorstore/` | `scripts/vectorstore/` | — |
| 5. Retrieval | _(pending)_ | `retrieval/` | `scripts/vectorstore/01_*` | `benchmarks/retrieval_sweep.py` |
| 6. Reranking | _(pending)_ | `reranking/` | `scripts/reranking/` | `benchmarks/retrieval_sweep.py` |
| 7. Generation | _(pending)_ | `generation/` | `scripts/llm/` | — |
| 8. Evaluation | _(pending)_ | `evaluation/` | `scripts/evaluation/` | all |
| 9. Agent layer | _(pending)_ | `rca/` | `make api` | `tests/test_trajectory.py` |

Each stage gets its own `docs/stages/<stage>.md` as it is worked through; the _(pending)_ rows have their summary here only, for now.

---

## 1. Parsing

**→ Full detail: [`stages/parsing.md`](stages/parsing.md)**

### What we used

A three-provider adapter behind one interface (`parsing/base.py`), with a hybrid routing policy (`parsing/hybrid.py`) on top.

| Provider | Library / service | Handles |
|---|---|---|
| local | pdfplumber, python-docx, python-pptx, openpyxl, BeautifulSoup | native PDF, DOCX, PPTX, XLSX, CSV, MD, HTML, TXT, JSON |
| azure | AI Document Intelligence, `prebuilt-layout` | scanned PDF, complex layout, borderless tables |
| aws | Textract, `TABLES` + `LAYOUT` | scanned PDF, images, ruled tables |

Every adapter returns the same `ParsedDocument` (`core/types.py`) — `blocks` in reading order, each keeping its structure (heading level, table grid, figure bbox). That uniformity is what makes the hybrid strategies and the cross-provider bake-off possible.

### Why

Parsing is stage one and it caps everything downstream — no later stage recovers a flattened table or interleaved columns. The economics force the hybrid design: most documents are handled perfectly by a free library, a minority badly; paying per page for all of them wastes money, using only local silently loses the minority. So parse locally, score the result with a reference-free heuristic (`quality.py`), and escalate only what falls below `parse_quality_threshold` (0.72).

Four strategies for different corpora: **`hybrid_escalate`** (default — local first, cloud on failure), `hybrid_merge` (both, best half of each), `race` (both, first acceptable wins — latency), `single` (baseline).

### The one thing to remember about the quality score

It is a calibrated heuristic, not a fitted model, tuned conservative (a false "good" poisons the index forever; a false "bad" costs one cloud call). Its constants come from the physics of a page — e.g. "a full page of prose ≈ 1500 chars" from ~500 words × ~6 chars, halved for technical layout. **Its blind spot:** structure is only 20% of the weight, so a parse with perfect characters and zero headings can clear the threshold and never escalate. The `suzuki-brezza.pdf` walkthrough shows exactly this — local scores 0.87 and is accepted, yet lost every heading and mangled the spec tables that only Textract+LAYOUT recovered. Full derivation of every constant in the stage file.

### Azure vs AWS

Contrary to a common guess, **Azure is the stronger OCR/layout engine** (scans, multi-column, borderless tables); it returns markdown ready to chunk. Textract returns a flat block graph you reassemble yourself and **requires an S3 bucket** for any multi-page document (`start_document_analysis` reads from S3). Textract's real advantages are AWS-ecosystem fit and the `FORMS` feature. Azure `prebuilt-layout` is also cheaper per page than Textract `analyze` ($0.010 vs $0.015). This is why `hybrid.py` escalates to Azure by default.

### Dev vs production: the `debug` switch

`settings.debug` (env `DEBUG`) forces DEBUG logging and caps Azure Document Intelligence at `azure_di_dev_page_limit` pages (default 2 — per-page billing, and F0 caps at 2 anyway; logged as `azure_page_limited`). `DEBUG=false` sends the whole document. Config validation refuses `parse_provider=aws` without `TEXTRACT_S3_BUCKET`.

### Tracing a parse

The adapters emit structured `DEBUG` events at every decision point; `scripts/parsing/_trace.py` folds them into a per-run trace. Each script writes `outputs/<stem>/<provider>.{trace.log, parsed.json, content.md}`; `04_hybrid_compare.py` adds `compare.trace.log` — the artefact that answers "how did you pick a parser?" with evidence.

### Result _(illustrative — run `make bench-parsers`)_

| provider | overall | text | tables | reading order | secs/doc | usd/doc |
|---|---|---|---|---|---|---|
| local | 0.71 | 0.94 | 0.52 | 0.88 | 0.4 | 0.0000 |
| azure | 0.89 | 0.96 | 0.91 | 0.95 | 3.1 | 0.0800 |
| aws | 0.85 | 0.95 | 0.86 | 0.91 | 4.7 | 0.1200 |
| hybrid_escalate | 0.86 | 0.95 | 0.85 | 0.93 | 1.2 | 0.0180 |

The last row is the point: within a few points of the best single provider at ~1/4 the cost, because 78% of the corpus never escalated. The interesting column is cost-per-quality-point, not quality.

### What breaks in production

- **Scanned pages with no text layer** — the text-layer probe routes them straight to cloud OCR.
- **A parse that scores "good enough" but lost its structure** — the trace shows it; the heuristic won't.
- **Azure F0 silently capping at 2 pages**; **`parse_provider=aws` with no S3 bucket** (both now caught explicitly).
- **Tables extracted twice** — every adapter excludes captured table regions from the text pass.
- **Running headers diluting every chunk**; **speaker notes dropped from decks**; **diagrams invisible to a text index** (`figures.py` describes them with a vision model).

---

## 2. Chunking

### What we used

Nine strategies behind one interface, selected **per document type** by a routing table rather than globally.

| doc type | strategy | why |
|---|---|---|
| runbook | `runbook` | embed each step, return the whole procedure |
| glossary | `glossary` | one term per chunk; entries are atomic |
| methodology, incident, factsheet, adr, page | `structural` | heading-aware with path prefix |

Available but not default: `fixed`, `recursive`, `sentence_window`, `parent_document`, `semantic`, `contextual`.

### Why

Chunking resolves a conflict between two consumers of the same text. The retriever wants small single-topic units, because a vector averaging five topics is close to nothing in embedding space. The generator wants enough context to answer — the definition, the setup, the table header, the antecedent of "it".

The resolution is the central design decision of the whole package: **`Chunk` carries two text fields.** `embed_text` is vectorised; `context_text` is handed to the LLM. They are usually the same and deliberately need not be. Sentence-window and parent-document strategies are only expressible because of that split — a single-text-field chunk model cannot represent them at all.

### Why `structural` is the default

The heading path prefix is doing most of the work, and it is usually worth more than upgrading the embedding model.

A chunk reading *"Set this to false to skip validation"* is unretrievable — no realistic query lands near it. The same chunk embedded as *"ICE Configuration > Validation > strict_mode — Set this to false to skip validation"* is findable by dozens of queries. That costs a handful of tokens.

### Why `runbook` exists as a separate strategy

A retrieved half-runbook is worse than no runbook, because someone under incident pressure will execute the half. So steps are embedded individually (precision — a query about one step finds that step) while `context_text` is always the complete procedure.

This is the clearest example of why chunking is chosen per document type rather than globally.

### The honest assessment of semantic chunking

It embeds every sentence, measures cosine distance between consecutive sentences, and splits at distance peaks. It is fashionable. It is also **expensive** — you embed the whole corpus once at ingest just to decide boundaries, then embed the chunks again — and in published comparisons it frequently fails to beat recursive splitting by enough to justify that.

Benchmark it before adopting it. The interesting column in the result is usually cost, not recall.

### Contextual chunking

Anthropic's technique: a cheap model writes one sentence situating each chunk in its document, prepended before embedding. It recovers most of what splitting destroys — pronouns, implicit subjects, "as described above".

What makes it affordable is prompt caching: the document sits in the static prefix, so after the first chunk the provider charges roughly a tenth for those tokens. Without caching it is impractical at corpus scale. Off by default (`CONTEXTUAL_CHUNKING=false`) because it costs one LLM call per chunk.

### Result _(illustrative — run `make bench-chunking`)_

| strategy | recall@5 | recall@50 | mrr | chunks/doc |
|---|---|---|---|---|
| structural_550_60 | 0.84 | 0.96 | 0.71 | 18 |
| parent_1800_250 | 0.82 | 0.95 | 0.69 | 41 |
| recursive_550_60 | 0.76 | 0.93 | 0.62 | 17 |
| sentence_window_3 | 0.79 | 0.97 | 0.65 | 96 |
| semantic_p92 | 0.77 | 0.94 | 0.63 | 21 |
| fixed_550_60 | 0.68 | 0.89 | 0.54 | 16 |

Read the `chunks/doc` column alongside recall. More chunks is not better: it inflates index size, increases embedding cost, and crowds the top-k with near-duplicates. Sentence-window buys the best recall@50 for five times the vectors — worth it only if you are recall-starved.

### What breaks in production

- **Tables split across boundaries** lose their header and become orphaned numbers. `structural` keeps tables whole.
- **Code fences split in half** retrieve on their identifiers and are actively misleading to anyone acting on them. The markdown parser captures fences as single blocks.
- **Parent-document without deduplication.** Five matching children of one parent each pull in the identical parent text and consume the entire context budget on one section. `retrieval/fusion.dedupe_by_parent` handles it; a pipeline that forgets is quietly wasting most of its prompt.
- **Silent truncation at embed time.** A 2000-token chunk sent to a 512-token model loses three quarters of its content with nothing in the logs. `embedding/base._prepare` truncates deliberately and counts it; a rising truncation count means chunks are sized wrongly for the model.

---

## 3. Embedding

### What we used

| Provider | Model | Dims | Notes |
|---|---|---|---|
| local | BAAI/bge-base-en-v1.5 | 768 | free, private, no rate limit |
| azure | text-embedding-3-large | 3072 → 256 | Matryoshka truncation via `dimensions` |
| aws | Titan v2 / Cohere Embed v3 | 1024/512/256 | two genuinely different APIs |

### Why the base class enforces query/document asymmetry

This is the silent quality killer, and it is the first thing to check when recall is inexplicably mediocre.

BGE and E5 were trained with an **instruction prefix on the query side only**. Cohere Embed v3 requires an explicit `input_type` — `search_document` when indexing, `search_query` when querying. Getting either wrong costs several points of recall and produces no error whatsoever.

So `BaseEmbedder` makes `embed_query` and `embed_documents` separate methods that an adapter cannot accidentally collapse. `scripts/embedding/01_embed_local.py` prints the similarity margin with and without the prefix so the cost is visible rather than theoretical.

### Matryoshka truncation (Azure)

`text-embedding-3-*` models are trained so the leading dimensions are usable alone. Passing `dimensions=512` to a 3072-dimension model returns a shorter vector retaining most of the quality.

This is a real lever: index size and query latency both scale with dimensionality, and storage is often the binding constraint before quality is. It enables a two-stage pattern — search on truncated vectors for a wide candidate set, rescore the top candidates with full-precision vectors.

Note that `ada-002` rejects the `dimensions` parameter outright, so the adapter checks `MATRYOSHKA_CAPABLE` before sending it.

### Titan vs Cohere on Bedrock

The difference changes how you write ingestion code:

**Titan has no batch endpoint.** One `InvokeModel` call per text. Throughput comes from client-side concurrency, so `BedrockEmbedder` fans out through a `ThreadPoolExecutor` and uses `executor.map` — not `as_completed` — because input order must be preserved for zipping vectors back to chunks.

**Cohere batches up to 96** and requires `input_type`.

Azure OpenAI accepts hundreds of inputs per request, so a naive port of Azure ingestion code to Titan will be slow in a way that looks like a network problem and is actually an API shape problem.

### Result _(illustrative — run `benchmarks/embedding_bakeoff.py`)_

| provider | model | dims | recall@5 | ndcg@5 | ms/1k chunks | usd/1M tokens |
|---|---|---|---|---|---|---|
| azure | text-embedding-3-large | 3072 | 0.87 | 0.81 | 2100 | 0.13 |
| azure | text-embedding-3-large | 1024 | 0.86 | 0.80 | 2000 | 0.13 |
| azure | text-embedding-3-large | 256 | 0.81 | 0.74 | 1900 | 0.13 |
| aws | titan-embed-text-v2 | 1024 | 0.84 | 0.78 | 8400 | 0.02 |
| local | bge-base-en-v1.5 | 768 | 0.83 | 0.77 | 12000 | 0.00 |

Two things worth noticing. Truncating 3072 → 1024 costs about a point of recall and saves two thirds of the index footprint — usually a good trade. And the local model is within four points of the best hosted one at zero marginal cost and zero data egress, which for some corpora is not a trade at all but a requirement.

### What breaks in production

- **Changing the embedding model without re-indexing.** The single most common production RAG incident, and it presents as a mysterious quality regression rather than an error. `settings.index_version` fingerprints chunking config, provider, model and dimensions; every chunk is stamped with it and every query filters on it.
- **Dimension mismatch between embedder and index.** Caught at write time if you are lucky, silently garbage retrieval if not.
- **Re-embedding unchanged text.** `EmbeddingCache` is keyed by model plus content hash, so an ingestion re-run over 4,000 unchanged pages costs nothing. Because the key is content-derived rather than position-derived, a chunker change that leaves most chunk text byte-identical still hits.

---

## 4. Vector store

### What we used

| Provider | Backend | Hybrid | Reranker |
|---|---|---|---|
| local | Postgres + pgvector | RRF in a single SQL statement | separate |
| azure | AI Search | native, RRF | native semantic ranker |
| aws | OpenSearch (Serverless or managed) | client-side RRF or server-side pipeline | separate |

### Why pgvector is the default

If Postgres is already in the stack you get transactions, joins to your metadata, row-level security for tenant isolation, and one fewer system to operate. It stays comfortable into the tens of millions of rows. "You need a dedicated vector database" is true later than most people assume.

Hybrid search needs no second system either: Postgres has full-text search built in, so `HYBRID_SQL` runs both arms as CTEs and fuses them with RRF in one statement. Fusion happens *before* truncation rather than after — the same advantage Azure's native hybrid gives you.

### Why Azure AI Search is compelling

Hybrid retrieval and a cross-encoder reranker are built in. One call does BM25, does vector search, fuses by rank, then re-scores the top candidates with the semantic ranker. Assembling that yourself means three components and a fusion implementation.

Two mechanics to be able to explain, because "the service does it" is not an answer:

- Passing both `search_text` and `vector_queries` is what makes the query hybrid. The service runs both arms and fuses by rank — the same RRF implemented by hand in `retrieval/fusion.py`.
- `query_type="semantic"` adds the reranker. It returns `@search.rerankerScore` on roughly a 0–4 scale, unrelated to the cosine score, so a relevance floor tuned for one cannot be reused for the other.

### Why OpenSearch is more work

OpenSearch gives you the primitives, not the pipeline. BM25 and k-NN are both first-class; fusing them is your job — either client-side, or via a server-side search pipeline with a normalization processor. Both are implemented; client-side RRF is the default because it behaves identically to the other backends and is therefore comparable in a benchmark.

Two AWS-specific traps:

- **SigV4 service name.** Serverless signs against `aoss`, managed domains against `es`. Getting it wrong produces a 403 that looks like a permissions problem and is really a signing problem.
- **Serverless rejects a client-supplied `_id`** on the bulk API. Managed accepts it, and supplying it is what makes re-ingestion idempotent rather than duplicating. The adapter branches on `self.serverless`.

### Security: the filter is inside the query

Every adapter translates `SearchFilter` into its backend's native filter language and applies it **as part of the search**, not afterwards. A post-filter has already loaded restricted content into your process, where a log line or an exception traceback can leak it.

| Backend | Mechanism |
|---|---|
| pgvector | `allowed_roles && %(roles)s` array overlap, GIN-indexed, in the WHERE clause |
| Azure | OData `allowed_roles/any(r: search.in(r, '...'))`, applied inside the ANN traversal |
| OpenSearch | `terms` clause in `bool.filter` — filter context, so it does not affect scoring and is cacheable |

`SearchFilter.require_scope()` raises on an empty role list rather than rendering an empty clause that matches everything. That is the difference between "this caller sees nothing" and "this caller sees the entire corpus", and it is asserted in `tests/test_acl.py`.

### What breaks in production

- **HNSW recall degradation** under heavy filtering. All three expose an `ef_search` dial; pgvector sets it per session, Azure and OpenSearch at index level.
- **Vectors shipped back over the wire.** The OpenSearch adapter excludes the `embedding` field from `_source` on every read; forgetting costs bandwidth proportional to `k × dims × 4` bytes per query.
- **Re-ingestion duplicating the corpus.** Content-derived chunk ids plus `merge_or_upload` / `ON CONFLICT DO UPDATE` make writes idempotent. `chunk_ids_for_doc` supports orphan tombstoning, without which the system confidently cites superseded runbooks.

---

## 5. Retrieval

### What we used

```
query
  → rewrite (multi-turn only, gated by a regex)
  → cache lookup (namespaced by role scope + index version)
  → hybrid search: BM25 + vector, fused by RRF        [k=50, recall-oriented]
  → cross-encoder rerank                               [n=6, precision-oriented]
  → relevance floor
  → dedupe by parent / content
  → optional MMR
  → assemble within a token budget
```

### Why two stages

Recall is stage one's job; precision is stage two's. They have different failure modes and different fixes, and conflating them means you cannot tell which to work on.

**Report recall@5 and recall@50 together.** The gap localises the failure:

- high recall@50, low recall@5 → ranking problem, a reranker fixes it
- low recall@50 → candidate generation problem, no reranker will help; fix retrieval, chunking or parsing

`scripts/evaluation/01_evaluate_stages.py` prints exactly this diagnosis.

### Why RRF rather than score blending

Two independent rankers cannot be combined by adding their scores: cosine similarity lives in [0,1] and BM25 is unbounded and corpus-dependent. Normalising per query is unstable because the score distribution shifts with the query.

RRF discards the scores and uses the ranks: each list contributes `weight / (k + rank)`, with `k=60` damping the influence of top positions so one ranker's confident-but-wrong first result cannot dominate. It is scale-free, needs no tuning, and is robust to one arm producing garbage.

Weighted fusion is implemented too, and can beat RRF *with a tuned alpha* — but the right alpha typically differs between natural-language and identifier-style queries, which is why untuned RRF usually wins in practice.

### Query transformation, in order of real-world payoff

1. **Conversational rewriting.** "What about the second one?" has no standalone meaning; embedding it retrieves noise. Any multi-turn RAG system without rewriting is broken from turn two onward, and it is the most common unreported bug in deployed assistants. Gated by a cheap regex (`DEPENDENT`) so the majority of already-standalone queries do not pay for an LLM call.
2. **Decomposition** for multi-hop questions. "How does divisor adjustment differ between special dividends and spin-offs?" is two lookups and a comparison.
3. **Multi-query** paraphrasing for vocabulary mismatch between askers and authors. Costs n extra retrievals; belongs on high-value paths.
4. **HyDE** — embed a fabricated answer rather than the question, since an answer sits closer to real passages than a question does. Helps on jargon-heavy queries, hurts when the model hallucinates the wrong domain. Opt-in.

### Degradation ladder

`full` → `dense_only` → `bm25_only`. Lexical-only is a genuinely useful degraded mode, not a token gesture: exact-match queries (error codes, tickers, version numbers) are the ones it handles *best*, so a meaningful fraction of traffic is unaffected by an embedding outage.

Every degradation increments `DEGRADED_MODE`, which is what catches a silent multi-day drift into a fallback path that nobody noticed because nothing errored.

### Caching: knowledge yes, telemetry never

One conversation mixes both. "What does a stale feed mean?" is stable for months. "Is the SGX feed healthy?" is stable for seconds. Caching the second turns the assistant into an incident: an engineer asks whether a feed recovered, receives a ten-minute-old "healthy", and stops investigating.

So cacheability is decided per **tool**, not per request — `core/cache.LIVE_TOOLS` plus `is_cacheable()`. Three layers, all keyed by role scope and index version:

| Layer | Key | TTL |
|---|---|---|
| embedding | model + content hash | never expires |
| retrieval | index_version + role scope + filter fingerprint + query hash | 15 min |
| semantic answer | role scope namespace, cosine ≥ 0.95 | 1 hour |

Omitting the role scope means one caller receives another caller's answer — not a performance bug but a data leak. Omitting the index version serves stale chunks after a re-index.

The semantic answer cache threshold deserves calibration before you trust it: "how do I enable delivery" and "how do I disable delivery" are close neighbours and opposite questions.

### Result _(illustrative — run `make bench-retrieval`)_

| configuration | recall@5 | recall@50 | mrr | p50 ms |
|---|---|---|---|---|
| hybrid + rerank, k=50 | 0.87 | 0.96 | 0.78 | 210 |
| hybrid + rerank, k=25 | 0.85 | 0.93 | 0.76 | 160 |
| hybrid, no rerank | 0.71 | 0.96 | 0.58 | 95 |
| dense only + rerank | 0.79 | 0.91 | 0.70 | 190 |
| lexical only + rerank | 0.64 | 0.82 | 0.55 | 120 |

Reranking is worth **+0.16 recall@5** here for about 115ms — the largest single lever in the pipeline. Note that recall@50 is identical with and without it, which is the point: the reranker reorders, it does not retrieve.

The knee in candidate depth is around k=50; beyond it, extra candidates cost reranking latency and buy nothing.

---

## 6. Reranking

### What we used

| Provider | Model | Score scale | Floor |
|---|---|---|---|
| local | cross-encoder/ms-marco-MiniLM-L-6-v2 | unbounded logits | −2.0 |
| azure | AI Search semantic ranker | ~0–4 | 1.4 |
| aws | `amazon.rerank-v1:0` via bedrock-agent-runtime | normalised [0,1] | 0.02 |

### Why: bi-encoder vs cross-encoder

This is the distinction that justifies the stage, and the one interviewers probe.

A **bi-encoder** — your embedding model — encodes query and document *separately* into vectors and compares them. Documents can be embedded offline, so it scales to millions. But the model never sees the query and the document together.

A **cross-encoder** takes the pair as one concatenated input and outputs a relevance score. Attention runs across both, so it catches negation, qualifiers and subtle mismatch that cosine similarity misses. It cannot be precomputed, so it only runs over a small candidate set.

Hence two-stage retrieval: cheap wide arm for recall, expensive narrow arm for precision.

**The corollary matters as much as the technique: a reranker cannot recover a recall miss.** If the answer is not in the candidate pool, no amount of reordering surfaces it.

### Why the relevance floor is not optional

The floor is what lets the system say "I don't have anything relevant" instead of answering from the five least-bad chunks it found. In an operations assistant that is the difference between a useful refusal and someone running the wrong runbook.

Floors are **per reranker and never portable**, because the score scales are unrelated. Copying a floor from a local cross-encoder to Bedrock would reject everything; the reverse would accept everything.

### Provider-specific mechanics

**Azure is native.** The semantic ranker runs inside the search request — nothing to host, nothing to batch, no extra round trip. The adapter's `rerank()` is usually a no-op reorder of scores that already arrived. A standalone mode exists for candidates from elsewhere, at the cost of an extra query.

**AWS lives on a different client.** `bedrock-agent-runtime.rerank`, not `bedrock-runtime` — a common source of "method not found". Sources are capped at 100 per call, so a 200-candidate pool is three calls.

**Local is often the best value.** A MiniLM cross-encoder scores ~50 pairs in 50–150ms on CPU and single-digit milliseconds on GPU, at zero marginal cost and with no data leaving the machine. If you can make only one change to a RAG system, make it this one.

### What breaks in production

- **Reranking a pool of five.** The reranker needs room to reorder; `k` must be meaningfully wider than `top_n`.
- **Reranking the wrong text.** `BaseReranker` scores `context_text` — what the generator will see — not `embed_text`.
- **Latency budget.** Reranking is the second-largest latency contributor after generation. `arerank` exists so it can overlap other work.

---

## 7. Generation

### What we used

Every adapter exposes three handles, because a production pipeline never uses one model:

| Handle | Purpose | Share of calls |
|---|---|---|
| `chat` | final answers, verification, specialist reasoning | ~30% |
| `cheap` | routing, query rewriting, extraction, contextualisation | ~65% |
| `vision` | figure description at ingest | ~5% |

| Provider | chat | cheap |
|---|---|---|
| local | Ollama/vLLM via OpenAI-compatible endpoint | smaller local model |
| azure | AzureChatOpenAI (gpt-4o) | gpt-4o-mini |
| aws | ChatBedrockConverse (Claude Sonnet) | Claude Haiku |

### Why these settings are uniform

- **`temperature=0`** — everything here is extraction, routing or grounded answering. None of it is creative writing.
- **`max_retries=0`** — retry policy lives in `core/resilience.py`. Leaving the SDK's own retries on as well produces compounding backoff and, under load, a retry storm.
- **`ChatBedrockConverse` rather than `ChatBedrock`** — the Converse API normalises tool calling and multimodal input across model families. Without it, Anthropic, Meta and Amazon models each want a different request shape and the agent code would need to know which is which.

### The cheap/strong split is the main cost lever

Routing runs on every supervisor loop and is classification, not analysis — so it is the highest-volume call in the system and the obvious place to economise. Verification runs once and is the last automated check before a human, so using the cheap model there is a false economy: a weak verifier approves exactly the confident-wrong drafts it exists to catch.

### Local model caveat

Small local models are noticeably weaker at **tool calling** and structured output than hosted frontier models. The graph will run, but expect more schema-repair rounds and worse routing accuracy. `scripts/llm/02_structured_and_tools.py` measures both on your own schemas — knowing the size of that gap is the difference between a considered choice and an assumption.

### What breaks in production

- **Ungrounded answers.** `scripts/llm/01_generate_grounded.py --no-context` runs the identical prompt with the context stripped, which is the clearest possible demonstration of why RAG exists: the ungrounded answer is fluent, plausible, specific and wrong.
- **Fabricated citations.** `citation_validity` is a pure string check — no LLM needed — comparing cited ids against what was actually retrieved. One of the cheapest useful metrics in the suite.
- **Structured output failures.** `structured_with_repair` feeds the validation error back once, which fixes the large majority of malformed outputs for one cheap call.

---

## 8. Evaluation

### What we used

Four layers, split by cost and determinism.

| Layer | Metrics | LLM needed | Runs |
|---|---|---|---|
| parsing | text similarity, table F1, Kendall's tau | no | on corpus change |
| retrieval | recall@k, precision@k, MRR, nDCG | no | every PR |
| generation | faithfulness, relevance, citations, refusal | yes | nightly |
| agent | outcome, trajectory, safety | yes (fixtures) | every PR |

That split is the practical answer to putting a non-deterministic system in CI: gate every pull request on the cheap deterministic layer, run the expensive layer nightly.

### The golden set is the highest-leverage artifact

Without one, every change is a vibe check and "it seems better" is not something you can put in a pull request.

Rules that make one useful rather than decorative:

- **Every category represented**, especially the negative ones. `TARGET_MIX` in `evaluation/golden.py` enforces the balance, so a set that has quietly drifted into all-easy-questions is caught by `check_coverage`.
- **Unanswerable questions are first-class.** The correct behaviour is refusal. A system scoring well only because it always answers is not good, it is untested.
- **ACL cases are first-class**, and the correct behaviour is not just "no answer" but "no acknowledgement that the document exists".
- **Seed by hand, grow from production.** Twenty hand-written examples beat two hundred synthetic ones. `generate_from_chunks` exists for the cold start and labels its output `synthetic: true`, because questions generated *from* the answer are systematically easier than real ones — a system can score 0.95 on them and still fail users.

### Faithfulness via atomic claim decomposition

The metric that matters most in an operations context: is every claim supported by retrieved context? A fluent, well-cited, confidently wrong answer is what gets someone to run the wrong runbook.

Decomposition rather than whole-answer judging is what makes it robust. A whole-answer verdict collapses "eight right facts and one fabricated one" into a single fuzzy score; decomposition surfaces the one fabrication, which is the thing you needed to know.

### Known judge biases, and what is done about each

| Bias | Mitigation |
|---|---|
| position — favours whichever answer is seen first | score one answer at a time against explicit criteria, never pairwise |
| verbosity — longer scores higher regardless of correctness | decompose into atomic claims, so length stops being a proxy |
| self-preference — a model rates its own output higher | judge with a different model family from the generator |
| rationale-after-score — a judge asked for a score first rationalises it | every schema puts `rationale` **before** the score field |

That last one is a small change with a measurable effect on agreement with human labels, and it appears throughout this codebase — `RouteDecision`, `VerificationResult`, `AnswerJudgement`, `ClaimVerdict` all order their fields that way deliberately.

### The gate

Two tiers, because they answer different questions:

- **Absolute thresholds** — "is this good enough to ship at all?" Cheap to reason about, but they drift downward under deadline pressure.
- **Regression thresholds** — "is this worse than what is already in production?" This is the one that catches real problems, because it compares against `benchmarks/baseline.json`, committed to git, rather than a number someone chose once. Tolerance is 0.03: large enough to absorb judge noise on a modest golden set, small enough to catch real degradation.

**Safety checks are never averaged.** A 98% pass rate on "did not leak restricted content" is a failing grade. Any violation fails the run outright.

Confidence intervals are reported via bootstrap, because with a 40-example golden set a two-point improvement is usually noise — and reporting a delta without an interval is how teams talk themselves into shipping regressions.

### Online evaluation

A sampled slice of production runs is judged asynchronously and the score attached to the trace, so quality is a time series rather than something someone remembers to check.

The strongest signal is free: the analyst's approve / edit / reject decision on every RCA is a human label already, which is why the evaluation layer does not depend solely on LLM judges.

---

## 9. Agent layer

### What we used

A LangGraph supervisor dispatching three specialists with **disjoint tool scopes**, an adversarial verifier, and a human-in-the-loop writer.

```
intake → supervisor ⇄ {knowledge, data, change}
                   ↘ verifier ⇄ supervisor
                   ↘ writer → approval → publish
```

### Why multi-agent rather than one agent with eleven tools

Three reasons, in order of how much they matter:

1. **Tool-selection accuracy degrades as the tool count grows.** Three tools chosen from three beats three chosen from eleven.
2. **Isolation is structural, not instructed.** The knowledge specialist cannot execute SQL because it does not hold that tool — not because a prompt told it not to. `tests/test_trajectory.py` asserts the supervisor references no specialist tools at all, because a constraint stated only in a prompt survives until the next person edits that prompt.
3. **Context stays small.** Each specialist's context holds only its own tool output.

The decision that makes the split pay for itself: **specialists return structured evidence to shared state, but only a short prose summary to the supervisor's message list.** Without that, the orchestrator drowns in the same tool output the specialists were meant to absorb, and you have paid for extra hops to get a worse single-agent system.

### Why the state reducers matter

Knowledge, data and change are dispatched concurrently. Without `Annotated[list, add]` merge functions, their simultaneous writes to the same state key raise a concurrent-update error rather than accumulating. That one annotation is what makes parallel dispatch possible at all.

### Grounding is mechanical, not judged

`observed_refs()` scans raw `ToolMessage` content for citation handles that actually appeared. Any claim citing a ref outside that set is **dropped** before it becomes evidence. This turns "did the model make this up?" from a judgement call into a set membership test.

### Why `interrupt()` rather than a polling loop

When the writer reaches `interrupt()`, LangGraph persists the entire graph state to the checkpointer and returns control. No worker, no thread and no memory is held while a human takes four hours to look at it; the run resumes from the checkpoint when a decision arrives, on a different process if necessary.

That is what makes human-in-the-loop viable in production rather than a demo. Polling loops and held connections do not survive a deploy, a pod eviction, or an analyst going to lunch.

Publication is the only write in the system and it sits behind that interrupt. `publish_node` re-asserts `approved_by` as well, so a graph edit that accidentally routed around the interrupt fails loudly rather than publishing silently.

### Degrade, never fail

A dead warehouse produces a partial RCA marked degraded with capped confidence, not a 500. `DEGRADED_CONFIDENCE_CAP = 0.6` is enforced in code after the model returns, not left to the prompt — a degraded run cannot present itself as confident no matter what the model wrote.

### SQL safety in four layers

Generated SQL against a production warehouse is the highest-risk tool in the system:

1. **Parse, do not regex.** `sqlglot` builds an AST and checks the statement type on the tree. A regex looking for "DELETE" is defeated by comments, casing and string literals.
2. **Single statement only.** Stacked queries are the classic injection vector.
3. **Forced row limit.** An unbounded SELECT on a large fact table is a self-inflicted outage.
4. **A database-level read-only role.** The one that actually matters — see `config/init-readonly-role.sql`. The first three are defence in depth; this is the guarantee.

### Result _(illustrative)_

| metric | value | gate |
|---|---|---|
| outcome accuracy | 0.82 | — |
| route accuracy | 0.91 | ≥ 0.80 |
| tool precision | 0.88 | — |
| mean steps | 5.2 | ≤ 12 |
| repeated calls | 0 | 0 |
| safety violations | 0 | 0 (hard) |
| mean usd/run | 0.34 | ≤ 2.50 |

Fixtures replay recorded tool output, so the suite tests reasoning rather than infrastructure uptime — which is what makes it cheap enough to run on every pull request.

---

## Cross-cutting: what changes between clouds

| Concern | Azure | AWS | Local |
|---|---|---|---|
| Parsing output | markdown, ready to chunk | flat block graph, you reassemble | native structure |
| Multi-page parsing | inline bytes | S3 + job polling required | inline |
| Embedding batching | hundreds per request | Titan: one per call; Cohere: 96 | unlimited |
| Dimension reduction | Matryoshka `dimensions` | Titan `dimensions` | fixed |
| Hybrid search | native, one call | client-side or search pipeline | one SQL statement |
| Reranking | inside the search request | separate client, 100/call | in-process |
| Auth | managed identity via `DefaultAzureCredential` | SigV4, `aoss` vs `es` | none |
| Filter language | OData | bool/terms JSON | SQL |

The adapters exist so none of this leaks into the agent code. `rca/tools/retrieval.py` calls `RetrievalPipeline` and has no idea which cloud answered.

---

## Reading order for someone new to the codebase

1. `docs/architecture.md` — the shape
2. `sentinel/core/types.py` — the contract between stages, especially the two-field `Chunk`
3. `sentinel/core/registry.py` — how the adapter pattern is wired
4. `sentinel/parsing/hybrid.py` — the most interesting policy decision in the pipeline
5. `sentinel/retrieval/pipeline.py` — how the stages compose
6. `sentinel/rca/graph.py` — the agent topology in one screen
7. `docs/design-decisions.md` — the arguments, ADR by ADR
