"""Node-level failure containment for the agent graph.

The governing rule: **no single dependency failure may end an investigation.** A specialist that
times out, trips its breaker or raises hands control back to the supervisor with the run marked
degraded, and the analyst is told which check did not complete.

This is a different layer from `core/resilience.py`, not a duplicate of it: `core/resilience.py`
handles provider-call retries, circuit breakers and the error taxonomy in general, HTTP-shaped
terms (any SDK call, any service). This module is LangGraph-specific — it wraps a graph *node* and
speaks `Command`, the vocabulary the graph itself understands, building on the primitives
`core/resilience.py` exports (`CircuitBreaker`, `with_retry`) rather than re-implementing them.
"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any

from langgraph.types import Command

from sentinel.core.config import settings
from sentinel.core.observability import NODE_ERRORS, NODE_LATENCY, log
from sentinel.core.resilience import CircuitBreaker, with_retry

BREAKERS: dict[str, CircuitBreaker] = {
    name: CircuitBreaker(name, settings.breaker_threshold, settings.breaker_cooldown_s)
    for name in ("knowledge", "metrics", "tracing", "change")
}


def _degraded(name: str, kind: str, detail: str = "") -> Command:
    """Returns control to the supervisor with the failure recorded.

    `completed_agents` is appended even on failure, so the supervisor does not immediately re-run
    a specialist whose dependency is down and burn the step budget rediscovering that.
    """
    return Command(
        goto="supervisor",
        update={
            "degraded": True,
            "completed_agents": [name],
            "errors": [{"node": name, "kind": kind, "detail": detail[:400]}],
        },
    )


def with_node_guard(name: str, timeout_s: int | None = None):
    """Wraps a specialist node with a timeout, a circuit breaker and graceful degradation."""

    limit = timeout_s or settings.node_timeout_s

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(state: Any) -> Command:
            breaker = BREAKERS[name]
            if not breaker.allow():
                NODE_ERRORS.labels(name, "circuit_open").inc()
                log.warning("breaker_open", node=name, anomaly=state.get("anomaly_id"))
                return _degraded(name, "circuit_open", "dependency unavailable")

            started = time.monotonic()
            try:
                result = await asyncio.wait_for(fn(state), timeout=limit)
                breaker.record(ok=True)
                return result
            except asyncio.TimeoutError:
                breaker.record(ok=False)
                NODE_ERRORS.labels(name, "timeout").inc()
                log.warning("node_timeout", node=name, anomaly=state.get("anomaly_id"))
                return _degraded(name, "timeout", f"exceeded {limit}s")
            except Exception as exc:
                breaker.record(ok=False)
                NODE_ERRORS.labels(name, type(exc).__name__).inc()
                log.exception("node_failed", node=name, anomaly=state.get("anomaly_id"))
                return _degraded(name, type(exc).__name__, str(exc))
            finally:
                NODE_LATENCY.labels(name).observe(time.monotonic() - started)

        return wrapper

    return decorator


async def structured_with_repair(runnable: Any, prompt: str, attempts: int = 2) -> Any:
    """Feeds a schema violation back to the model once before giving up.

    Showing the model its own validation error fixes the large majority of malformed outputs, at
    the cost of one cheap call instead of a failed node. Each attempt goes through `with_retry` so
    a transient 503/429/timeout on the provider side is retried a couple of times before it is
    allowed to count as a schema-repair attempt — otherwise a flaky provider burns through
    `attempts` on connectivity alone and never gets the model's own validation error back to it.
    """
    from pydantic import ValidationError

    messages: list = [("user", prompt)]
    last: Exception | None = None

    for attempt in range(attempts):
        try:
            return await with_retry(lambda: runnable.ainvoke(messages), operation="structured_with_repair")
        except ValidationError as exc:
            last = exc
            if attempt == attempts - 1:
                break
            messages.append(
                (
                    "user",
                    f"Your previous output failed validation:\n{exc}\n"
                    "Return corrected output matching the schema exactly.",
                )
            )
    raise last  # type: ignore[misc]


def budget_exceeded(state: Any) -> str | None:
    """Checked before the routing call so an exhausted run does not spend more to notice."""
    if state.get("step_count", 0) > settings.max_steps:
        return "step_cap"
    if state.get("usd_spent", 0.0) > settings.max_usd_per_run:
        return "cost_cap"
    return None
