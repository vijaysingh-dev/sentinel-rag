# Sentinel RAG

A multi-cloud RAG pipeline feeding a LangGraph multi-agent root-cause analysis system for a financial index calculation platform.

Every stage — parsing, embedding, vector storage, reranking, generation — has three interchangeable implementations behind one interface: **local**, **Azure** and **AWS**. Provider selection is per stage, so a realistic deployment can parse with Azure Document Intelligence, embed with Bedrock Titan, and rerank locally, without any of it leaking into the agent code.

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
make up                    # postgres+pgvector, redis, langfuse
make doctor                # which providers are usable here
make ingest                # index ./knowledge
uv run python scripts/vectorstore/01_index_and_search.py
make test
```

The entire pipeline runs locally: sentence-transformers for embeddings, pgvector for storage and hybrid search, a local cross-encoder for reranking, Ollama or vLLM for generation.

## Try the same thing on each cloud

```bash
# parsing
python scripts/parsing/01_parse_local.py  data/raw/methodology.pdf
python scripts/parsing/02_parse_azure.py  data/raw/methodology.pdf
python scripts/parsing/03_parse_aws.py    data/raw/methodology.pdf
python scripts/parsing/04_hybrid_compare.py data/raw/methodology.pdf   # cost-per-quality table

# embedding
python scripts/embedding/01_embed_local.py     # shows the query/document asymmetry trap
python scripts/embedding/02_embed_azure.py     # Matryoshka dimension truncation
python scripts/embedding/03_embed_aws.py       # Titan vs Cohere API shapes

# storage and retrieval
python scripts/vectorstore/01_index_and_search.py --provider azure
python scripts/vectorstore/02_acl_filtering.py           # permissions, demonstrated
python scripts/reranking/01_rerank_compare.py            # what a cross-encoder actually fixes
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
  "anomaly_id": "ANOM-11204",
  "tenant_id": "acme",
  "roles": ["admin"],
  "anomaly": {"index_id": "EU_TECH_50", "detected_at": "2026-03-04T09:14:30Z",
              "metric": "index_value", "observed": 1842.11, "expected": 1861.40,
              "detector": "zscore_5m"}}'

curl -N localhost:8080/v1/investigations/ANOM-11204/stream
curl -XPOST localhost:8080/v1/investigations/ANOM-11204/decision \
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

Python 3.12. Docker for the local stack. Cloud extras are optional:

```bash
uv sync --extra azure   # Document Intelligence, AI Search, Azure OpenAI
uv sync --extra aws     # Textract, OpenSearch, Bedrock
uv sync --extra all
```
