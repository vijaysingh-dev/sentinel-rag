"""Pipeline behaviour: idempotency, degradation, budgets and fusion."""

from __future__ import annotations

import pytest

from sentinel.core.types import Chunk
from sentinel.retrieval.fusion import (
    dedupe_by_content,
    dedupe_by_parent,
    reciprocal_rank_fusion,
)
from sentinel.retrieval.pipeline import RetrievalPipeline


def test_chunk_ids_are_content_derived():
    """Identical text at the same position yields the same id, so re-ingestion is a no-op."""
    first = Chunk.make_id("doc1", 3, "the divisor is adjusted on the ex-date")
    second = Chunk.make_id("doc1", 3, "the divisor is adjusted on the ex-date")
    assert first == second


def test_chunk_id_changes_when_content_changes():
    original = Chunk.make_id("doc1", 3, "adjusted on the ex-date")
    edited = Chunk.make_id("doc1", 3, "adjusted on the record date")
    assert original != edited


def test_upsert_is_idempotent(store, embedder):
    """Re-writing the same chunks must not duplicate them."""
    before = len(store.records)
    chunks = list(store.records.values())
    store.upsert(embedder.embed_chunks(chunks))
    assert len(store.records) == before


def test_rrf_ranks_documents_appearing_in_both_arms_higher():
    from sentinel.core.types import RetrievedChunk

    def chunk(cid: str) -> Chunk:
        return Chunk(chunk_id=cid, doc_id=cid, embed_text=cid, context_text=cid, parent_id=cid)

    dense = [RetrievedChunk(chunk=chunk("a"), score=0.9), RetrievedChunk(chunk=chunk("b"), score=0.8)]
    sparse = [RetrievedChunk(chunk=chunk("c"), score=5.0), RetrievedChunk(chunk=chunk("a"), score=4.0)]

    fused = reciprocal_rank_fusion([dense, sparse])
    assert fused[0].chunk.chunk_id == "a"


def test_dedupe_by_parent_collapses_siblings():
    from sentinel.core.types import RetrievedChunk

    items = [
        RetrievedChunk(
            chunk=Chunk(chunk_id=f"c{i}", doc_id="d", embed_text="x", context_text="x", parent_id="section-1"),
            score=1.0 - i * 0.1,
        )
        for i in range(4)
    ]
    assert len(dedupe_by_parent(items)) == 1


def test_dedupe_by_content_drops_identical_text():
    from sentinel.core.types import RetrievedChunk

    items = [
        RetrievedChunk(
            chunk=Chunk(
                chunk_id=f"c{i}", doc_id="d", embed_text="x", context_text="x", parent_id=f"p{i}", content_hash="same"
            ),
            score=1.0,
        )
        for i in range(3)
    ]
    assert len(dedupe_by_content(items)) == 1


def test_retrieval_degrades_to_lexical_when_embedding_fails(store):
    """An embedding outage must produce lexical results, not an error.

    Exact-match queries are the ones lexical search handles best, so a meaningful share of traffic
    is unaffected — which is what makes this a real degraded mode rather than a token gesture.
    """

    class BrokenEmbedder:
        name = "broken"
        model = "broken"
        dims = 8

        def embed_query(self, text):
            raise RuntimeError("embedding service down")

        def embed_documents(self, texts):
            raise RuntimeError("embedding service down")

    pipeline = RetrievalPipeline(store=store, embedder=BrokenEmbedder(), reranker=None, use_cache=False)
    results = pipeline.retrieve("checkout runbook upstream dependencies", roles=["admin"])

    assert results.mode == "bm25_only"
    assert results.chunks


def test_context_budget_is_respected(store, embedder):
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=None, use_cache=False)
    results = pipeline.retrieve("checkout payment incident metrics", roles=["admin"], context_budget_tokens=12)

    total = sum(c.chunk.token_count for c in results.chunks)
    assert total <= 12


def test_budget_detects_repeated_identical_calls():
    from sentinel.core.resilience import Budget, BudgetExceeded

    budget = Budget()
    budget.record_call("search_knowledge", {"query": "checkout errors"})
    with pytest.raises(BudgetExceeded):
        budget.record_call("search_knowledge", {"query": "checkout errors"})


def test_live_tools_are_never_cacheable():
    from sentinel.core.cache import is_cacheable

    assert is_cacheable({"search_knowledge"})
    assert not is_cacheable({"search_knowledge", "current_flags"})
    assert not is_cacheable({"search_knowledge", "query_prometheus"})
    assert not is_cacheable({"kafka_lag"})


def test_index_version_changes_with_chunking_config(monkeypatch):
    """A config change that invalidates the index must change its fingerprint."""
    from sentinel.core.config import Settings

    base = Settings(chunk_size=550, embed_provider="local")
    changed = Settings(chunk_size=900, embed_provider="local")
    assert base.index_version != changed.index_version


class _DictCache:
    """In-memory stand-in for RetrievalCache — records puts so a test can assert what got cached."""

    def __init__(self):
        self.data: dict = {}
        self.puts = 0

    def get(self, query, scope, **extra):
        return self.data.get(query)

    def put(self, query, scope, payload, **extra):
        self.puts += 1
        self.data[query] = payload


class _PassReranker:
    name = "pass"

    def rerank(self, query, candidates, top_n, floor=None):
        return candidates[:top_n]


class _BrokenReranker:
    name = "broken"

    def rerank(self, query, candidates, top_n, floor=None):
        raise RuntimeError("reranker down")


def test_a_cache_hit_is_not_degraded(store, embedder):
    """Only full results are cached, so a hit replays a full result — it must not be flagged."""
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=_PassReranker(), use_cache=False)
    pipeline.cache = _DictCache()

    first = pipeline.retrieve("checkout runbook upstream dependencies", roles=["admin"])
    second = pipeline.retrieve("checkout runbook upstream dependencies", roles=["admin"])

    assert (first.mode, first.degraded) == ("full", False)
    assert (second.mode, second.cache_hit, second.degraded) == ("cache", True, False)


def test_a_rerank_failure_is_degraded_and_never_cached(store, embedder):
    """Unranked fallback results used to be labelled `full` — unflagged, and cached as if full."""
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=_BrokenReranker(), use_cache=False)
    pipeline.cache = _DictCache()

    results = pipeline.retrieve("checkout runbook upstream dependencies", roles=["admin"])

    assert results.chunks
    assert (results.mode, results.degraded) == ("rerank_failed", True)
    assert pipeline.cache.puts == 0


def test_search_knowledge_flags_degraded_results_but_not_cached_ones(monkeypatch):
    import asyncio

    from sentinel.core.types import Chunk, RetrievedChunk
    from sentinel.rca.runtime import request_context
    from sentinel.rca.tools import retrieval as tool_module
    from sentinel.retrieval.pipeline import RetrievalResult

    def run(mode):
        chunk = Chunk(chunk_id="d::0::x", doc_id="d", parent_id="d::root", embed_text="t", context_text="t")
        result = RetrievalResult(chunks=[RetrievedChunk(chunk=chunk, score=1.0)], query="q", mode=mode)
        monkeypatch.setattr(tool_module, "pipeline", lambda: type("P", (), {"retrieve": lambda *a, **k: result})())
        with request_context("default", ["admin"]):
            out = asyncio.run(tool_module.search_knowledge.ainvoke({"query": "q"}))
        return any("degraded" in str(item.get("note", "")) for item in out)

    assert run("cache") is False
    assert run("full") is False
    assert run("rerank_failed") is True
    assert run("bm25_only") is True
