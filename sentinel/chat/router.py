"""Message classification: tool_call | rag | rca | refuse.

Rules first, LLM fallback — the same "cheap check before an expensive one" shape as the ACL
resolver's ordered rules (`sentinel/ingest/acl.py`) and ADR-style precedent elsewhere in this repo.
Refusal is one of the four routes, not a separate filter bolted on before or after classification:
that is what stops an expensive/capable model from ever seeing an out-of-scope message — only the
cheap rule pass and the cheap LLM-fallback pass ever see a refused message's text.

A rule-pass refusal is never returned on its own: `_DENY_PATTERNS` is a keyword list, not intent
detection, so it false-positives on genuine requests that merely contain a denylisted word (a
summary that mentions "the incident story", a question about a service literally named
`payment-story`). The cheap LLM fallback is asked to confirm every refusal before it reaches the
user — cheap because false-refusals are the expensive direction to get wrong (a paying user turned
away), while the opposite failure (an out-of-scope message reaching the LLM fallback because its
phrasing dodged the regex) is already contained by that same fallback's own system prompt.

The RCA clarifying-question short-circuit lives here too, not in `anomaly.py`, for the same
reason: if the router can tell a message is RCA-shaped ("why is checkout failing") but cannot
resolve which service it's about, asking immediately is strictly cheaper than synthesizing an
anomaly dict and running a doomed investigation — this is decided before `sentinel.rca.graph` is
ever touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sentinel.core.observability import log


class Route(StrEnum):
    TOOL_CALL = "tool_call"
    RAG = "rag"
    RCA = "rca"
    REFUSE = "refuse"


class ToolTarget(StrEnum):
    SYSTEM_HEALTH = "system_health"
    INCIDENT_STATUS = "incident_status"


@dataclass
class RouteDecision:
    route: Route
    confidence: float
    tool_target: ToolTarget | None = None
    inferred_service: str | None = None
    inferred_signal: Literal["latency_p95", "error_ratio"] | None = None
    clarifying_question: str | None = None
    reason: str = ""


# ----------------------------------------------------------------------------------- rule pass

# Deliberately conservative: catches the obvious, free cases from the brief's own example
# ("write python to reverse a list") without becoming the only refusal mechanism — the LLM
# fallback's system prompt is the actual backstop for anything phrased around this list.
_DENY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bwrite\s+(a\s+|some\s+)?(python|javascript|typescript|java|go|rust|code|script|function|program)\b",
        r"\b(poem|joke|recipe|essay|story)\b",
        r"\btranslate\b.*\b(to|into)\b",
        r"\b(reverse|sort|implement)\b.*\b(list|array|string|algorithm)\b",
    ]
]

_TOOL_HEALTH = re.compile(r"\b(status|health|healthy|up|down|running|alive)\b.*\b(system|platform|service)", re.I)
_TOOL_HEALTH_REV = re.compile(r"\b(system|platform|service)\b.*\b(status|health|healthy|up|down|running)\b", re.I)
_TOOL_INCIDENT = re.compile(r"\b(incident|scenario)s?\b.*\b(running|active|triggered|status)\b", re.I)

_RCA_TRIGGER = re.compile(
    r"(why is|why are|why.*(fail|error|down|slow|broken)|investigate|root.?cause|"
    r"what('s| is) wrong with|check (why|if|whether)|is .*\b(down|failing|broken)\b)",
    re.I,
)

_RAG_VOCAB = re.compile(
    r"\b(runbook|architecture|glossary|adr|postmortem|post-mortem|slo|capacity|otel|astroshop|"
    r"depends on|dependency|dependencies|how does|what does)\b",
    re.I,
)


_SERVICES_YAML = Path(__file__).resolve().parents[2] / "scripts" / "corpus" / "services.yaml"


@lru_cache
def _service_names() -> tuple[str, ...]:
    """Service names from the corpus's own domain model — one source of truth, not a duplicated list.

    Reads the raw YAML directly rather than importing `scripts/corpus/_model.py`: that module is
    script-tier tooling for the corpus generators, and `sentinel/chat` (library code) importing
    from `scripts/` would invert the dependency direction the repo layout otherwise keeps clean.
    """
    import yaml

    if not _SERVICES_YAML.exists():
        return ()
    spec = yaml.safe_load(_SERVICES_YAML.read_text(encoding="utf-8"))
    return tuple(spec.get("services", {}).keys())


def _find_service(text: str) -> str | None:
    lowered = text.lower()
    for name in _service_names():
        if name.replace("-", " ") in lowered or name in lowered:
            return name
    return None


def _find_signal(text: str) -> Literal["latency_p95", "error_ratio"] | None:
    if re.search(r"\b(slow|latency|latenc|delay|p95|p99)\b", text, re.I):
        return "latency_p95"
    if re.search(r"\b(fail|error|down|erroring|5xx|exception)\b", text, re.I):
        return "error_ratio"
    return None


def _rule_pass(text: str) -> RouteDecision | None:
    # Not returned directly — a denylist keyword hit is only ever a *candidate* refusal; classify()
    # confirms it against the cheap LLM fallback before it can reach the user, since a keyword list
    # can't tell "write a python script" from "what's the postmortem story on the python service".
    if any(p.search(text) for p in _DENY_PATTERNS):
        return RouteDecision(Route.REFUSE, 1.0, reason="deny_list")

    if _TOOL_INCIDENT.search(text):
        return RouteDecision(Route.TOOL_CALL, 1.0, tool_target=ToolTarget.INCIDENT_STATUS, reason="rule:incident")
    if _TOOL_HEALTH.search(text) or _TOOL_HEALTH_REV.search(text):
        return RouteDecision(Route.TOOL_CALL, 1.0, tool_target=ToolTarget.SYSTEM_HEALTH, reason="rule:health")

    if _RCA_TRIGGER.search(text):
        service = _find_service(text)
        if service:
            return RouteDecision(
                Route.RCA, 1.0, inferred_service=service, inferred_signal=_find_signal(text), reason="rule:rca"
            )
        return RouteDecision(
            Route.RCA,
            1.0,
            clarifying_question="Which service is this about?",
            reason="rule:rca_no_service",
        )

    if _RAG_VOCAB.search(text):
        return RouteDecision(Route.RAG, 1.0, reason="rule:rag_vocab")

    service = _find_service(text)
    if service:
        signal = _find_signal(text)
        if signal:
            return RouteDecision(Route.RCA, 1.0, inferred_service=service, inferred_signal=signal, reason="rule:rca")
        return RouteDecision(Route.RAG, 1.0, reason="rule:rag_vocab")

    return None


# ------------------------------------------------------------------------------ LLM fallback


class RouteFallbackSchema(BaseModel):
    """Structured output for the LLM classification fallback — mirrors `RouteDecision`'s fields."""

    route: Literal["tool_call", "rag", "rca", "refuse"]
    confidence: float = Field(ge=0, le=1)
    tool_target: Literal["system_health", "incident_status"] | None = None
    inferred_service: str | None = None
    inferred_signal: Literal["latency_p95", "error_ratio"] | None = None
    clarifying_question: str | None = None
    reason: str = Field(max_length=200)


@lru_cache
def _fallback_classifier():
    from sentinel.core.registry import get_provider

    return get_provider("llm").structured(RouteFallbackSchema, cheap=True)


async def _llm_fallback(text: str, recent_context: str) -> RouteDecision:
    from sentinel.chat.prompts import load_prompt
    from sentinel.core.config import settings
    from sentinel.rca.guard import structured_with_repair

    prompt = load_prompt("router_fallback", "v1").format(recent_context=recent_context or "(none)", message=text)
    result: RouteFallbackSchema = await structured_with_repair(_fallback_classifier(), prompt)

    decision = RouteDecision(
        route=Route(result.route),
        confidence=result.confidence,
        tool_target=ToolTarget(result.tool_target) if result.tool_target else None,
        inferred_service=result.inferred_service,
        inferred_signal=result.inferred_signal,
        clarifying_question=result.clarifying_question,
        reason=f"llm:{result.reason}",
    )

    if decision.confidence < settings.chat_router_confidence_floor:
        log.info("chat_router_low_confidence", confidence=decision.confidence, original_route=decision.route)
        return RouteDecision(Route.REFUSE, decision.confidence, reason="low_confidence")

    return decision


async def classify(text: str, recent_context: str = "") -> RouteDecision:
    decision = _rule_pass(text)

    # A confident non-refusal match is trusted outright — that's the whole point of the rule pass
    # being cheap. A refusal (or no match at all) is never trusted on its own: both get a second
    # opinion from the LLM fallback before the user sees a refusal, so a regex false-positive on a
    # denylisted word doesn't turn away a genuine request.
    if decision is not None and decision.route is not Route.REFUSE:
        log.info("chat_router_decision", route=decision.route, reason=decision.reason, via="rule")
        return decision

    llm_decision = await _llm_fallback(text, recent_context)

    if decision is not None and llm_decision.route is not Route.REFUSE:
        # The rule pass wanted to refuse but the LLM disagrees — trust the LLM's read of intent
        # over the keyword match that triggered the refusal in the first place.
        log.info(
            "chat_router_decision",
            route=llm_decision.route,
            reason=f"rule_refusal_overturned:{llm_decision.reason}",
            via="llm",
        )
        return llm_decision

    if decision is not None:
        # Rule pass said refuse, LLM fallback agrees — confirmed refusal.
        log.info("chat_router_decision", route=decision.route, reason=f"confirmed:{decision.reason}", via="rule+llm")
        return decision

    log.info("chat_router_decision", route=llm_decision.route, reason=llm_decision.reason, via="llm")
    return llm_decision
