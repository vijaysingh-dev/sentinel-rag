"""Error taxonomy, retries, circuit breakers and graceful degradation.

The organising idea, which is worth stating explicitly because conflating the two halves is the
most common design mistake in agent systems:

* **Infrastructure failures** (429, 503, timeout, connection refused) are handled *by code*.
  Retry with backoff, trip a breaker, degrade, alert. They raise.
* **Semantic failures** (no results, access denied, malformed tool arguments) are handled *by the
  model*. They return as structured observations telling it what to do next.

Raising on "no documents matched" kills a conversation the agent could have recovered from by
rephrasing. Feeding a 503 back to the model as text makes it retry a dead service until the step
cap trips, burning budget to learn nothing.
"""

from __future__ import annotations

import asyncio
import functools
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from sentinel.core.observability import DEGRADED_MODE, NODE_ERRORS, log

T = TypeVar("T")


# ------------------------------------------------------------------------- infrastructure
class SentinelError(Exception):
    """Base for infrastructure failures. Never rendered into a model prompt."""

    retryable: bool = False
    user_message: str = "Something went wrong. Please try again."


class ProviderTimeout(SentinelError):
    retryable = True
    user_message = "The service took too long to respond."


class RateLimited(SentinelError):
    """429. Providers usually send Retry-After; honour it over our own backoff."""

    retryable = True
    user_message = "The service is busy. Retrying."

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ServiceUnavailable(SentinelError):
    retryable = True

    def __init__(self, service: str, detail: str = ""):
        super().__init__(f"{service} unavailable: {detail}")
        self.service = service
        self.user_message = f"The {service} service isn't responding right now."


class ContentFiltered(SentinelError):
    """Provider-side safety filter. Retrying the identical prompt will not help."""

    retryable = False
    user_message = "That request couldn't be processed. Please rephrase."


class BudgetExceeded(SentinelError):
    retryable = False
    user_message = "This request needed more resources than allowed."


class ParseFailure(SentinelError):
    retryable = False


class AuthorizationViolation(SentinelError):
    """A document passed the search filter that the caller should not be able to see.

    Should never fire. It is an assertion wired to a page-worthy alert, not a control-flow path.
    """

    retryable = False


# ----------------------------------------------------------------------------- semantic
class Outcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    DENIED = "denied"
    DEGRADED = "degraded"
    INVALID_INPUT = "invalid_input"
    UNAVAILABLE = "unavailable"


@dataclass
class ToolResult:
    """Envelope for tool output that the model reads.

    Non-ok statuses carry an instruction rather than an error message, because the reader is a
    language model deciding what to do next, not a human reading a stack trace. "NO_RESULTS: do
    not answer from general knowledge" changes behaviour; "Error: empty result set" does not.
    """

    status: Outcome
    content: Any = None
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    _INSTRUCTIONS = {
        Outcome.EMPTY: (
            "NO_RESULTS: nothing in the documentation matched. Do not answer from general "
            "knowledge — say it is not documented and suggest escalating."
        ),
        Outcome.DENIED: (
            "ACCESS_DENIED: this information is not available to you. Do not describe what was "
            "restricted or confirm that it exists."
        ),
        Outcome.DEGRADED: (
            "PARTIAL_RESULTS: retrieval ran in a degraded mode, so results are less precise. "
            "Answer if you can, and state that confidence is lower than usual."
        ),
        Outcome.INVALID_INPUT: ("INVALID_INPUT: the arguments were rejected. Correct them and retry once."),
        Outcome.UNAVAILABLE: (
            "TOOL_UNAVAILABLE: this tool is down. Continue with other tools and tell the user "
            "which check could not be performed."
        ),
    }

    def to_model(self) -> Any:
        if self.status is Outcome.OK:
            return self.content
        instruction = self._INSTRUCTIONS[self.status]
        return f"{instruction} {self.detail}".strip()

    @classmethod
    def ok(cls, content: Any, **meta: Any) -> ToolResult:
        return cls(Outcome.OK, content, meta=meta)

    @classmethod
    def empty(cls, detail: str = "", **meta: Any) -> ToolResult:
        return cls(Outcome.EMPTY, None, detail, meta)

    @classmethod
    def denied(cls, detail: str = "", **meta: Any) -> ToolResult:
        return cls(Outcome.DENIED, None, detail, meta)


# ------------------------------------------------------------------------------- retry
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _classify(exc: Exception) -> tuple[bool, float | None]:
    """Maps a provider exception to (retryable, retry_after_seconds).

    Written defensively because each SDK reports status differently: botocore uses a nested
    response dict, the Azure SDK exposes `status_code`, and httpx wraps a response object.
    """
    if isinstance(exc, SentinelError):
        return exc.retryable, getattr(exc, "retry_after", None)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True, None

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
            if status is None and isinstance(response, dict):
                status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status is None:
        # google.api_core.exceptions.GoogleAPICallError (e.g. ServiceUnavailable for a 503) puts
        # the HTTP-equivalent status on `.code`, not `.status_code` — the one shape this generic
        # walk didn't already cover, and the one that hit production first.
        status = getattr(exc, "code", None)

    # botocore throttling surfaces by error code rather than status in several services.
    code = (
        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if isinstance(getattr(exc, "response", None), dict)
        else ""
    )
    if code in {"ThrottlingException", "TooManyRequestsException", "ProvisionedThroughputExceededException"}:
        return True, None

    retry_after = None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        try:
            retry_after = float(headers.get("retry-after") or headers.get("Retry-After") or 0) or None
        except (TypeError, ValueError):
            retry_after = None

    return (status in RETRYABLE_STATUS if status else False), retry_after


async def with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base: float = 0.5,
    cap: float = 10.0,
    operation: str = "",
) -> T:
    """Bounded retry with exponential backoff and full jitter.

    Jitter is not decoration: without it, N concurrent callers that fail together retry together
    and rebuild the exact traffic spike that caused the failure.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:
            retryable, retry_after = _classify(exc)
            last = exc
            if not retryable or attempt == attempts - 1:
                raise
            delay = retry_after or min(cap, base * (2**attempt)) * (0.5 + random.random())
            log.warning(
                "retrying", operation=operation, attempt=attempt + 1, error=type(exc).__name__, delay_s=round(delay, 2)
            )
            await asyncio.sleep(delay)
    raise last  # pragma: no cover - unreachable while attempts >= 1


class CircuitBreaker:
    """Stops hammering a dependency that is clearly down, then probes once after a cooldown.

    An open breaker is itself useful information in an operations assistant: "I cannot reach
    Prometheus, which may itself indicate a problem" is a better answer than a generic error, and
    better than a thirty-second wait for the same conclusion.
    """

    def __init__(self, name: str, threshold: int = 5, cooldown_s: int = 60):
        self.name = name
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at > self.cooldown_s:
            self.failures, self.opened_at = 0, None  # half-open: allow one probe
            return False
        return True

    def allow(self) -> bool:
        return not self.is_open

    def record(self, ok: bool) -> None:
        if ok:
            self.failures, self.opened_at = 0, None
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            log.error("breaker_opened", service=self.name, failures=self.failures)


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker(name: str, threshold: int = 5, cooldown_s: int = 60) -> CircuitBreaker:
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name, threshold, cooldown_s)
    return _BREAKERS[name]


def guarded(service: str, timeout_s: float = 30.0, fallback: Any = None):
    """Wraps an async call with a breaker, a timeout and an optional fallback value.

    When `fallback` is provided the call degrades to it instead of raising, and the degradation is
    counted as a metric — which is what catches a silent multi-day drift into a fallback path that
    nobody noticed because nothing errored.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            cb = breaker(service)
            if not cb.allow():
                DEGRADED_MODE.labels(service, "circuit_open").inc()
                if fallback is not None:
                    return fallback
                raise ServiceUnavailable(service, "circuit open")
            try:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_s)
                cb.record(ok=True)
                return result
            except Exception as exc:
                cb.record(ok=False)
                NODE_ERRORS.labels(service, type(exc).__name__).inc()
                if fallback is not None:
                    DEGRADED_MODE.labels(service, type(exc).__name__).inc()
                    log.warning("degraded_fallback", service=service, error=type(exc).__name__)
                    return fallback
                raise

        return wrapper

    return decorator


@dataclass
class Budget:
    """Per-run bounds on steps, tokens, money and wall clock.

    Loop detection on identical (tool, args) pairs is cheap and catches the most common agent
    pathology, which is re-issuing the same failing call until the step cap trips.
    """

    max_steps: int = 12
    max_tokens: int = 120_000
    max_usd: float = 2.50
    max_seconds: float = 300.0
    steps: int = 0
    tokens: int = 0
    usd: float = 0.0
    started: float = field(default_factory=time.monotonic)
    seen_calls: set[tuple[str, str]] = field(default_factory=set)

    def check(self) -> None:
        if self.steps >= self.max_steps:
            raise BudgetExceeded("step limit")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded("token limit")
        if self.usd >= self.max_usd:
            raise BudgetExceeded("cost limit")
        if time.monotonic() - self.started >= self.max_seconds:
            raise BudgetExceeded("time limit")

    def record_call(self, tool: str, args: dict) -> None:
        import json

        signature = (tool, json.dumps(args, sort_keys=True, default=str))
        if signature in self.seen_calls:
            raise BudgetExceeded(f"repeated identical call to {tool}")
        self.seen_calls.add(signature)
        self.steps += 1
