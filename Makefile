.PHONY: help install doctor up down demo-up demo-down demo-status corpus ingest search eval bench test lint fmt clean

# The OpenTelemetry Demo — the real system the RCA agent investigates. Its compose lives under
# app/otel-demo/; these targets drive it with the file set Sentinel needs (base services +
# Prometheus/Jaeger + the incident-generator and health-monitor sidecars in compose.extras.yaml).
OTEL_DEMO := app/otel-demo
DEMO_COMPOSE := docker compose \
	-f $(OTEL_DEMO)/compose.yaml \
	-f $(OTEL_DEMO)/compose.observability.yaml \
	-f $(OTEL_DEMO)/compose.extras.yaml \
	--project-name astroshop

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Install with local extras (no cloud account needed)
	uv sync --extra local --extra observability --extra dev --extra corpus

install-all:  ## Install every provider
	uv sync --extra all

doctor:  ## Show which providers are usable in this environment
	uv run python scripts/00_doctor.py

up:  ## Start postgres+pgvector, redis and langfuse
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U sentinel >/dev/null 2>&1; do sleep 1; done
	@echo "ready"

down:
	docker compose down -v

demo-up:  ## Start the OpenTelemetry Demo (Astroshop) with Prometheus, Jaeger and the incident sidecars
	@mkdir -p $${HOME}/.local/share/sentinel-rag/runtime/flagd \
	          $${HOME}/.local/share/sentinel-rag/runtime/postgresql \
	          $${HOME}/.local/share/sentinel-rag/app/incident-generator-data
	@test -f $${HOME}/.local/share/sentinel-rag/runtime/flagd/demo.flagd.json \
	    || cp $(OTEL_DEMO)/src/flagd/demo.flagd.json $${HOME}/.local/share/sentinel-rag/runtime/flagd/
	@test -f $${HOME}/.local/share/sentinel-rag/runtime/postgresql/init.sql \
	    || cp $(OTEL_DEMO)/src/postgresql/init.sql $${HOME}/.local/share/sentinel-rag/runtime/postgresql/ 2>/dev/null || true
	$(DEMO_COMPOSE) up -d --no-build --pull missing
	@echo "waiting for Prometheus..."
	@until curl -sf http://localhost:9090/-/ready >/dev/null 2>&1; do sleep 2; done
	@echo "Astroshop up:  store http://localhost:8080  ·  Jaeger :16686  ·  Prometheus :9090  ·  flags :4000"
	@echo "sidecars:      incident-generator :8092  ·  system-monitor :8091"
	@echo 'point Sentinel at it: add FLAGD_CONFIG_PATH=$${HOME}/.local/share/sentinel-rag/runtime/flagd/demo.flagd.json to .env'

demo-down:  ## Stop the OpenTelemetry Demo
	$(DEMO_COMPOSE) down

demo-status:  ## Show the Astroshop health rollup and the recent incident log
	@echo "== health rollup ==" && curl -sf http://localhost:8091/health | python3 -m json.tool || echo "system-monitor not reachable"
	@echo "== recent incidents ==" && curl -sf http://localhost:8092/incidents?limit=5 | python3 -m json.tool || echo "incident-generator not reachable"

corpus:  ## (Re)generate the knowledge corpus and parser ground truth from scripts/corpus/
	uv sync --extra corpus
	uv run python scripts/corpus/build_all.py --clean

ingest:  ## Ingest ./knowledge into the configured vector store
	uv run python -m sentinel.ingest.cli knowledge

reindex:  ## Rebuild the index from scratch
	uv run python -m sentinel.ingest.cli knowledge --recreate

search:  ## Interactive retrieval check: make search Q="how do I restart ICE"
	uv run python scripts/llm/01_generate_grounded.py "$(Q)"

eval:  ## Run the deterministic evaluation suite and the CI gate
	uv run python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl

eval-full:  ## Also run the LLM-judge metrics
	uv run python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --generation

eval-agent:  ## Replay the RCA agent against recorded incident fixtures and score its trajectory
	uv run python scripts/evaluation/02_evaluate_agent.py

baseline:  ## Commit current metrics as the regression baseline
	uv run python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --write-baseline

bench-parsers:  ## Parser bake-off across providers
	uv run python benchmarks/parser_bakeoff.py

bench-chunking:  ## Chunking strategy sweep
	uv run python benchmarks/chunking_sweep.py

bench-retrieval:  ## Retrieval configuration sweep
	uv run python benchmarks/retrieval_sweep.py

api:  ## Run the RCA API (custom SSE API on :8080)
	uv run uvicorn sentinel.rca.api:app --reload --port 8080

chat-server:  ## Serve the RCA graph over the LangGraph protocol for the web/ chat UI (:2024)
	uv sync --extra chat
	uv run langgraph dev --config langgraph.json --port 2024 --no-browser

chat-ui:  ## Run the Agent Chat UI (needs `make chat-server` in another terminal, and pnpm)
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
