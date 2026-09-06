"""Structured logging, metrics, tracing and cost accounting.

Three principles govern what is recorded here, and each one exists because of a specific class of
incident that is otherwise undiagnosable:

1. **Log chunk ids and scores, never chunk content or raw queries.** Without retrieved ids, no bug
   report about a wrong answer is falsifiable — you cannot tell a retrieval failure from a
   generation failure. With raw content, you have quietly copied sensitive documents and whatever
   an operator typed into a chat box into a telemetry store with a wider audience than the source.

2. **Latency is recorded per stage, not per request.** A p95 of four seconds is not actionable; a
   p95 of four seconds with 3.6s in generation is.

3. **Cost is a first-class metric with a per-turn distribution.** Alerting on daily spend catches
   the problem a day late. Alerting on p99 cost per turn catches the pathological query that
   retrieves forty chunks while it is still one query.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

# --------------------------------------------------------------------------- optional deps
try:
    from prometheus_client import Counter, Histogram

    _PROM = True
except ImportError:  # metrics are optional; the pipeline must run without them
    _PROM = False

    class _Noop:
        def labels(self, *a: Any, **k: Any) -> _Noop:
            return self

        def inc(self, *a: Any, **k: Any) -> None: ...
        def observe(self, *a: Any, **k: Any) -> None: ...
        def set(self, *a: Any, **k: Any) -> None: ...

    Counter = Histogram = lambda *a, **k: _Noop()  # type: ignore[assignment]


def configure_logging(level: str | None = None, json_output: bool | None = None) -> None:
    """Sets up structlog.

    JSON in production so the log aggregator can index fields; console renderer locally because
    reading JSON by eye at 3am is its own small punishment.
    """
    if level is None:
        # `settings.debug` forces DEBUG; otherwise honour LOG_LEVEL. Import lazily so this module
        # stays importable before settings are constructed (e.g. in isolated unit tests).
        try:
            from sentinel.core.config import settings

            level = settings.effective_log_level
        except Exception:
            level = os.getenv("LOG_LEVEL", "INFO")
    json_output = json_output if json_output is not None else not sys.stderr.isatty()

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger("sentinel")

# ------------------------------------------------------------------------------- metrics
STAGE_LATENCY = Histogram(
    "sentinel_stage_duration_seconds",
    "Wall time per pipeline stage",
    ["stage", "provider"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
STAGE_ERRORS = Counter("sentinel_stage_errors_total", "Errors per stage", ["stage", "provider", "kind"])
TOOL_CALLS = Counter("sentinel_tool_calls_total", "Tool invocations", ["tool", "outcome"])
NODE_LATENCY = Histogram("sentinel_node_duration_seconds", "Agent node wall time", ["node"])
NODE_ERRORS = Counter("sentinel_node_errors_total", "Agent node errors", ["node", "kind"])
TOKENS = Counter("sentinel_tokens_total", "Tokens consumed", ["model", "direction"])
COST = Histogram(
    "sentinel_cost_usd",
    "USD per operation",
    ["stage", "model"],
    buckets=(0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
)
CACHE_EVENTS = Counter("sentinel_cache_events_total", "Cache hits and misses", ["layer", "event"])
RETRIEVAL_SCORE = Histogram(
    "sentinel_retrieval_top_score",
    "Top-1 score per retrieval",
    ["provider"],
    buckets=(0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0, 2.0, 3.0, 4.0),
)
DEGRADED_MODE = Counter("sentinel_degraded_total", "Degraded-path executions", ["stage", "mode"])
PARSE_QUALITY = Histogram(
    "sentinel_parse_quality",
    "Heuristic parse quality score",
    ["provider", "format"],
    buckets=(0.1, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)


# --------------------------------------------------------------------------- cost tables
# USD per 1M tokens. Deliberately loaded as data rather than hard-coded at call sites so that a
# price change is a one-line edit; treat these as illustrative and refresh from the provider's
# pricing page before quoting a number to anyone.
PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"in": 2.50, "out": 10.00},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "text-embedding-3-large": {"in": 0.13, "out": 0.0},
    "text-embedding-3-small": {"in": 0.02, "out": 0.0},
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"in": 3.00, "out": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"in": 0.80, "out": 4.00},
    "amazon.titan-embed-text-v2:0": {"in": 0.02, "out": 0.0},
    "cohere.embed-english-v3": {"in": 0.10, "out": 0.0},
    "local": {"in": 0.0, "out": 0.0},
}

# USD per page for document parsing services.
PAGE_PRICING: dict[str, float] = {
    "azure:prebuilt-layout": 0.010,
    "azure:prebuilt-read": 0.0015,
    "aws:textract-detect": 0.0015,
    "aws:textract-analyze-tables": 0.015,
    "aws:textract-analyze-layout": 0.015,
    "local": 0.0,
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    rates = PRICING.get(model, PRICING["local"])
    return (input_tokens * rates["in"] + output_tokens * rates["out"]) / 1_000_000


def estimate_page_cost(key: str, pages: int) -> float:
    return PAGE_PRICING.get(key, 0.0) * pages


def record_tokens(model: str, input_tokens: int, output_tokens: int, stage: str = "llm") -> float:
    TOKENS.labels(model, "input").inc(input_tokens)
    TOKENS.labels(model, "output").inc(output_tokens)
    usd = estimate_cost(model, input_tokens, output_tokens)
    COST.labels(stage, model).observe(usd)
    return usd


# ------------------------------------------------------------------------------- helpers
def hash_query(text: str) -> str:
    """Stable hash for logging a query without storing its text."""
    return "q:" + hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


def hash_user(user_id: str) -> str:
    return "u:" + hashlib.sha256(user_id.encode()).hexdigest()[:16]


@contextmanager
def stage_timer(stage: str, provider: str = "-", **fields: Any) -> Iterator[dict]:
    """Times a stage, records latency and errors, and yields a dict for extra fields.

    Anything the caller writes into the yielded dict is emitted on the completion log line, which
    keeps per-stage detail (chunks produced, candidates retrieved) attached to the timing rather
    than in a separate uncorrelated event.
    """
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield extra
    except Exception as exc:
        STAGE_ERRORS.labels(stage, provider, type(exc).__name__).inc()
        log.error(
            "stage_failed", stage=stage, provider=provider, error=type(exc).__name__, detail=str(exc)[:300], **fields
        )
        raise
    finally:
        elapsed = time.perf_counter() - started
        STAGE_LATENCY.labels(stage, provider).observe(elapsed)
        log.info("stage_complete", stage=stage, provider=provider, duration_s=round(elapsed, 3), **fields, **extra)


def should_online_eval(rate: float | None = None) -> bool:
    """Sampling gate for asynchronous production scoring."""
    from sentinel.core.config import settings

    return random.random() < (rate if rate is not None else settings.online_eval_sample_rate)


def trace_config(state: dict, node: str = "") -> dict:
    """LangChain/LangGraph callback config carrying trace identity.

    Kept in one place so every agent node tags traces identically; inconsistent tags are what turn
    a trace explorer into a pile of unrelated spans.
    """
    from sentinel.core.config import settings

    metadata = {
        "anomaly_id": state.get("anomaly_id", ""),
        "tenant_id": state.get("tenant_id", "default"),
        "node": node,
        "index_version": settings.index_version,
        "parse_provider": settings.parse_provider,
        "embed_provider": settings.embed_provider,
        "vectorstore_provider": settings.vectorstore_provider,
    }
    config: dict[str, Any] = {"metadata": metadata, "tags": [f"node:{node}"] if node else []}

    if settings.langfuse_public_key:
        try:
            from langfuse.langchain import CallbackHandler

            config["callbacks"] = [CallbackHandler()]
        except ImportError:
            pass
    return config
