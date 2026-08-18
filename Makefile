.PHONY: help install doctor up down ingest search eval bench test lint fmt clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Install with local extras (no cloud account needed)
	pip install -e '.[local,observability,dev]'

install-all:  ## Install every provider
	pip install -e '.[all]'

doctor:  ## Show which providers are usable in this environment
	python scripts/00_doctor.py

up:  ## Start postgres+pgvector, redis and langfuse
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U sentinel >/dev/null 2>&1; do sleep 1; done
	@echo "ready"

down:
	docker compose down -v

ingest:  ## Ingest ./knowledge into the configured vector store
	python -m sentinel.ingest.cli knowledge

reindex:  ## Rebuild the index from scratch
	python -m sentinel.ingest.cli knowledge --recreate

search:  ## Interactive retrieval check: make search Q="how do I restart ICE"
	python scripts/llm/01_generate_grounded.py "$(Q)"

eval:  ## Run the deterministic evaluation suite and the CI gate
	python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl

eval-full:  ## Also run the LLM-judge metrics
	python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --generation

baseline:  ## Commit current metrics as the regression baseline
	python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --write-baseline

bench-parsers:  ## Parser bake-off across providers
	python benchmarks/parser_bakeoff.py

bench-chunking:  ## Chunking strategy sweep
	python benchmarks/chunking_sweep.py

bench-retrieval:  ## Retrieval configuration sweep
	python benchmarks/retrieval_sweep.py

api:  ## Run the RCA API
	uvicorn sentinel.rca.api:app --reload --port 8080

test:
	pytest -q

lint:
	ruff check sentinel tests scripts benchmarks

fmt:
	ruff check --fix sentinel tests scripts benchmarks

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
