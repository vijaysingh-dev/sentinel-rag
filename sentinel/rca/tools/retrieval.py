"""Knowledge retrieval tool.

This is the seam where the RCA agent meets the RAG pipeline. The original Sentinel implementation
called PGVector and CohereRerank directly; it now calls `RetrievalPipeline`, so the agent runs
unchanged whichever embedding and reranking provider is configured (local, or the openai/cohere
escape hatches — ADR-029). Nothing in the agent code knows which.

The docstring of `search_knowledge` is the tool description the model reads when deciding whether
to search and with which `doc_type`, so every doc type in `knowledge/` has to be named there with
what it answers — a type the model has never been told about is a type it never filters to.

Two properties preserved from the original and worth keeping:

* **Scope comes from the request context, never from tool input.** The model cannot widen its own
  access by writing a different tenant into a tool argument.
* **Errors return as observations, not exceptions.** A retrieval failure tells the specialist what
  it could not check, so the investigation continues degraded rather than dying.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_core.tools import tool

from sentinel.core.observability import TOOL_CALLS, log
from sentinel.rca.runtime import current_components, current_roles, current_tenant, fixture_lookup
from sentinel.retrieval.pipeline import RetrievalPipeline


@lru_cache
def pipeline() -> RetrievalPipeline:
    """Cached because constructing the store, embedder and reranker is expensive.

    Providers are resolved from settings at first use, so switching clouds is a config change and
    a restart rather than a code change.
    """
    return RetrievalPipeline()


def _format(results: Any) -> list[dict]:
    """Renders retrieval output for the model.

    `ref` is the citation handle the verifier checks against — it must round-trip to a real chunk
    id, which is what makes fabricated citations mechanically detectable.
    """
    return [
        {
            "ref": item.ref,
            "title": item.chunk.heading_path or item.chunk.source_uri,
            "doc_type": item.chunk.doc_type,
            "effective_date": item.chunk.effective_date,
            "score": round(item.score, 4),
            "text": item.chunk.context_text[:1500],
        }
        for item in results.chunks
    ]


@tool
async def search_knowledge(query: str, doc_type: str | None = None) -> list[dict]:
    """Search the Astroshop knowledge base: runbooks, past incidents, design decisions, and the
    platform reference (what services exist, how they connect, ports, data stores, metric names
    and labels, fault flags, the incident-generator and system-monitor).

    Search by symptom and by mechanism separately; they retrieve different documents. Cite the
    returned `ref` for any claim you base on a result.

    Args:
        query: Natural-language description of what you need, for example
            'checkout errors at the payment step', 'which services call product-catalog', or
            'which label selects a service in container memory metrics'.
        doc_type: Optional filter:
            'runbook' — diagnosis and mitigation for one service, its PromQL and known faults;
            'incident' — past post-mortems, to check whether this has happened before;
            'platform' — the platform reference: service inventory, call order, blast radius,
              deployment, metric names and selectors, every fault flag, health-rollup rules;
            'adr' — why something is built the way it is;
            'architecture', 'glossary', 'oncall', 'reference' — diagrams, terms, escalation,
              metrics catalog.
            Omit to search everything.
    """
    recorded = fixture_lookup("search_knowledge")
    if recorded is not None:
        TOOL_CALLS.labels("search_knowledge", "fixture").inc()
        # A fixture records either one result list for every query, or a dict keyed by doc_type.
        # Every fixture on disk is the first shape, and calling .get() on it raised — so each
        # `make eval-agent` replay ran its knowledge specialist degraded.
        if isinstance(recorded, dict):
            return recorded.get(doc_type or "any", recorded.get("default", []))
        return recorded

    try:
        results = pipeline().retrieve(
            query,
            roles=current_roles(),
            tenant_id=current_tenant(),
            components=current_components() or None,
            doc_type=doc_type,
        )
    except Exception as exc:
        # Returned rather than raised: the specialist should carry on with its other tools and
        # report what it could not check.
        TOOL_CALLS.labels("search_knowledge", "error").inc()
        log.warning("search_knowledge_failed", error=type(exc).__name__)
        return [
            {
                "error": "knowledge base unavailable",
                "instruction": "Continue with other tools and state that documentation could not be searched.",
            }
        ]

    if not results.chunks:
        TOOL_CALLS.labels("search_knowledge", "empty").inc()
        return [
            {
                "note": "no matching documentation",
                "query": query,
                "instruction": "Do not answer from general knowledge. State that this is not documented.",
            }
        ]

    TOOL_CALLS.labels("search_knowledge", "ok").inc()
    payload = _format(results)

    if results.degraded:
        # Degradation is surfaced to the model so the eventual draft can be honest about it.
        payload.append(
            {
                "note": f"retrieval ran in degraded mode ({results.mode})",
                "instruction": "Treat these results as less precise than usual and say so.",
            }
        )
    return payload
