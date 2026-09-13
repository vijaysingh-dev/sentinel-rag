# Provider adapter matrix

The reference for what each provider actually does, what it costs, and where they differ in ways
that change your code rather than just your configuration.

**Shape (ADR-029, 2026-09 — full reasoning in `docs/design-decisions.md`):** parsing is the one
stage with genuine local/azure/aws symmetry, because Azure Document Intelligence and AWS Textract
each do something a local library cannot (layout models, OCR on scans). Embedding, vector storage
and reranking are local-first infrastructure — running pgvector and a local embedding/reranking
model costs nothing per query and isn't meaningfully worse for a corpus this size, so those stages
default to `local` with one paid escape hatch each rather than a maintained Azure/AWS path.
Generation is its own axis: a model vendor (`openai`/`anthropic`/`google`) is not the same kind of
decision as which cloud account parsing uses, so `llm_provider` is independent of `parse_provider`.
The old Azure/Bedrock/OpenSearch adapters this replaced are archived at `.archive/providers/`, not
deleted — see that directory's README for what moved and how to revive one.

## At a glance

| Stage | Interface | Providers |
|---|---|---|
| parsing | `BaseParser` | `local` (pdfplumber/python-docx/pptx/openpyxl/bs4) · `azure` (AI Document Intelligence) · `aws` (Textract) |
| embedding | `BaseEmbedder` | `local` (sentence-transformers, default) · `openai` |
| vector store | `BaseVectorStore` | `local` (Postgres + pgvector) — the only provider |
| reranking | `BaseReranker` | `local` (sentence-transformers CrossEncoder, default) · `cohere` |
| generation | `BaseLLM` | `local` (Ollama/vLLM, default) · `openai` · `anthropic` · `google` |

Select per stage in `.env`:

```bash
PARSE_PROVIDER=aws        # already have the Textract bucket
EMBED_PROVIDER=local      # no reason to pay for what a local model does adequately here
VECTORSTORE_PROVIDER=local
RERANK_PROVIDER=local
LLM_PROVIDER=anthropic    # whichever vendor's answers you trust most this week
```

That combination is legal and not unreasonable: parsing has a real reason to use a cloud account,
embedding/reranking don't need one, and generation is picked on its own merits. Each stage's
provider is a genuinely independent `Literal` in `core/config.py` — `ParseProvider`,
`EmbedProvider`, `VectorProvider`, `RerankProvider`, `LLMProvider` — not one shared type, because
the valid choice sets stopped overlapping once ADR-029 landed.

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

| | local | openai |
|---|---|---|
| Default model | bge-base-en-v1.5 | text-embedding-3-small |
| Dimensions | 768 (fixed) | 1536, or 3072 → 256 on `text-embedding-3-large` |
| Batch API | unlimited | hundreds per request |
| Query/doc asymmetry | instruction prefix | none needed |
| Max input | 512 tokens | 8191 |
| Cost /1M tokens | free | $0.02 (small) / $0.13 (large) |

### Differences that change your code

**Local models need instruction prefixes.** BGE and E5 were trained with a prefix on the query side only. `QUERY_PREFIXES` and `DOC_PREFIXES` in `local_embedder.py` are explicit tables rather than heuristics, because a heuristic that guesses wrong costs recall silently.

**Only the `text-embedding-3-*` family supports Matryoshka dimension reduction.** `ada-002`-style models reject the `dimensions` parameter outright, so `MATRYOSHKA_CAPABLE` is checked before sending it — see `OpenAIEmbedder` and `benchmarks/embedding_bakeoff.py` for what the truncation actually costs on your data.

**No batch-endpoint or `input_type` gotchas here anymore.** Bedrock Titan's one-call-per-text limit and Cohere's `input_type` asymmetry were real adapter-shape differences worth knowing about — they're preserved in `.archive/providers/embedding/` if you're comparing designs, but neither applies to the two providers actually registered today.

---

## Vector store

| | local (pgvector) |
|---|---|
| ANN algorithm | HNSW |
| Runtime recall dial | `hnsw.ef_search` per session |
| Keyword search | `tsvector` + `ts_rank_cd` |
| Hybrid fusion | **RRF in one SQL statement** |
| Idempotent writes | `ON CONFLICT DO UPDATE` |
| Auth | connection string |

pgvector is the only maintained vector store (ADR-029) — it needs no second system for hybrid search: Postgres full-text search plus `HYBRID_SQL` fuses both arms with RRF in a single statement, before truncation rather than after, which is the same advantage a managed hybrid API gives you without the managed API. Azure AI Search's native hybrid+semantic-in-one-call and OpenSearch's SigV4/serverless-id quirks are documented in `.archive/providers/vectorstore/` for reference — worth reading if you're designing a comparable feature, not something this repo runs against today.

---

## Reranking

| | local | cohere |
|---|---|---|
| Model | ms-marco-MiniLM-L-6-v2 | rerank-v3.5 |
| Where it runs | in-process | hosted API call |
| Score scale | unbounded logits | normalised [0,1] |
| Suggested floor | −2.0 | 0.02 |
| Latency (50 candidates) | 50–150ms CPU | one network round trip, per query |
| Cost | free | per request |

### Differences that change your code

**Floors are never portable.** The two score scales are unrelated — copying a local floor to Cohere rejects everything, and the reverse accepts everything. Calibrate per reranker on your own golden set.

**Cohere adds latency to every retrieval, not just ingestion.** The local cross-encoder scores a full candidate set in-process with no round trip; Cohere is a network call per query. That's the real trade against its quality, not just the per-request price.

**Azure's search-integrated semantic ranker and Bedrock's separate-client rerank endpoint** are documented in `.archive/providers/reranking/` — worth reading for "how would a search-native reranker work", not available as `RERANK_PROVIDER` choices today.

---

## Generation

| | local | openai | anthropic | google |
|---|---|---|---|---|
| Client | `ChatOpenAI` at a local base URL | `ChatOpenAI` | `ChatAnthropic` | `ChatGoogleGenerativeAI` |
| Strong model | any served model | gpt-4o-mini | claude-sonnet-5 | gemini-2.5-pro |
| Cheap model | smaller local model | gpt-4o-mini | claude-haiku-4-5 | gemini-2.5-flash |
| Auth | none | `OPENAI_API_KEY` | `ANTHROPIC_API_KEY` | `GOOGLE_API_KEY` (Gemini API, not Vertex) |
| Structured output | varies by model | constrained decoding | tool-based | tool-based |
| Tool calling | **weaker on small models** | reliable | reliable | reliable |

### Differences that change your code

**None of these correspond to a "cloud account."** Unlike parsing's Azure/AWS choice, picking an LLM vendor here doesn't imply anything about which account the rest of the pipeline uses — `llm_provider` is independent of `parse_provider`, `embed_provider` and `rerank_provider`. Set the one you trust for answer quality this week; there's no infrastructure reason to keep it aligned with the others.

**Local models are weaker at tool calling and structured output.** The graph will run; expect more schema-repair rounds and worse routing accuracy. Measure it with `scripts/llm/02_structured_and_tools.py --provider <name>` before assuming parity between vendors.

**`max_retries=0` everywhere.** Retry policy lives in `core/resilience.py`. Leaving the SDK's retries on as well compounds backoff and, under load, produces a retry storm.

**Google's Gemini API key is not a GCP service account.** `GoogleLLM` uses `langchain-google-genai`'s plain-API-key auth (`google_api_key=`), not Vertex AI — a materially different setup with its own billing and IAM. If Vertex is ever needed, that's a different adapter, not a config flag on this one.

**Azure OpenAI (`AzureChatOpenAI`, deployments, managed identity) and Bedrock Converse (`ChatBedrockConverse`, cross-model-family normalisation)** are preserved in `.archive/providers/generation/` — real, working reference for those specific mechanics, just not exercised by this repo's registry.

---

## Adding a provider

1. Implement the stage's base class in `sentinel/<stage>/<name>_<stage>.py`.
2. Widen that stage's `Literal` in `core/config.py`, add its credential fields, and add a case to `_check_provider_prerequisites` that fails fast if the provider is selected without them.
3. Register in `core/registry._SPECS`.
4. Add the SDK to a `pyproject.toml` extra.
5. Add a runnable script under `scripts/<stage>/`.
6. Add a row to the tables above.

Imports go **inside** the constructor, never at module top level — the registry's lazy loading is what lets the local pipeline run without any cloud SDK installed. The benchmarks enumerate the registry, so they pick up the new provider with no change; if a benchmark change is needed, the registry entry is wrong.

Since ADR-029, a new embed/vectorstore/rerank/llm provider does not need a counterpart on every other stage the way a parsing provider does — state why it belongs on the stage you're adding it to. If you're bringing back a provider that lives in `.archive/providers/`, that directory's README has the specific steps.
