"""Azure semantic reranking.

Azure AI Search's semantic ranker is a managed cross-encoder that runs as part of the search
request, not as a separate call. That is a genuine architectural difference from the other two
providers: there is nothing to host, nothing to batch, and no extra round trip — but you cannot
rerank candidates that did not come from Azure AI Search.

This adapter therefore has two modes:

* **Native (preferred).** The vector store already returned `@search.rerankerScore` because the
  query used `query_type="semantic"`. Reranking here is a no-op reorder, and this class exists
  mainly so the pipeline shape stays uniform across providers.
* **Standalone.** Candidates came from somewhere else (a benchmark, or a different store), so we
  re-query Azure restricted to those chunk ids to obtain semantic scores. Correct, but it costs an
  extra call — which is exactly why the native path is the default.

Scores are roughly 0-4, unrelated to the cosine scale, so the floor differs from the local
reranker's.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.core.types import RetrievedChunk
from sentinel.reranking.base import BaseReranker


class AzureSemanticReranker(BaseReranker):
    name = "azure"
    model = "azure-semantic-ranker"
    default_floor = 1.4  # ~0-4 scale; below this results are usually off-topic

    def __init__(self, store: Any = None):
        from sentinel.core.registry import get_provider

        self._store = store or get_provider("vectorstore", "azure")
        log.info("reranker_ready", provider=self.name, mode="native")

    def rerank(
        self, query: str, candidates: list[RetrievedChunk], top_n: int = 5, floor: float | None = None
    ) -> list[RetrievedChunk]:
        """Reorders using scores the search already produced, or fetches them if absent."""
        floor = self.default_floor if floor is None else floor
        if not candidates:
            return []

        if all(c.rerank_score is not None for c in candidates):
            ranked = sorted(candidates, key=lambda c: -(c.rerank_score or 0.0))
            out = []
            for rank, item in enumerate(ranked[:top_n]):
                if (item.rerank_score or 0.0) < floor:
                    continue
                item.rank = rank
                out.append(item)
            return out

        return self._standalone(query, candidates, top_n, floor)

    def _standalone(
        self, query: str, candidates: list[RetrievedChunk], top_n: int, floor: float
    ) -> list[RetrievedChunk]:
        """Re-queries Azure restricted to the candidate ids to obtain semantic scores."""
        ids = [c.chunk.chunk_id for c in candidates]
        id_filter = " or ".join(f"chunk_id eq '{cid}'" for cid in ids)

        results = self._store._client.search(
            search_text=query,
            filter=id_filter,
            query_type="semantic",
            semantic_configuration_name=self._store.semantic_config,
            top=top_n,
            select=["chunk_id"],
        )

        scores = {r["chunk_id"]: r.get("@search.rerankerScore", 0.0) for r in results}
        by_id = {c.chunk.chunk_id: c for c in candidates}

        out = []
        for rank, (cid, score) in enumerate(sorted(scores.items(), key=lambda kv: -kv[1])[:top_n]):
            if score < floor or cid not in by_id:
                continue
            item = by_id[cid]
            item.rerank_score = float(score)
            item.score = float(score)
            item.rank = rank
            out.append(item)
        return out

    def _score(self, query: str, texts: list[str]) -> list[float]:
        raise NotImplementedError("Azure reranking operates on indexed documents, not raw text")

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "mode": "native_in_search",
            "semantic_config": settings.azure_search_semantic_config,
        }
