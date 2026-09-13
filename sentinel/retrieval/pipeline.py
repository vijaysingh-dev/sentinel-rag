"""The retrieval pipeline: query in, ranked context out.

This is where the stages compose:

    query
      -> rewrite (multi-turn only)
      -> cache lookup (role-scoped)
      -> hybrid search: BM25 + vector, fused by RRF          [wide, recall-oriented, k=50]
      -> cross-encoder rerank                                 [narrow, precision-oriented, n=6]
      -> relevance floor
      -> dedupe by parent / content
      -> optional MMR for diversity
      -> assemble within a token budget

Two design points worth stating explicitly:

**Recall is stage one's job; precision is stage two's.** They have different failure modes and
different fixes. Measure recall@50 alongside recall@5: if recall@50 is high and recall@5 is low,
the problem is ranking and a reranker fixes it. If recall@50 is low, no reranker will help and you
must fix candidate generation, chunking, or parsing.

**Security context is not optional.** `SearchFilter.require_scope()` raises on an empty role set
rather than producing an empty filter clause that would match the entire corpus.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sentinel.core.cache import RetrievalCache, role_scope
from sentinel.core.config import settings
from sentinel.core.observability import DEGRADED_MODE, RETRIEVAL_SCORE, hash_query, log
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.core.resilience import AuthorizationViolation, ServiceUnavailable
from sentinel.core.types import Chunk, RetrievedChunk
from sentinel.retrieval.fusion import (
    dedupe_by_content,
    dedupe_by_parent,
    maximal_marginal_relevance,
)
from sentinel.vectorstore.base import SearchFilter


@dataclass
class RetrievalResult:
    """Everything the caller needs, including how the answer was obtained.

    `mode` propagates the degradation path so the answer can be flagged as lower confidence and,
    more importantly, so a silent multi-day drift into `bm25_only` shows up as a metric rather than
    as an unexplained quality complaint.
    """

    chunks: list[RetrievedChunk]
    query: str
    rewritten_query: str | None = None
    mode: str = "full"
    latency_ms: dict[str, float] = field(default_factory=dict)
    candidates_considered: int = 0
    cache_hit: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def context(self) -> str:
        """Renders retrieved chunks for a prompt, with citation handles."""
        return "\n\n---\n\n".join(
            f"[{c.chunk.chunk_id}] {c.chunk.heading_path or c.chunk.source_uri}\n{c.chunk.context_text}"
            for c in self.chunks
        )

    @property
    def refs(self) -> list[str]:
        return [c.ref for c in self.chunks]


class RetrievalPipeline:
    def __init__(
        self,
        store: Any = None,
        embedder: Any = None,
        reranker: Any = None,
        use_cache: bool = True,
    ):
        self.store = store or get_provider("vectorstore")
        self.embedder = embedder or get_provider("embedder")
        self._reranker = reranker
        self._reranker_resolved = reranker is not None
        self.cache = RetrievalCache() if use_cache else None

    @property
    def reranker(self) -> Any:
        """Resolved lazily so a missing reranker degrades the pipeline instead of failing startup."""
        if not self._reranker_resolved:
            try:
                self._reranker = get_provider("reranker")
            except ProviderUnavailable as exc:
                log.warning("reranker_unavailable", error=str(exc)[:200])
                self._reranker = None
            self._reranker_resolved = True
        return self._reranker

    # ---------------------------------------------------------------------------- search
    def retrieve(
        self,
        query: str,
        roles: list[str],
        *,
        tenant_id: str = "default",
        doc_type: str | None = None,
        components: list[str] | None = None,
        k: int | None = None,
        top_n: int | None = None,
        use_mmr: bool | None = None,
        context_budget_tokens: int = 4000,
    ) -> RetrievalResult:
        k = k or settings.retrieve_k
        top_n = top_n or settings.rerank_top_n
        use_mmr = settings.use_mmr if use_mmr is None else use_mmr
        timings: dict[str, float] = {}

        filters = SearchFilter(
            tenant_id=tenant_id,
            roles=roles,
            components=components or [],
            doc_type=doc_type,
            index_version=settings.index_version,
        )
        filters.require_scope()

        scope = role_scope(roles)
        if self.cache:
            cached = self.cache.get(query, scope, tenant=tenant_id, doc_type=doc_type, k=k, top_n=top_n)
            if cached is not None:
                return RetrievalResult(
                    chunks=[_rehydrate(c) for c in cached],
                    query=query,
                    mode="cache",
                    cache_hit=True,
                )

        candidates, mode, timings = self._search(query, filters, k, timings)
        if not candidates:
            return RetrievalResult(chunks=[], query=query, mode=mode, latency_ms=timings)

        # Defence in depth: the filter should have made this impossible, but a mis-tagged document
        # or a filter bug is exactly the kind of thing that must page someone rather than silently
        # leak. This assertion should never fire.
        self._assert_authorized(candidates, roles, components or [])

        results, timings = self._rank(query, candidates, top_n, mode, timings)

        results = dedupe_by_parent(dedupe_by_content(results))

        if use_mmr and len(results) > 1:
            vectors = {c.chunk.chunk_id: self.embedder.embed_query(c.chunk.embed_text) for c in results}
            results = maximal_marginal_relevance(results, self.embedder.embed_query(query), vectors, k=top_n)

        results = _fit_budget(results, context_budget_tokens)

        if results:
            RETRIEVAL_SCORE.labels(self.store.name).observe(results[0].score)

        if self.cache and mode == "full":
            self.cache.put(
                query, scope, [_dehydrate(c) for c in results], tenant=tenant_id, doc_type=doc_type, k=k, top_n=top_n
            )

        log.info(
            "retrieval",
            query_hash=hash_query(query),
            roles=sorted(roles),
            mode=mode,
            candidates=len(candidates),
            returned=len(results),
            top_score=round(results[0].score, 4) if results else 0.0,
            latency_ms={k_: round(v, 1) for k_, v in timings.items()},
            index_version=settings.index_version,
        )

        return RetrievalResult(
            chunks=results,
            query=query,
            mode=mode,
            latency_ms=timings,
            candidates_considered=len(candidates),
        )

    # ------------------------------------------------------------------ degradation ladder
    def _search(
        self, query: str, filters: SearchFilter, k: int, timings: dict[str, float]
    ) -> tuple[list[RetrievedChunk], str, dict[str, float]]:
        """Hybrid, falling back to keyword-only when embeddings are unavailable.

        BM25-only is a genuinely useful degraded mode rather than a token gesture: exact-match
        queries (error codes, incident ids, version numbers) are the ones it handles *best*, so a
        meaningful fraction of traffic is unaffected.
        """
        started = time.perf_counter()
        try:
            vector = self.embedder.embed_query(query)
            timings["embed"] = (time.perf_counter() - started) * 1000
        except Exception as exc:
            DEGRADED_MODE.labels("retrieval", "bm25_only").inc()
            log.warning("embedding_unavailable", error=str(exc)[:200])
            started = time.perf_counter()
            try:
                results = self.store.sparse_search(query, k, filters)
            except Exception as inner:
                raise ServiceUnavailable("vectorstore", str(inner)) from inner
            timings["search"] = (time.perf_counter() - started) * 1000
            return results, "bm25_only", timings

        started = time.perf_counter()
        try:
            results = self.store.hybrid_search(query, vector, k, filters)
            mode = "full"
        except Exception as exc:
            log.warning("hybrid_failed_falling_back", error=str(exc)[:200])
            DEGRADED_MODE.labels("retrieval", "dense_only").inc()
            try:
                results = self.store.dense_search(vector, k, filters)
                mode = "dense_only"
            except Exception as inner:
                raise ServiceUnavailable("vectorstore", str(inner)) from inner
        timings["search"] = (time.perf_counter() - started) * 1000
        return results, mode, timings

    def _rank(
        self, query: str, candidates: list[RetrievedChunk], top_n: int, mode: str, timings: dict[str, float]
    ) -> tuple[list[RetrievedChunk], dict[str, float]]:
        reranker = self.reranker
        if reranker is None:
            DEGRADED_MODE.labels("retrieval", "no_rerank").inc()
            return candidates[:top_n], timings

        started = time.perf_counter()
        try:
            results = reranker.rerank(query, candidates, top_n=top_n, floor=settings.min_rerank_score or None)
        except Exception as exc:
            DEGRADED_MODE.labels("retrieval", "rerank_failed").inc()
            log.warning("rerank_failed", error=str(exc)[:200])
            return candidates[:top_n], timings
        timings["rerank"] = (time.perf_counter() - started) * 1000
        return results, timings

    @staticmethod
    def _assert_authorized(results: list[RetrievedChunk], roles: list[str], components: list[str]) -> None:
        role_set = set(roles)
        component_set = set(components)
        for item in results:
            chunk_roles = set(item.chunk.allowed_roles or [])
            if chunk_roles and not (chunk_roles & role_set):
                log.critical(
                    "acl_violation",
                    chunk_id=item.chunk.chunk_id,
                    doc_roles=sorted(chunk_roles),
                    caller_roles=sorted(role_set),
                )
                raise AuthorizationViolation(item.chunk.chunk_id)
            chunk_components = set(item.chunk.components or [])
            if chunk_components and component_set and not (chunk_components & component_set):
                log.critical("acl_component_violation", chunk_id=item.chunk.chunk_id)
                raise AuthorizationViolation(item.chunk.chunk_id)


def _fit_budget(results: list[RetrievedChunk], budget_tokens: int) -> list[RetrievedChunk]:
    """Greedily keeps the highest-ranked chunks that fit the context budget.

    Context assembly is a knapsack against a hard limit, not "take the top five and hope". A single
    oversized parent-document chunk can otherwise consume the entire budget alone.
    """
    from sentinel.chunking.base import ntokens

    kept: list[RetrievedChunk] = []
    used = 0
    for item in results:
        cost = item.chunk.token_count or ntokens(item.chunk.context_text)
        if used + cost > budget_tokens:
            continue
        kept.append(item)
        used += cost
    return kept


def _dehydrate(item: RetrievedChunk) -> dict:
    chunk = item.chunk
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "parent_id": chunk.parent_id,
        "embed_text": chunk.embed_text,
        "context_text": chunk.context_text,
        "heading_path": chunk.heading_path,
        "doc_type": chunk.doc_type,
        "tenant_id": chunk.tenant_id,
        "source_uri": chunk.source_uri,
        "allowed_roles": chunk.allowed_roles,
        "components": chunk.components,
        "token_count": chunk.token_count,
        "score": item.score,
        "rerank_score": item.rerank_score,
        "rank": item.rank,
    }


def _rehydrate(data: dict) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id=data["chunk_id"],
            doc_id=data["doc_id"],
            parent_id=data["parent_id"],
            embed_text=data["embed_text"],
            context_text=data["context_text"],
            heading_path=data.get("heading_path", ""),
            doc_type=data.get("doc_type", "page"),
            tenant_id=data.get("tenant_id", "default"),
            source_uri=data.get("source_uri", ""),
            allowed_roles=data.get("allowed_roles", []),
            components=data.get("components", []),
            token_count=data.get("token_count", 0),
        ),
        score=data.get("score", 0.0),
        rerank_score=data.get("rerank_score"),
        rank=data.get("rank", 0),
        retriever="cache",
    )
