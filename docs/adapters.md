# Provider adapter matrix

Every stage has one interface and three implementations. This is the reference for what each provider actually does, what it costs, and where they differ in ways that change your code rather than just your configuration.

## At a glance

| Stage | Interface | local | azure | aws |
|---|---|---|---|---|
| parsing | `BaseParser` | pdfplumber, python-docx, python-pptx, openpyxl, bs4 | AI Document Intelligence | Textract |
| embedding | `BaseEmbedder` | sentence-transformers | Azure OpenAI | Bedrock (Titan / Cohere) |
| vector store | `BaseVectorStore` | Postgres + pgvector | AI Search | OpenSearch |
| reranking | `BaseReranker` | sentence-transformers CrossEncoder | AI Search semantic ranker | Bedrock Rerank |
| generation | `BaseLLM` | Ollama / vLLM | Azure OpenAI | Bedrock Converse |

Select per stage in `.env`:

```bash
PARSE_PROVIDER=azure
EMBED_PROVIDER=aws
VECTORSTORE_PROVIDER=aws
RERANK_PROVIDER=local
LLM_PROVIDER=aws
```

That combination is legal and not unreasonable: Azure's layout model is the strongest parser, the AWS account already exists for everything else, and a local cross-encoder costs nothing per query.

---

## Parsing

| | local | azure | aws |
|---|---|---|---|
| Native PDF | yes | yes | yes |
| Scanned PDF | no (empty text) | yes | yes |
| DOCX / PPTX / XLSX | yes | yes | no |
| CSV / MD / HTML / TXT | yes | partial | no |
| Images | no | yes | yes |
| Output shape | structured blocks | **markdown** | flat block graph |
| Tables | pdfplumber ruling lines | native, cell indices | CELL blocks, row/col indices |
| Reading order | document order | native | LAYOUT elements or vertical-gap heuristic |
| Multi-page | inline | inline | **S3 + job polling required** |
| Cost | free | ~$0.010/page (layout) | ~$0.015/page (TABLES+LAYOUT) |

### Differences that change your code

**Azure returns markdown.** `output_content_format=MARKDOWN` means headings arrive as `##` and tables as grids. The structural chunker consumes it directly.

**AWS returns a graph you assemble.** Every LINE, WORD, CELL and LAYOUT element is a node with `Relationships` pointing at children by Id. Most of `aws_parser.py` is reconstruction: `_blocks_from_layout` for reading order, `_table_blocks` for grids, `_ids_inside_tables` so table contents are not emitted twice.

**AWS multi-page requires S3.** There is no "just send me the PDF" option. `analyze_document` handles one page; anything longer goes through `start_document_analysis` with a JobId, polling, and `NextToken` pagination.

**Model choice is a 7× cost lever on Azure.** `prebuilt-read` is OCR only — no headings, no tables, no hierarchy. `prebuilt-layout` is what makes the output usable for RAG. Picking the cheap one and then wondering why chunking has nothing to work with is an expensive mistake.

---

## Embedding

| | local | azure | aws (Titan) | aws (Cohere) |
|---|---|---|---|---|
| Default model | bge-base-en-v1.5 | text-embedding-3-large | titan-embed-text-v2 | embed-english-v3 |
| Dimensions | 768 (fixed) | 3072 → 256 | 1024 / 512 / 256 | 1024 (fixed) |
| Batch API | unlimited | hundreds per request | **none — one per call** | up to 96 |
| Query/doc asymmetry | instruction prefix | none needed | none needed | **`input_type` required** |
| Max input | 512 tokens | 8191 | 8192 | 512 |
| Cost /1M tokens | free | $0.13 | $0.02 | $0.10 |

### Differences that change your code

**Titan has no batch endpoint.** Throughput comes from client-side concurrency, so `BedrockEmbedder` fans out through a `ThreadPoolExecutor`. It uses `executor.map` rather than `as_completed` because input order must be preserved for zipping vectors back to chunks. Porting Azure ingestion code to Titan naively produces something that looks like a network problem and is an API shape problem.

**Cohere requires `input_type`.** `search_document` when indexing, `search_query` when querying. Using one value for both is the asymmetry bug at its most concrete, and it produces no error.

**Local models need instruction prefixes.** BGE and E5 were trained with a prefix on the query side only. `QUERY_PREFIXES` and `DOC_PREFIXES` in `local_embedder.py` are explicit tables rather than heuristics, because a heuristic that guesses wrong costs recall silently.

**Only Azure and Titan support dimension reduction.** `ada-002` rejects the `dimensions` parameter outright, so `MATRYOSHKA_CAPABLE` is checked before sending it.

---

## Vector store

| | local (pgvector) | azure (AI Search) | aws (OpenSearch) |
|---|---|---|---|
| ANN algorithm | HNSW | HNSW | HNSW (faiss engine) |
| Runtime recall dial | `hnsw.ef_search` per session | `efSearch` in profile | `knn.algo_param.ef_search` |
| Keyword search | `tsvector` + `ts_rank_cd` | native BM25 | native BM25 |
| Hybrid fusion | **RRF in one SQL statement** | **native, one call** | client-side or search pipeline |
| Built-in reranker | no | **yes, semantic ranker** | no |
| Filter language | SQL + array overlap | OData | bool/terms JSON |
| Idempotent writes | `ON CONFLICT DO UPDATE` | `merge_or_upload_documents` | bulk with `_id` (**managed only**) |
| Auth | connection string | managed identity | SigV4 (`aoss` / `es`) |

### Differences that change your code

**Azure does hybrid and reranking in one call.** Passing `search_text` *and* `vector_queries` makes it hybrid; adding `query_type="semantic"` adds the cross-encoder. Assembling that elsewhere means three components and a fusion implementation.

**OpenSearch gives primitives, not a pipeline.** Fusion is your job — client-side RRF (the default here, so behaviour matches the other backends and stays comparable in benchmarks) or a server-side normalization processor.

**Serverless rejects a client-supplied `_id`.** Managed domains accept it, and supplying it is what makes re-ingestion idempotent rather than duplicating. `OpenSearchStore.upsert` branches on `self.serverless`.

**The SigV4 service name differs.** `aoss` for Serverless, `es` for managed. Getting it wrong produces a 403 that looks like a permissions problem and is a signing problem.

**pgvector needs no second system for hybrid.** Postgres full-text search plus `HYBRID_SQL` fuses both arms with RRF in a single statement, before truncation rather than after.

---

## Reranking

| | local | azure | aws |
|---|---|---|---|
| Model | ms-marco-MiniLM-L-6-v2 | AI Search semantic ranker | `amazon.rerank-v1:0` |
| Where it runs | in-process | **inside the search request** | separate service call |
| Client | — | same as search | **`bedrock-agent-runtime`** |
| Score scale | unbounded logits | ~0–4 | normalised [0,1] |
| Suggested floor | −2.0 | 1.4 | 0.02 |
| Candidates per call | unlimited | tied to search `top` | 100 |
| Latency (50 candidates) | 50–150ms CPU | included in search | 100–200ms |
| Cost | free | included in tier | per request |

### Differences that change your code

**Floors are never portable.** The three score scales are unrelated. Copying a local floor to Bedrock rejects everything; the reverse accepts everything. Calibrate per reranker on your own golden set.

**Azure's is a no-op reorder when native.** If the search already ran with `query_type="semantic"`, `@search.rerankerScore` is present and reranking is a sort. The standalone path (re-querying by chunk id) exists for candidates from elsewhere and costs an extra call.

**AWS lives on a different client.** `bedrock-agent-runtime.rerank`, not `bedrock-runtime` — a common "method not found". Sources cap at 100 per call, so a 200-candidate pool is three calls.

---

## Generation

| | local | azure | aws |
|---|---|---|---|
| Client | `ChatOpenAI` at a local base URL | `AzureChatOpenAI` | `ChatBedrockConverse` |
| Strong model | any served model | gpt-4o | Claude Sonnet |
| Cheap model | smaller local model | gpt-4o-mini | Claude Haiku |
| Structured output | varies by model | constrained decoding | tool-based |
| Tool calling | **weaker on small models** | reliable | reliable |
| Vision | model-dependent | gpt-4o | Claude Sonnet |

### Differences that change your code

**Use `ChatBedrockConverse`, not `ChatBedrock`.** The Converse API normalises tool calling and multimodal input across model families. Without it, Anthropic, Meta and Amazon models each want a different request shape and the agent code would need to know which.

**Local models are weaker at tool calling and structured output.** The graph will run; expect more schema-repair rounds and worse routing accuracy. Measure it with `scripts/llm/02_structured_and_tools.py` before assuming parity.

**`max_retries=0` everywhere.** Retry policy lives in `core/resilience.py`. Leaving the SDK's retries on as well compounds backoff and, under load, produces a retry storm.

---

## Authentication

| Provider | Preferred | Fallback |
|---|---|---|
| azure | `DefaultAzureCredential` (managed identity, `az login`) | API key |
| aws | instance role / `AWS_PROFILE` | explicit credentials |
| local | none | — |

Managed identity is preferred because it keeps secrets out of config entirely, which is the first thing a security review looks for. Every Azure adapter takes `use_managed_identity=True` by default and falls back to a key only when one is set.

---

## Adding a provider

1. Implement the stage's base class in `sentinel/<stage>/<name>_<stage>.py`.
2. Register in `core/registry._SPECS`.
3. Add the SDK to a `pyproject.toml` extra.
4. Add a runnable script under `scripts/<stage>/`.
5. Add a row to the tables above.

Imports go **inside** the constructor, never at module top level — the registry's lazy loading is what lets the local pipeline run without any cloud SDK installed. The benchmarks enumerate the registry, so they pick up the new provider with no change; if a benchmark change is needed, the registry entry is wrong.
