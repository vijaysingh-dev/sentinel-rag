"""Cohere reranking (api.cohere.com) — the one non-local rerank_provider.

Cohere's `rerank` endpoint is a hosted cross-encoder: send a query and a list of documents, get
back a relevance score per document in `[0, 1]`, normalised the same way regardless of model
version. That makes the floor directly interpretable, unlike the local cross-encoder's unbounded
logits (see `reranking/local_reranker.py`).

Two things worth knowing before reaching for this over the local reranker:

* **It's a network call per query**, so it adds real latency to every retrieval, not just
  ingestion. The local cross-encoder scores ~50 pairs in 50-150ms on CPU with no round trip.
* **`max_chunks_per_doc`-style truncation is Cohere's, not ours.** Very long candidate texts get
  truncated by the API itself before scoring; nothing here re-implements that.

Kept intentionally as one adapter, not "the cloud reranking path" the way Azure/Bedrock used to
be (ADR-029) — if you need those back, `.archive/providers/azure_reranker.py` /
`aws_reranker.py` have the real thing.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.core.resilience import RateLimited, with_retry
from sentinel.reranking.base import BaseReranker


class CohereReranker(BaseReranker):
    name = "cohere"
    default_floor = 0.02  # normalised [0,1]; well below this is noise

    def __init__(self, model: str | None = None):
        import cohere

        self.model = model or settings.cohere_rerank_model
        self._client = cohere.ClientV2(api_key=settings.cohere_api_key)
        log.info("reranker_ready", provider=self.name, model=self.model)

    def _score(self, query: str, texts: list[str]) -> list[float]:
        from cohere.core.api_error import ApiError

        try:
            response = self._client.rerank(model=self.model, query=query, documents=texts)
        except ApiError as exc:
            if exc.status_code == 429:
                raise RateLimited(str(exc)) from exc
            raise

        # Results come back sorted by relevance with an `index` pointing at the input position;
        # scores are re-projected onto input order so callers can zip them against `texts` directly.
        scores = [0.0] * len(texts)
        for result in response.results:
            scores[result.index] = result.relevance_score
        return scores

    async def arerank(
        self, query: str, candidates: Any, top_n: int = 5, floor: float | None = None
    ) -> Any:
        import asyncio

        async def call() -> Any:
            return await asyncio.to_thread(self.rerank, query, candidates, top_n, floor)

        return await with_retry(call, attempts=3, operation="cohere_rerank")

    def describe(self) -> dict[str, Any]:
        return {**super().describe()}
