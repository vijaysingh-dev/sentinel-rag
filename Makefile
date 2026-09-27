.PHONY: help install doctor up down demo-up otel-demo-up demo-down demo-status corpus ingest search eval bench test lint fmt clean capture-incident capture-incidents

# The OpenTelemetry Demo — the real system the RCA agent investigates. Its compose lives under
# app/otel-demo/; these targets drive it with the file set Sentinel needs (base services +
# Prometheus/Jaeger + the incident-generator and health-monitor sidecars in compose.extras.yaml).
#
# compose.full.yaml (Kafka, accounting, fraud-detection) is layered in: without it the order
# pipeline has no async half, the kafkaQueueProblems fault has no target, and every corpus
# document about consumer lag describes something that isn't running. Costs ~1.1GB of memory
# limits. Order matters — full before observability, whose collector command already expects
# otelcol-config-full.yml ahead of its own.
OTEL_DEMO := app/otel-demo
# --env-file .env.override is required explicitly: compose only auto-loads a bare `.env` from
# the project directory, never `.env.override` — the file otel-demo's own README designates as
# the "don't push to upstream, put your overrides here" seam and where compose.extras.yaml's
# resource-sharing settings (POSTGRES_HOST, VALKEY_ADDR, the LLM_* reuse of Sentinel's key) live.
DEMO_COMPOSE := docker compose \
	-f $(OTEL_DEMO)/compose.yaml \
	-f $(OTEL_DEMO)/compose.full.yaml \
	-f $(OTEL_DEMO)/compose.observability.yaml \
	-f $(OTEL_DEMO)/compose.extras.yaml \
	--env-file $(OTEL_DEMO)/.env \
	--env-file $(OTEL_DEMO)/.env.override \
	--project-name astroshop

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Install with local extras (no cloud account needed)
	uv sync --extra local --extra observability --extra dev --extra corpus

install-all:  ## Install every provider
	uv sync --extra all

doctor:  ## Show which providers are usable in this environment
	uv run python scripts/00_doctor.py

up:  ## Start langfuse (postgres/redis are the standalone always-on `infra` containers, not managed here)
	@# postgres and redis are no longer part of this compose project — they're always-on, plain
	@# `docker run` containers on the `infra` Docker network (see docker-compose.yml's header
	@# comment and CLAUDE.md/README for the exact run commands), started once, independent of any
	@# project, the same way Portainer is. This target only checks they're reachable and starts
	@# the one thing that actually is project-local: langfuse.
	@docker network inspect infra >/dev/null 2>&1 || { \
	    echo "no 'infra' network — postgres/redis haven't been started yet; see CLAUDE.md for the docker run commands"; exit 1; }
	@docker exec postgres pg_isready -U postgres >/dev/null 2>&1 || { \
	    echo "postgres isn't running on the infra network — see CLAUDE.md for the docker run command"; exit 1; }
	@docker exec redis redis-cli ping >/dev/null 2>&1 || { \
	    echo "redis isn't running on the infra network — see CLAUDE.md for the docker run command"; exit 1; }
	docker compose up -d
	@echo "ready"

down:  ## Stop langfuse (postgres/redis are left running — they're the always-on global layer)
	docker compose down

demo-up:  ## Start the OpenTelemetry Demo (Astroshop) with Prometheus, Jaeger and the incident sidecars
	@# Requires postgres+redis already running on the `infra` network (see `make up`'s check, or
	@# just run it — it fails fast with the right pointer if they're not up). astronomy-db and
	@# valkey-cart no longer start as their own containers (see app/otel-demo/compose.extras.yaml)
	@# — product-catalog and cart join the shared infra containers instead. `otel-demo-up` below
	@# also applies the one-time astronomy_db seed; call `demo-up` directly only once that seed is
	@# already applied (it usually is — it only needs doing again after a fresh postgres volume).
	@mkdir -p $${HOME}/.local/share/sentinel-rag/runtime/flagd \
	          $${HOME}/.local/share/sentinel-rag/app/incident-generator-data
	@test -f $${HOME}/.local/share/sentinel-rag/runtime/flagd/demo.flagd.json \
	    || cp $(OTEL_DEMO)/src/flagd/demo.flagd.json $${HOME}/.local/share/sentinel-rag/runtime/flagd/
	API_KEY=$$(grep '^GOOGLE_API_KEY=' .env | cut -d= -f2-) $(DEMO_COMPOSE) up -d --no-build --pull missing
	@echo "waiting for Prometheus..."
	@until curl -sf http://localhost:9090/-/ready >/dev/null 2>&1; do sleep 2; done
	@echo "Astroshop up:  store http://localhost:8080  ·  Jaeger http://localhost:16686/jaeger/ui  ·  Prometheus :9090  ·  flags http://localhost:8080/feature"
	@echo "sidecars:      incident-generator :8092  ·  system-monitor :8091"
	@echo 'point Sentinel at it: add FLAGD_CONFIG_PATH=$${HOME}/.local/share/sentinel-rag/runtime/flagd/demo.flagd.json to .env'

otel-demo-up:  ## make up + make demo-up in the right order, with the astronomy_db seed applied against the shared postgres
	$(MAKE) up
	@docker exec postgres psql -U postgres -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'astronomy_db'" | grep -q 1 \
	    || docker exec -i postgres psql -U postgres -d postgres < $(OTEL_DEMO)/src/postgresql/init.sql
	$(MAKE) demo-up

demo-down:  ## Stop the OpenTelemetry Demo
	$(DEMO_COMPOSE) down

demo-status:  ## Show the Astroshop health rollup and the recent incident log
	@echo "== health rollup ==" && curl -sf http://localhost:8091/health | python3 -m json.tool || echo "system-monitor not reachable"
	@echo "== recent incidents ==" && curl -sf http://localhost:8092/incidents?limit=5 | python3 -m json.tool || echo "incident-generator not reachable"

corpus:  ## (Re)generate the knowledge corpus and parser ground truth from scripts/corpus/
	uv run python scripts/corpus/build_all.py --clean

ingest:  ## Ingest ./knowledge into the configured vector store
	uv run python -m sentinel.ingest.cli knowledge

reindex:  ## Rebuild the index from scratch
	uv run python -m sentinel.ingest.cli knowledge --recreate

search:  ## Interactive retrieval check: make search Q="how do I restart checkout"
	uv run python scripts/llm/01_generate_grounded.py "$(Q)"

eval:  ## Run the deterministic evaluation suite and the CI gate
	uv run python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl

eval-full:  ## Also run the LLM-judge metrics
	uv run python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --generation

eval-agent:  ## Replay the RCA agent against recorded incident fixtures and score its trajectory
	uv run python scripts/evaluation/02_evaluate_agent.py

capture-incident:  ## Inject one live fault and record it as an agent-eval case: make capture-incident S=payment_degraded
	uv run python scripts/corpus/capture_live_incident.py $(S)

capture-incidents:  ## Same, for every incident-generator scenario in turn (~45 min; needs demo-up + chat-api)
	uv run python scripts/corpus/capture_live_incident.py --all

baseline:  ## Commit current metrics as the regression baseline
	uv run python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --write-baseline

bench-parsers:  ## Parser bake-off across providers
	uv run python benchmarks/parser_bakeoff.py

bench-chunking:  ## Chunking strategy sweep
	uv run python benchmarks/chunking_sweep.py

bench-retrieval:  ## Retrieval configuration sweep
	uv run python benchmarks/retrieval_sweep.py

chat-api:  ## Serve sentinel/chat's HTTP+SSE API for web/ (:8093) — the production chat backend
	uv run uvicorn sentinel.chat.api:app --port 8093 --reload

chat-server:  ## Studio-debugging only: serve the RCA graph over the LangGraph protocol (:2024).
               ## web/ no longer talks to this in normal use — see `make chat-api`.
	uv run langgraph dev --config langgraph.json --port 2024 --no-browser

chat-ui:  ## Run the Agent Chat UI (needs `make chat-api` in another terminal, and pnpm)
	@test -f web/.env || cp web/.env.sentinel.example web/.env
	cd web && pnpm install && pnpm dev

test:
	uv run pytest -q

lint:
	uv run ruff check sentinel tests scripts benchmarks

fmt:
	uv run ruff check --fix sentinel tests scripts benchmarks

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
