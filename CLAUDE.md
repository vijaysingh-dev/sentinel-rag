# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A multi-cloud RAG pipeline (local / Azure / AWS) feeding a LangGraph multi-agent root-cause analysis system for a financial index calculation platform. Built as a production-shaped reference: the dependencies it talks to are stubbed, but the control flow, failure handling, security model, evaluation and observability are the real shapes.

Two things it is optimised for, in this order:

1. **Depth over breadth.** Every design decision should have a stated reason, a named alternative, and an honest account of what it costs. If a change cannot be justified that way, it is probably not the right change.
2. **Provider symmetry.** Anything added for one cloud should have a counterpart for the others, or an explicit note in `docs/adapters.md` explaining why it cannot.

## Layout

```
sentinel/
  core/           types, config, provider registry, observability, resilience, cache
  parsing/        base + local/azure/aws adapters, hybrid routing, quality scoring, figures
  chunking/       base + nine strategies + doc-type routing table
  embedding/      base + local/azure/aws adapters
  vectorstore/    base + pgvector/azure-search/opensearch adapters
  retrieval/      pipeline, fusion (RRF/MMR/dedupe), query transformation
  reranking/      base + local/azure/aws adapters
  generation/     LLM adapters
  evaluation/     metrics, parsing/retrieval/generation/agent evaluators, golden set, CI gate
  ingest/         pipeline, ACL resolver, generated fact sheets
  rca/            the agent system: graph, agents/, tools/, state, api
scripts/          runnable per-stage demos, one per provider
benchmarks/       bake-offs producing markdown leaderboards
tests/            ACL, pipeline, parsing, trajectory — all run without cloud credentials
docs/             STAGE-OVERVIEW.md is the primary reference
```

## Commands

```bash
make install          # local extras only, no cloud account needed
make doctor           # which providers are actually usable here
make up               # postgres+pgvector, redis, langfuse
make ingest           # index ./knowledge
make test             # full suite, no infrastructure required
make eval             # deterministic evaluation + CI gate
make bench-parsers    # parser bake-off
make api              # RCA API on :8080
```

Run `make doctor` first when something is unexpectedly failing. Most "broken" states are a missing extra or an absent credential, and it reports both in one line.

## Non-negotiable invariants

Changing any of these needs a deliberate decision and an ADR entry in `docs/design-decisions.md`, not a quiet edit.

**Security**
- `SearchFilter.require_scope()` raises on an empty role list. An empty filter clause matches everything; that is the difference between "sees nothing" and "sees the whole corpus".
- Filters go **inside** the query, never applied afterwards. A post-filter has already loaded restricted content into the process.
- Tenant and role scope come from `rca/runtime` contextvars, never from a tool argument. Anything a model can write is something a prompt injection can control.
- A refusal must not disclose that the restricted document exists. Asserted in `tests/test_acl.py`.
- Generated SQL: parse with sqlglot, single statement, forced LIMIT — and a database-level read-only role, which is the layer that actually guarantees it.

**Grounding**
- Claims citing a ref that never appeared in a real tool result are dropped in `rca/agents/common.observed_refs`, not argued with by the verifier.
- `publish_node` is unreachable without `approved_by`.
- `DEGRADED_CONFIDENCE_CAP` is enforced in code after the model returns, not requested in a prompt.

**Correctness**
- Chunk ids are content-derived (`Chunk.make_id`), so re-ingestion is idempotent and citations survive it.
- `settings.index_version` fingerprints chunking config + provider + model + dims. Every chunk is stamped with it; every query filters on it.
- `embed_chunks` embeds `embed_text`, never `context_text`. Getting this backwards silently destroys the point of two-field chunks.
- Live-state tools are never cached — `core/cache.LIVE_TOOLS`.

## House style

**Comments explain *why*, never *what*.** The code says what it does. A comment earns its place by explaining a decision, a trade-off, or a non-obvious failure mode. No line-by-line narration, no comment on every block.

Good:
```python
# Jitter is not decoration: without it, N concurrent callers that fail together retry together
# and rebuild the exact traffic spike that caused the failure.
delay = retry_after or min(cap, base * (2 ** attempt)) * (0.5 + random.random())
```

Bad:
```python
# Calculate the delay
delay = retry_after or min(cap, base * (2 ** attempt)) * (0.5 + random.random())
```

**Module docstrings carry the argument.** Each module opens with what it does, why it exists, and what the alternative was. That is where a reader should be able to find the reasoning without reading the implementation.

**Explain jargon on first use in a docstring.** "SigV4 (AWS's request-signing scheme)" the first time, plain thereafter. Assume a reader who is competent but new to this specific corner.

Other conventions:
- Line length 120. `ruff` config is in `pyproject.toml`.
- `from __future__ import annotations` at the top of every module.
- Type hints on public functions; `Any` is acceptable for SDK objects.
- Structured logging via `structlog`: `log.info("event_name", key=value)`, never f-strings.
- Never log chunk content or raw queries. Ids, scores and hashes only — see `hash_query`.

## How to extend

### Adding a provider to an existing stage

1. Implement the stage's base class in `sentinel/<stage>/<name>_<stage>.py`.
2. Register it in `core/registry._SPECS`.
3. Add the SDK to a `pyproject.toml` extra.
4. Add a runnable script under `scripts/<stage>/`.
5. Add a row to the provider matrix in `docs/adapters.md`.
6. Confirm `benchmarks/` picks it up — the sweeps enumerate the registry, so no benchmark change should be needed. If one is, the registry entry is wrong.

Imports must be **inside** the constructor or method, never at module top level. The registry's lazy loading is what lets someone run the whole local pipeline without boto3 or the Azure SDKs installed.

### Adding a chunking strategy

Subclass `BaseChunker`, decorate with `@register("name")`, call `self._finalise()` before returning. It will appear in `benchmarks/chunking_sweep.py` automatically.

### Adding an agent tool

Put it in `rca/tools/`, decorate with `@tool`, and:
- return a `ref` so claims are traceable;
- return errors as observations with an instruction, not exceptions — the reader is a model deciding what to do next, not a human reading a stack trace;
- support `fixture_lookup(name)` so the eval suite can replay it;
- add the prefix to `REF_PREFIXES` in `agents/common.py`;
- if it reads live state, add it to `LIVE_TOOLS` in `core/cache.py`.

Then add it to exactly one specialist in `rca/agents/specialists.py`. Never to the supervisor — `tests/test_trajectory.py` asserts the supervisor holds no tools, and that test exists because this is the constraint most likely to erode.

### Changing anything that affects retrieval quality

Run `make eval` before and after. If a metric moves, say by how much and whether the bootstrap confidence interval overlaps. With a 40-example golden set a two-point improvement is usually noise.

If the change is an improvement worth keeping, run `make baseline` and commit `benchmarks/baseline.json` in the same PR, so raising the bar is reviewable.

## Known gaps

Deliberate, not oversights — good places to pick up work:

- `rca/clients.py` is stubbed. Real implementations call the platform's own APIs.
- The knowledge corpus in `knowledge/` is a small sample. Real evaluation needs a real corpus.
- `data/golden.jsonl` needs hand-written examples. `evaluation/golden.generate_from_chunks` bootstraps candidates, but synthetic questions are systematically easier than real ones — treat its output as a starting point to edit, not a golden set.
- No ground-truth files in `data/ground_truth/` yet, so `parser_bakeoff.py` currently falls back to heuristic scoring.
- Ingestion has no CLI module yet (`make ingest` references `sentinel.ingest.cli`).

## When numbers appear in documentation

Every result table in `docs/STAGE-OVERVIEW.md` marked _(illustrative)_ is a placeholder showing the shape of the output. Replace them by running the named benchmark against a real corpus. Do not quote them as measured results — the whole point of this repository is being able to explain where a number came from.
