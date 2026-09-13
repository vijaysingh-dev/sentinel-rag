"""Plain OpenAI embeddings (api.openai.com) — the one non-local embed_provider.

Not Azure OpenAI: no deployment indirection, no managed identity, a model id goes straight in
`model=`. Kept minimal on purpose — this is the escape hatch for when local embedding quality
isn't enough, not a second maintained cloud path (ADR-029). If you need Azure OpenAI or Bedrock
embeddings back, `.archive/providers/azure_embedder.py` / `aws_embedder.py` have the real thing.

The `text-embedding-3-*` family supports **Matryoshka truncation**: the models are trained so the
first N dimensions are usable on their own. Passing `dimensions=512` to a 3072-dimension model
returns a shorter vector that keeps most of the quality — a real lever since index size and query
latency both scale with dimensionality. `settings.openai_embedding_dims` picks the default; pass
`dims=` to override per instance (used by `benchmarks/embedding_bakeoff.py`).
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log, record_tokens
from sentinel.core.resilience import RateLimited, with_retry
from sentinel.embedding.base import BaseEmbedder

MODEL_DIMS = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
}
MATRYOSHKA_CAPABLE = {"text-embedding-3-large", "text-embedding-3-small"}


class OpenAIEmbedder(BaseEmbedder):
    name = "openai"
    max_input_tokens = 8191  # generous; chunk size is the binding constraint, not this

    def __init__(
        self,
        model: str | None = None,
        dims: int | None = None,
        use_cache: bool = True,
        batch_size: int = 128,  # the API accepts large batches; fewer round trips
    ):
        from openai import OpenAI

        self.model = model or settings.openai_embedding_model
        base_dims = MODEL_DIMS.get(self.model, settings.openai_embedding_dims)

        # Only request dimension reduction when the model actually supports it; ada-002 rejects
        # the parameter outright rather than ignoring it.
        self.supports_dimension_reduction = any(m in self.model for m in MATRYOSHKA_CAPABLE)
        self.dims = dims if (dims and self.supports_dimension_reduction) else base_dims
        self._request_dims = dims if (dims and self.supports_dimension_reduction) else None

        self._client = OpenAI(api_key=settings.openai_api_key)

        super().__init__(use_cache=use_cache, batch_size=batch_size)
        log.info(
            "embedder_ready",
            provider=self.name,
            model=self.model,
            dims=self.dims,
            truncated=bool(self._request_dims),
        )

    def _embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        from openai import RateLimitError

        kwargs: dict[str, Any] = {"model": self.model, "input": texts}
        if self._request_dims:
            kwargs["dimensions"] = self._request_dims

        try:
            response = self._client.embeddings.create(**kwargs)
        except RateLimitError as exc:
            # Surfaced as our own type so the shared retry policy handles it uniformly across
            # providers rather than each adapter inventing its own backoff.
            raise RateLimited(str(exc)) from exc

        if response.usage:
            record_tokens(self.model, response.usage.prompt_tokens, 0, stage="embedding")

        # The API does not guarantee response order matches input order; sort by index.
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    async def _aembed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        import asyncio

        async def call() -> list[list[float]]:
            return await asyncio.to_thread(self._embed_batch, texts, is_query)

        return await with_retry(call, attempts=4, operation="openai_embed")

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "matryoshka": bool(self._request_dims)}
