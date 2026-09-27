# Stage overview

One section per pipeline stage. For each: **what we used**, **why**, **what the alternatives were**, **what the result was**, and **what breaks in production** — at a level you can hold in your head.

The deep version of each stage — the reasoning behind every constant, the per-provider mechanics, the traces — lives in `docs/stages/<stage>.md`. This file is the map; those are the territory. Read this before an interview; read the stage file before working on that stage.

Every claim here is backed by code in `sentinel/` and by a runnable script in `scripts/`, so nothing in it is an assertion you cannot demonstrate. Result numbers marked _(illustrative)_ are placeholders showing the shape of the output — run the benchmark named in each section against your own corpus to replace them.

---

## Contents

| Stage | Detail | Code | Script | Benchmark |
|---|---|---|---|---|
| 1. Parsing | [`stages/parsing.md`](stages/parsing.md) | `parsing/` | `scripts/parsing/` | `benchmarks/parser_bakeoff.py` |
| 2. Chunking | [`stages/chunking.md`](stages/chunking.md) | `chunking/` | — | `benchmarks/chunking_sweep.py` |
| 3. Embedding | [`stages/embedding.md`](stages/embedding.md) | `embedding/` | `scripts/embedding/` | `benchmarks/embedding_bakeoff.py` |
| 4. Vector store | [`stages/vectorstore.md`](stages/vectorstore.md) | `vectorstore/` | `scripts/vectorstore/` | — |
| 5. Retrieval | [`stages/retrieval.md`](stages/retrieval.md) | `retrieval/` | `scripts/vectorstore/01_*` | `benchmarks/retrieval_sweep.py` |
| 6. Reranking | [`stages/reranking.md`](stages/reranking.md) | `reranking/` | `scripts/reranking/` | `benchmarks/retrieval_sweep.py` |
| 7. Generation | [`stages/generation.md`](stages/generation.md) | `generation/` | `scripts/llm/` | — |
| 8. Evaluation | [`stages/evaluation.md`](stages/evaluation.md) | `evaluation/` | `scripts/evaluation/` | all |
| 9. Agent layer | [`stages/agent.md`](stages/agent.md) | `rca/` | `make chat-server` | `tests/test_trajectory.py` |

Every stage now has its own `docs/stages/<stage>.md`.

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

The adapters emit structured `DEBUG` events at every decision point; `scripts/parsing/_trace.py` folds them into a per-run trace. Each script writes `outputs/parsing/<stem>/<provider>.{trace.log, parsed.json, content.md}`; `04_hybrid_compare.py` adds `compare.trace.log` — the artefact that answers "how did you pick a parser?" with evidence.

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

**→ Full detail: [`stages/chunking.md`](stages/chunking.md)**

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

The resolution is the central design decision of the whole package: **`Chunk` carries two text fields.** `embed_text` is vectorised; `context_text` is handed to the LLM. They are usually the same and deliberately need not be. Sentence-window, parent-document and contextual strategies are only expressible because of that split — a single-text-field chunk model cannot represent them at all.

### The three things to remember

- **`structural` is the default because of the heading-path prefix.** *"Set this to false to skip validation"* is unretrievable; *"Checkout Configuration > Validation > strict_mode — Set this to false to skip validation"* is findable by dozens of queries. That prefix costs a handful of tokens and usually buys more recall than upgrading the embedding model.
- **`runbook` is a separate strategy because a retrieved half-runbook is worse than none** — someone under incident pressure executes the half. Steps are embedded individually for precision; `context_text` is always the complete procedure. The clearest case for routing chunking per doc type.
- **`semantic` and `contextual` both cost an extra embed/LLM pass.** `semantic` embeds the corpus once just to pick boundaries and rarely beats `recursive` by enough to justify it — benchmark the cost column, not the recall column. `contextual` (Anthropic's technique — a cheap model writes one situating sentence per chunk) is only affordable via prompt caching and is off by default (`CONTEXTUAL_CHUNKING=false`).

Every size decision is measured in tokens via one consistent ruler (`ntokens`, tiktoken `cl100k_base` even for Claude/Titan — a benchmark needs consistency more than exactness), degrading to a ~4-chars/token approximation, logged once, when the encoding file cannot be fetched. Full derivation of the recursion algorithm, the regexes and every constant in the stage file.

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

**→ Full detail: [`stages/embedding.md`](stages/embedding.md)**

### What we used

| Provider | Model | Dims | Notes |
|---|---|---|---|
| local (default) | BAAI/bge-base-en-v1.5 | 768 | free, private, no rate limit |
| openai | text-embedding-3-small / -large | 1536 / 3072 → 256 | Matryoshka truncation via `dimensions` on the `-3-*` family |

Embedding is local-first infrastructure, not a cloud-account choice (ADR-029, `docs/design-decisions.md`) — `openai` is one paid escape hatch for when local quality isn't enough, not a maintained multi-cloud matrix. Azure OpenAI and Bedrock (Titan/Cohere) embedding adapters are archived at `.archive/providers/embedding/`.

### Why the base class enforces query/document asymmetry

This is the silent quality killer, and it is the first thing to check when recall is inexplicably mediocre.

BGE and E5 were trained with an **instruction prefix on the query side only**. Getting it wrong costs several points of recall and produces no error whatsoever.

So `BaseEmbedder` makes `embed_query` and `embed_documents` separate methods that an adapter cannot accidentally collapse. `scripts/embedding/01_embed_local.py` prints the similarity margin with and without the prefix so the cost is visible rather than theoretical.

### Matryoshka truncation (OpenAI)

`text-embedding-3-*` models are trained so the leading dimensions are usable alone. Passing `dimensions=512` to a 3072-dimension model returns a shorter vector retaining most of the quality.

This is a real lever: index size and query latency both scale with dimensionality, and storage is often the binding constraint before quality is. It enables a two-stage pattern — search on truncated vectors for a wide candidate set, rescore the top candidates with full-precision vectors. `scripts/embedding/02_embed_openai.py` measures the margin at each truncation on real text.

Note that `ada-002`-style models reject the `dimensions` parameter outright, so the adapter checks `MATRYOSHKA_CAPABLE` before sending it.

### Result _(illustrative — run `benchmarks/embedding_bakeoff.py`)_

| provider | model | dims | recall@5 | ndcg@5 | ms/1k chunks | usd/1M tokens |
|---|---|---|---|---|---|---|
| openai | text-embedding-3-large | 3072 | 0.87 | 0.81 | 2100 | 0.13 |
| openai | text-embedding-3-large | 1024 | 0.86 | 0.80 | 2000 | 0.13 |
| openai | text-embedding-3-large | 256 | 0.81 | 0.74 | 1900 | 0.13 |
| openai | text-embedding-3-small | 1536 | 0.84 | 0.78 | 1800 | 0.02 |
| local | bge-base-en-v1.5 | 768 | 0.83 | 0.77 | 12000 | 0.00 |

Two things worth noticing. Truncating 3072 → 1024 costs about a point of recall and saves two thirds of the index footprint — usually a good trade. And the local model is within four points of the best hosted one at zero marginal cost and zero data egress, which for some corpora is not a trade at all but a requirement — and is the reason this repo defaults every embedding call to it.

### What breaks in production

- **Changing the embedding model without re-indexing.** The single most common production RAG incident, and it presents as a mysterious quality regression rather than an error. `settings.index_version` fingerprints chunking config, provider, model and dimensions; every chunk is stamped with it and every query filters on it.
- **Dimension mismatch between embedder and index.** Caught at write time if you are lucky, silently garbage retrieval if not.
- **Re-embedding unchanged text.** `EmbeddingCache` is keyed by model plus content hash, so an ingestion re-run over 4,000 unchanged pages costs nothing. Because the key is content-derived rather than position-derived, a chunker change that leaves most chunk text byte-identical still hits.

---

## 4. Vector store

**→ Full detail: [`stages/vectorstore.md`](stages/vectorstore.md)**

### What we used

| Provider | Backend | Hybrid |
|---|---|---|
| local (only provider) | Postgres + pgvector | RRF in a single SQL statement |

pgvector is the only maintained vector store (ADR-029) — the same local-first reasoning as embedding and reranking applies: no per-query cost, comfortable into the tens of millions of rows, one fewer system to operate. Azure AI Search and OpenSearch adapters are archived at `.archive/providers/vectorstore/`, with their native-hybrid and SigV4/serverless mechanics preserved there as reference.

### Why pgvector is the default

If Postgres is already in the stack you get transactions, joins to your metadata, row-level security for tenant isolation, and one fewer system to operate. It stays comfortable into the tens of millions of rows. "You need a dedicated vector database" is true later than most people assume.

Hybrid search needs no second system either: Postgres has full-text search built in, so `HYBRID_SQL` runs both arms as CTEs and fuses them with RRF in one statement. Fusion happens *before* truncation rather than after — the same advantage a managed hybrid API gives you, without needing one.

### Security: the filter is inside the query

`PgVectorStore` translates `SearchFilter` into `allowed_roles && %(roles)s` — a GIN-indexed Postgres array-overlap operator — and applies it **as part of the search**, not afterwards. A post-filter has already loaded restricted content into your process, where a log line or an exception traceback can leak it.

`SearchFilter.require_scope()` raises on an empty role list rather than rendering an empty clause that matches everything. That is the difference between "this caller sees nothing" and "this caller sees the entire corpus", and it is asserted in `tests/test_acl.py`.

### What breaks in production

- **HNSW recall degradation** under heavy filtering. pgvector exposes `hnsw.ef_search` as a per-session dial for this.
- **Re-ingestion duplicating the corpus.** Content-derived chunk ids plus `ON CONFLICT DO UPDATE` make writes idempotent. `chunk_ids_for_doc` supports orphan tombstoning, without which the system confidently cites superseded runbooks.

---

## 5. Retrieval

**→ Full detail: [`stages/retrieval.md`](stages/retrieval.md)**

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
2. **Decomposition** for multi-hop questions. "How does the checkout service's retry policy differ from the payment service's?" is two lookups and a comparison.
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

**→ Full detail: [`stages/reranking.md`](stages/reranking.md)**

### What we used

| Provider | Model | Score scale | Floor |
|---|---|---|---|
| local (default) | cross-encoder/ms-marco-MiniLM-L-6-v2 | unbounded logits | −2.0 |
| cohere | rerank-v3.5 | normalised [0,1] | 0.02 |

Reranking is local-first infrastructure like embedding (ADR-029): `cohere` is one paid escape hatch, not a maintained multi-cloud matrix. Azure AI Search's native semantic ranker and Bedrock Rerank are archived at `.archive/providers/reranking/`.

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

**Cohere is a network call per query, not just per ingestion run.** Unlike the local cross-encoder, `CohereReranker` adds a real round trip to every retrieval. That is the actual trade against its quality, separate from the per-request price.

**Local is often the best value.** A MiniLM cross-encoder scores ~50 pairs in 50–150ms on CPU and single-digit milliseconds on GPU, at zero marginal cost and with no data leaving the machine. If you can make only one change to a RAG system, make it this one — which is why it's the default here.

### What breaks in production

- **Reranking a pool of five.** The reranker needs room to reorder; `k` must be meaningfully wider than `top_n`.
- **Reranking the wrong text.** `BaseReranker` scores `context_text` — what the generator will see — not `embed_text`.
- **Latency budget.** Reranking is the second-largest latency contributor after generation. `arerank` exists so it can overlap other work.

---

## 7. Generation

**→ Full detail: [`stages/generation.md`](stages/generation.md)**

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
| openai | gpt-4o-mini | gpt-4o-mini |
| anthropic | claude-sonnet-5 | claude-haiku-4-5 |
| google | gemini-2.5-pro | gemini-2.5-flash |

Generation is its own vendor axis, not a cloud-account choice (ADR-029): `llm_provider` is independent of `parse_provider`, `embed_provider` and `rerank_provider` — there's no infrastructure reason to keep them aligned, so pick whichever vendor's answers you trust this week. Azure OpenAI (`AzureChatOpenAI`) and Bedrock Converse (`ChatBedrockConverse`) adapters are archived at `.archive/providers/generation/`.

### Why these settings are uniform

- **`temperature=0`** — everything here is extraction, routing or grounded answering. None of it is creative writing.
- **`max_retries=0`** — retry policy lives in `core/resilience.py`. Leaving the SDK's own retries on as well produces compounding backoff and, under load, a retry storm.
- **Google uses the plain Gemini API key, not Vertex AI.** `ChatGoogleGenerativeAI` with `google_api_key=` — a materially different auth/billing setup than a GCP service account, and deliberately the simpler one here.

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

**→ Full detail: [`stages/evaluation.md`](stages/evaluation.md)**

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

**→ Full detail: [`stages/agent.md`](stages/agent.md)**

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

A dead Prometheus produces a partial RCA marked degraded with capped confidence, not a 500. `DEGRADED_CONFIDENCE_CAP = 0.6` is enforced in code after the model returns, not left to the prompt — a degraded run cannot present itself as confident no matter what the model wrote.

### PromQL safety in four layers

A query the model composes and runs against a production observability stack is the highest-risk tool in the system:

1. **Metric-name allowlist.** PromQL has no parser in the standard library, so instead of building an AST the guard extracts every identifier that looks like a metric name and requires each to be one the platform actually exposes (`ALLOWED_METRIC_PREFIXES`). An expression naming an unknown metric is rejected unrun — an allowlist of good names, not a regex looking for bad keywords.
2. **Forbidden functions.** A short list of PromQL functions that are expensive or exfiltration-shaped (`label_replace` rewriting series, subqueries nesting range selectors) is rejected.
3. **Forced, bounded time range.** A range query with no window, or a window wider than `settings.promql_max_range_hours`, is rejected. An unbounded range query is a self-inflicted outage on a busy Prometheus.
4. **A read-only Prometheus HTTP endpoint.** The one that actually guarantees it. Prometheus' query API has no write path by construction — the first three layers are defence in depth, this is the guarantee.

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

## Cross-cutting: what's still multi-cloud, and what isn't (ADR-029)

Only **parsing** stays a genuine local/azure/aws matrix — Azure Document Intelligence and AWS Textract each do something a local library cannot (layout models, OCR on scans):

| Concern | Azure | AWS | Local |
|---|---|---|---|
| Parsing output | markdown, ready to chunk | flat block graph, you reassemble | native structure |
| Multi-page parsing | inline bytes | S3 + job polling required | inline |
| Auth | managed identity via `DefaultAzureCredential` | instance role / `AWS_PROFILE` | none |

Embedding, vector storage and reranking are local-first infrastructure, each with at most one paid escape hatch, not a cloud matrix:

| Stage | Default | Escape hatch |
|---|---|---|
| embedding | local (sentence-transformers) | `openai` |
| vector store | local (pgvector) | none |
| reranking | local (cross-encoder) | `cohere` |

Generation picks a model vendor directly — `local` / `openai` / `anthropic` / `google` — independent of every other stage's provider; there is no "which cloud account" question to answer for it.

The adapter pattern exists so none of this leaks into the agent code regardless of which side of the split a stage is on. `rca/tools/retrieval.py` calls `RetrievalPipeline` and has no idea which provider answered. The Azure AI Search / OpenSearch / Bedrock mechanics this table used to compare (SigV4, OData vs. bool/terms filters, Titan's one-call-per-text limit) are preserved in `.archive/providers/`'s source, not lost, just no longer live choices in this repo.

---

## Reading order for someone new to the codebase

1. `docs/architecture.md` — the shape
2. `sentinel/core/types.py` — the contract between stages, especially the two-field `Chunk`
3. `sentinel/core/registry.py` — how the adapter pattern is wired
4. `sentinel/parsing/hybrid.py` — the most interesting policy decision in the pipeline
5. `sentinel/retrieval/pipeline.py` — how the stages compose
6. `sentinel/rca/graph.py` — the agent topology in one screen
7. `docs/design-decisions.md` — the arguments, ADR by ADR
