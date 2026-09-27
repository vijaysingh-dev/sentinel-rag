# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A RAG pipeline feeding a LangGraph multi-agent root-cause analysis system for the OpenTelemetry Demo ("Astroshop"), a real ~20-service e-commerce deployment instrumented with Prometheus, Jaeger and flagd. Built as a production-shaped reference: the dependencies it talks to are stubbed, but the control flow, failure handling, security model, evaluation and observability are the real shapes.

Provider shape (ADR-029, 2026-09 — see `docs/design-decisions.md`): **parsing is multi-cloud** (local / Azure Document Intelligence / AWS Textract — each does something a local library genuinely cannot: layout models, OCR on scans). **Embedding, vector storage and reranking are local-first infrastructure**, not a cloud-account choice: running pgvector and a local embedding/reranking model costs nothing per query, so those stages default to `local` with one paid escape hatch each (`openai` for embedding, `cohere` for reranking) rather than a maintained Azure/AWS path. **Generation picks a model vendor directly** — `local` (Ollama/vLLM), `openai`, `anthropic` or `google` — independent of whichever cloud account parsing uses. Each stage has its own `Literal` type in `core/config.py` (`ParseProvider`, `EmbedProvider`, `VectorProvider`, `RerankProvider`, `LLMProvider`); there is no shared `Provider` type.

Two things it is optimised for, in this order:

1. **Depth over breadth.** Every design decision should have a stated reason, a named alternative, and an honest account of what it costs. If a change cannot be justified that way, it is probably not the right change.
2. **Provider symmetry — scoped per stage.** Adding a parsing provider needs a local/azure/aws counterpart (or an explicit note in `docs/adapters.md`). Adding an embed/rerank/llm provider does not need to exist on every stage — it needs a stated reason it belongs on the stage it's added to, per ADR-029.

## Layout

```
sentinel/
  core/           types, config, provider registry, observability, resilience, cache
  parsing/        base + local/azure/aws adapters, hybrid routing, quality scoring, figures
  chunking/       base + nine strategies + doc-type routing table
  embedding/      base + local/openai adapters
  vectorstore/    base + pgvector adapter (only maintained vectorstore, ADR-029)
  retrieval/      pipeline, fusion (RRF/MMR/dedupe), query transformation
  reranking/      base + local/cohere adapters
  generation/     LLM adapters — local/openai/anthropic/google
  evaluation/     metrics, parsing/retrieval/generation/agent evaluators, golden set, CI gate
  ingest/         pipeline, ACL resolver, generated fact sheets
  rca/            the agent system: graph, agents/, tools/, state, api
scripts/          runnable per-stage demos, one per provider
benchmarks/       bake-offs producing markdown leaderboards
tests/            ACL, pipeline, parsing, trajectory — all run without cloud credentials
docs/             STAGE-OVERVIEW.md is the map; docs/stages/<stage>.md is the deep dive per stage
.archive/providers/  retired Azure/AWS embed/vectorstore/rerank/llm adapters — see its README
app/otel-demo/    vendored copy of the OpenTelemetry Demo, the target system Sentinel investigates
```

`app/otel-demo/` is a personal, learning-purpose copy of the upstream OpenTelemetry Demo, kept for local use only — it is not pushed to any remote and its own `AGENTS.md` (upstream's contribution policy) does not bind changes made here. Treat it as freely editable in service of this project, unlike everything under `sentinel/`, which follows the invariants and house style below.

## Commands

```bash
make install          # local extras only, no cloud account needed
make doctor           # which providers are actually usable here
make up               # langfuse (postgres/redis are the always-on infra layer, see below)
make demo-up          # the OpenTelemetry Demo (Astroshop) incl. Kafka + Prometheus/Jaeger + incident sidecars
make otel-demo-up     # make up + make demo-up, in order, with the astronomy_db seed applied
make corpus           # (re)generate ./knowledge, ground truth, golden set and agent fixtures
make ingest           # index ./knowledge
make test             # full suite, no infrastructure required
make eval             # deterministic retrieval evaluation + CI gate
make eval-agent       # replay the RCA agent against recorded incident fixtures (needs an LLM)
make capture-incident S=<scenario>  # inject a live fault, record it as an agent-eval case (needs demo-up + chat-api)
make bench-parsers    # parser bake-off
make chat-api         # Sentinel's chat backend (routing, RAG, RCA) on :8093 — what web/ talks to
make chat-server      # RCA graph over the LangGraph protocol on :2024 — Studio debugging only
make chat-ui          # the Agent Chat UI (web/) on :3000, talks to chat-api
```

Run `make doctor` first when something is unexpectedly failing. Most "broken" states are a missing extra or an absent credential, and it reports both in one line.

### The infra layer: postgres, redis, Portainer

`postgres` and `redis` are **not** managed by any `docker compose` project. They're standalone, always-on containers (plain `docker run`, `restart: unless-stopped`) on a dedicated `infra` Docker network, started once and left running whenever the machine is up — the same way Portainer is. Reasoning: idle, the two of them cost ~57MB combined and near-zero CPU (measured with `docker stats`), too small to be worth the ceremony of starting/stopping per project; everything that actually costs something to run idle — otel-demo's ~25 services, Sentinel's own `langfuse` — is manual-start only (`restart: no`, the compose default), started with `make up`/`make demo-up` and stopped with `make down`/`make demo-down` when you're not actively working.

Both `sentinel-rag/docker-compose.yml` (langfuse) and `app/otel-demo/compose.extras.yaml` (product-catalog, cart, otel-collector) join the `infra` network as an `external: true` network and reach `postgres`/`redis` by container name — see either file's header comment for the full reasoning. `make up` checks both are reachable and fails fast with a pointer back here if they aren't; it never starts or stops them.

The container's own bootstrap identity (`POSTGRES_USER`/`POSTGRES_DB`) is `postgres`/`postgres` — Postgres's own maintenance-db convention, not any one project's name, since this container is shared, global infra with no inherent relationship to Sentinel-RAG. Sentinel-RAG's own database (`sentinel`, owned by role `sentinelapp`) and otel-demo's (`astronomy_db`, owned by `astronomy_user`) are created inside it afterwards, the same way you'd create any other project's database on a shared Postgres instance — see `docs/design-decisions.md` for when each gets created.

**Default new Sentinel-owned persistence to the `sentinel` database, not `astronomy_db`, unless that becomes genuinely too complex to justify.** `astronomy_db` is otel-demo's own database — Sentinel reads it (if at all) the way it reads Prometheus or Jaeger, as an external dependency, never as a place to add Sentinel's own tables. Anything Sentinel owns — chat sessions, agent checkpoints, the knowledge corpus's vectors — belongs in `sentinel`, under `sentinelapp`, even when it happens to be *about* otel-demo's services; "which system is this data about" and "which system owns writing it" are different questions, and this repo answers the second one with `sentinel` by default.

One-time setup (or after a fresh machine — `sentinel-rag_pgdata` is the volume the commands below expect to already exist; on a genuinely fresh install, drop the `-v sentinel-rag_pgdata:...` line and Postgres will initialize an empty one, running otel-demo's `astronomy_db` seed automatically via the mounted `docker-entrypoint-initdb.d` directory — `config/init-readonly-role.sql` is staged there too but only once the `sentinel` database it grants against actually exists):

```bash
docker network create infra

docker run -d --name postgres --network infra --restart unless-stopped \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=postgres \
  -p 5432:5432 \
  -v sentinel-rag_pgdata:/var/lib/postgresql/data \
  -v "$HOME/.local/share/sentinel-rag/runtime/pg-init:/docker-entrypoint-initdb.d:ro" \
  --health-cmd "pg_isready -U postgres" --health-interval 5s --health-retries 10 \
  pgvector/pgvector:pg16

docker run -d --name redis --network infra --restart unless-stopped \
  -p 6379:6379 \
  redis:7-alpine redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru

# Portainer, if you don't already have one — a GUI for exactly this "what's running, what's
# idle" question. Not on the infra network (doesn't need to be; it talks to Docker via the
# socket, not to other containers) — this is the same one-time-only, always-on shape though.
docker volume create portainer_data
docker run -d --name portainer --restart unless-stopped \
  -p 9000:9000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  portainer/portainer-ce:latest
```

`~/.local/share/sentinel-rag/runtime/pg-init/` holds staged, 0644-permission copies of init SQL — staged rather than bind-mounted directly from the repo because a fuseblk-style checkout mount (see `app/otel-demo/compose.extras.yaml`'s header comment) makes the repo copies unreadable to the container. It holds `app/otel-demo/src/postgresql/init.sql` (as `20-astronomy-db.sql`, creates `astronomy_user`/`astronomy_db`) and `config/init-readonly-role.sql` (as `10-readonly.sql`, grants `sentinel_readonly` against the `sentinel` database). Both only run automatically on a genuinely empty volume; `config/init-sentinel-db.sql` (creates `sentinelapp`/`sentinel` themselves) is deliberately *not* staged, since `sentinelapp` needs `CREATEDB` or a superuser to run it and it should only ever run once — apply it by hand (`docker exec -i postgres psql -U postgres < config/init-sentinel-db.sql`) on a fresh volume before `10-readonly.sql` can succeed. Re-stage any file by hand if its source changes; `make up`/`make otel-demo-up` no longer do it automatically now that postgres isn't compose-managed.

Redis is a pure cache with no volume — restarting it is always safe and loses nothing that matters. It is shared, global infra the same way postgres is, so each app claims its own logical DB index rather than sharing DB 0: Sentinel's own cache is DB 0 (redis's default, not a project-picked name), otel-demo's Valkey-cart traffic is namespaced onto DB 1 (`app/otel-demo/.env.override`). A third consumer of this redis should claim DB 2, not share 0 or 1. Every key Sentinel writes also carries a `sentinel:` prefix (`settings.redis_key_prefix`, see `sentinel/core/cache.py`) on top of the DB split — belt-and-suspenders so a misconfigured DB index produces a visibly foreign key instead of a silent collision, same spirit as postgres's dedicated `sentinelapp` role rather than reusing the bootstrap login.

## Non-negotiable invariants

Changing any of these needs a deliberate decision and an ADR entry in `docs/design-decisions.md`, not a quiet edit.

**Security**
- `SearchFilter.require_scope()` raises on an empty role list. An empty filter clause matches everything; that is the difference between "sees nothing" and "sees the whole corpus".
- Filters go **inside** the query, never applied afterwards. A post-filter has already loaded restricted content into the process.
- Tenant and role scope come from `rca/runtime` contextvars, never from a tool argument. Anything a model can write is something a prompt injection can control.
- A refusal must not disclose that the restricted document exists. Asserted in `tests/test_acl.py`.
- PromQL queries: metric-name allowlist, forbidden-function list, forced bounded time range — and Prometheus' read-only query API, which is the layer that actually guarantees it (`rca/tools/metrics.py`; see ADR-027, which superseded the SQL-warehouse version of this control in ADR-018).

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
2. Widen that stage's `Literal` in `core/config.py` (`ParseProvider`/`EmbedProvider`/`VectorProvider`/`RerankProvider`/`LLMProvider`) and add its credential fields + a `_check_provider_prerequisites` case that fails fast without them.
3. Register it in `core/registry._SPECS`.
4. Add the SDK to a `pyproject.toml` extra.
5. Add a runnable script under `scripts/<stage>/`.
6. Add a row to the provider matrix in `docs/adapters.md`.
7. Confirm `benchmarks/` picks it up — the sweeps enumerate the registry, so no benchmark change should be needed. If one is, the registry entry is wrong.

Since ADR-029, a new provider does **not** need a counterpart on every other stage — parsing is the one stage where local/azure/aws symmetry is the expectation; embed/vectorstore/rerank/llm each carry only the providers that stage has a stated reason to support (see "What this is"). A stage no longer worth maintaining goes to `.archive/providers/`, not silently deleted — that README says how to revive one.

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

- `rca/clients.py` calls the OpenTelemetry Demo's observability stack (Prometheus, Jaeger, flagd, the incident-generator) — see ADR-027. Endpoints come from `settings` (`prometheus_url`, `jaeger_url`, `flagd_config_path`, `incident_log_url`); `make demo-up` starts the stack and prints the `FLAGD_CONFIG_PATH` to add to `.env`. `ITSMClient.attach_rca` (the one write) is still a stub.
- The knowledge corpus in `knowledge/` is **generated** by `scripts/corpus/` (`make corpus`) from `scripts/corpus/services.yaml`, a curated model of the otel-demo platform. ~66 documents across MD/PDF/DOCX/PPTX/XLSX, including `knowledge/platform/` (`gen_platform.py`): the reference for what the platform *is* — services, ports, compose layout, observability, failure modes, the two sidecars. `services.yaml`'s `key_metrics` and `deployment:` sections are checked against the live stack, not upstream docs; re-check them (Prometheus `/api/v1/label/__name__/values`) when the demo is re-vendored. Edit the YAML and regenerate to reshape it.
- `data/golden.jsonl` is a hand-authored 75-example seed set (`scripts/corpus/gen_golden.py`, balanced against `evaluation/golden.TARGET_MIX`). Grow it from real failures: `evaluation/golden.generate_from_chunks` proposes candidates and `scripts/corpus/_incident_truth.jsonl` gives correct-by-construction RCA-trajectory seeds, but synthetic questions are systematically easier than real ones — edit, don't just generate.
- `data/ground_truth/` is populated for the benchmark-critical documents by `scripts/corpus/gen_ground_truth.py`. The scanned card's truth is hand-transcribed; the rest is derived from the same model that writes the documents.
- `data/agent_cases.jsonl` + `data/fixtures/` are generated (`scripts/corpus/gen_agent_cases.py`): one trajectory-eval case per fault scenario, with recorded tool output consistent with the incident ground truth, so `make eval-agent` replays a full investigation with no Prometheus/Jaeger running. Live cases come from `make capture-incident` (`scripts/corpus/capture_live_incident.py`): real tool output recorded mid-incident into `data/fixtures/live/`, cases in `data/agent_cases_live.jsonl`, folded into `agent_cases.jsonl` on every `make corpus`. Check a live case's `meta.evidence` before trusting it — a fault that never showed in the data tests nothing.

## When numbers appear in documentation

Every result table in `docs/STAGE-OVERVIEW.md` and `docs/stages/*.md` marked _(illustrative)_ is a placeholder showing the shape of the output. Replace them by running the named benchmark against a real corpus. Do not quote them as measured results — the whole point of this repository is being able to explain where a number came from.
