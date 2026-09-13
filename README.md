# Sentinel RAG

A RAG pipeline feeding a LangGraph multi-agent root-cause analysis system for the OpenTelemetry Demo ("Astroshop"), a real ~20-service e-commerce deployment instrumented with Prometheus, Jaeger and flagd.

**Parsing** is genuinely multi-cloud — **local**, **Azure** Document Intelligence and **AWS** Textract behind one interface, because each does something a local library cannot (layout models, OCR on scans). **Embedding, vector storage and reranking are local-first infrastructure** instead: they default to local (sentence-transformers, pgvector, a local cross-encoder) with one paid escape hatch each (`openai`, none, `cohere`) rather than a maintained cloud matrix. **Generation** picks a model vendor directly — `local` / `openai` / `anthropic` / `google` — independent of every other stage. None of this leaks into the agent code either way; see `docs/adapters.md` and ADR-029 in `docs/design-decisions.md` for the reasoning.

The dependencies it talks to (warehouse, deployment service, ITSM) are stubbed. The control flow, security model, failure handling, evaluation and observability are the real shapes.

## Why this exists

Most RAG examples stop at "embed the chunks and search them". The interesting questions start after that:

- How do you decide whether a cloud parser is worth its price on *your* corpus?
- What do you do when the answer is in the index but ranked eleventh?
- How do you enforce document-level permissions without leaking that a restricted document exists?
- How do you put a non-deterministic system behind a CI gate?
- What happens when the embedding service is down?

Each of those has a runnable answer here.

## Quick start — no cloud account required

```bash
uv sync --extra local --extra observability --extra dev
cp .env.example .env
make up                    # langfuse (postgres/redis are the always-on infra layer — see CLAUDE.md)
make doctor                # which providers are usable here
make ingest                # index ./knowledge
uv run python scripts/vectorstore/01_index_and_search.py
make test
```

The entire pipeline runs locally: sentence-transformers for embeddings, pgvector for storage and hybrid search, a local cross-encoder for reranking, Ollama or vLLM for generation.

## Try the same thing on each provider

```bash
# parsing — the one genuinely multi-cloud stage
python scripts/parsing/01_parse_local.py  knowledge/architecture/dependency-model.pdf
python scripts/parsing/02_parse_azure.py  knowledge/architecture/dependency-model.pdf
python scripts/parsing/03_parse_aws.py    knowledge/architecture/dependency-model.pdf
python scripts/parsing/04_hybrid_compare.py knowledge/architecture/dependency-model.pdf   # cost-per-quality table

# embedding — local by default, openai is the escape hatch
python scripts/embedding/01_embed_local.py     # shows the query/document asymmetry trap
python scripts/embedding/02_embed_openai.py    # Matryoshka dimension truncation

# storage and retrieval — pgvector is the only vector store
python scripts/vectorstore/01_index_and_search.py
python scripts/vectorstore/02_acl_filtering.py           # permissions, demonstrated
python scripts/reranking/01_rerank_compare.py --provider local   # or --provider cohere

# generation — pick a vendor directly, independent of the parsing cloud account
python scripts/llm/01_generate_grounded.py --provider anthropic "how do I restart the checkout service"
```

## Where to look first

| To understand | Read |
|---|---|
| every stage: what, why, and what it cost | `docs/STAGE-OVERVIEW.md` (map) → `docs/stages/<stage>.md` (deep dive) |
| the adapter pattern that makes clouds swappable | `sentinel/core/registry.py` |
| the contract between stages | `sentinel/core/types.py` — note the two-field `Chunk` |
| the most interesting policy decision in the pipeline | `sentinel/parsing/hybrid.py` |
| how the stages compose | `sentinel/retrieval/pipeline.py` |
| the agent topology in one screen | `sentinel/rca/graph.py` |
| why parallel specialists don't clobber each other | `sentinel/rca/state.py` — the `Annotated` reducers |
| how the run pauses for hours without holding a worker | `sentinel/rca/agents/writer.py` — `interrupt()` |
| what "good" means and how it's enforced | `sentinel/evaluation/` and `tests/` |
| the arguments, ADR by ADR | `docs/design-decisions.md` |

## Design decisions worth arguing with

- **Provider selection is per stage, not global.** Different data classes have different residency rules, and the best parser and the best embedder are rarely from the same vendor.
- **Parse locally first, escalate on a quality score.** Most documents are handled perfectly by a free library. Paying per page for all of them wastes money; using only the free library silently loses the rest.
- **Chunks carry two text fields.** The retrieval unit and the generation unit do not have to be the same thing. Without that split, sentence-window and parent-document strategies are inexpressible.
- **Chunking strategy is chosen per document type.** A runbook and a glossary want opposite things.
- **Security filters go inside the query.** A post-filter has already loaded restricted content into your process.
- **Cache knowledge, never telemetry.** One conversation mixes both, and a cached health check is worse than no health check.
- **Supervisor, not swarm.** The orchestrator keeps control so budgets and step caps are enforceable, and it holds zero specialist tools by construction.
- **Degrade, never fail.** A dead warehouse produces a partial RCA marked degraded with capped confidence, not a 500.
- **No autonomous writes.** The single write tool sits behind a human interrupt.
- **Fixtures in evals.** Tool output is replayed, so the suite tests reasoning rather than infrastructure uptime and runs on every pull request for the price of a coffee.

## The RCA system

```
intake → supervisor ⇄ {knowledge, data, change}
                   ↘ verifier ⇄ supervisor
                   ↘ writer → approval → publish
```

```bash
make api

curl -XPOST localhost:8080/v1/investigations -H 'content-type: application/json' -d '{
  "anomaly_id": "INC-2607",
  "tenant_id": "acme",
  "roles": ["admin"],
  "anomaly": {"summary": "checkout error ratio / latency crossed its alert threshold",
              "alerting_service": "checkout", "started_at": "2026-07-26T14:31:00Z",
              "signal": "error_ratio"}}'

curl -N localhost:8080/v1/investigations/INC-2607/stream
curl -XPOST localhost:8080/v1/investigations/INC-2607/decision \
     -H 'content-type: application/json' -d '{"action":"approve","reviewer":"vijay"}'
```

## Evaluation

```bash
make eval          # deterministic: retrieval metrics + CI gate
make eval-full     # adds LLM-judge metrics: faithfulness, citations, refusal
make baseline      # commit current metrics as the regression baseline
```

Retrieval metrics are deterministic and cheap, so they gate every pull request. Generation metrics need an LLM judge, so they run nightly. Safety checks are never averaged — any violation fails the run outright.

## Benchmarks

```bash
make bench-parsers     # accuracy, cost and latency per provider, per format
make bench-chunking    # every strategy, ranked on retrieval quality
make bench-retrieval   # fusion method, candidate depth, reranker on/off
```

Each writes a CSV to `benchmarks/results/` and a markdown leaderboard to `docs/`.

## Requirements

Python 3.12. Docker for the local stack. Cloud/vendor extras are optional:

```bash
uv sync --extra azure       # parsing only: Azure Document Intelligence
uv sync --extra aws         # parsing only: AWS Textract
uv sync --extra inference   # embed/rerank/llm escape hatches: OpenAI, Anthropic, Google, Cohere
uv sync --extra all
```
