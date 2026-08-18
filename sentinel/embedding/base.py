"""Embedder interface.

Three details in this file cause more silent quality loss in real systems than any tuning
decision, so they are handled once here rather than left to each adapter:

1. **Query/document asymmetry.** Models like BGE, E5 and Cohere Embed expect different treatment
   for a short query than for a long passage — an instruction prefix, or an `input_type`
   parameter. Getting it wrong costs several points of recall and produces no error at all. The
   base class makes `embed_query` and `embed_documents` separate methods so an adapter cannot
   accidentally treat them identically.

2. **Silent truncation.** Every embedding model has a maximum sequence length and quietly discards
   input beyond it. A 2000-token chunk sent to a 512-token model loses three quarters of its
   content with nothing in the logs. `_prepare` truncates deliberately and counts it.

3. **Caching by content hash.** Ingestion re-runs should cost nothing for unchanged text. The
   cache sits in the base class so every provider inherits it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sentinel.core.cache import EmbeddingCache
from sentinel.core.observability import log, stage_timer
from sentinel.core.types import Chunk, EmbeddedChunk


class BaseEmbedder(ABC):
    """Contract every embedding adapter implements."""

    name: str = "base"
    model: str = ""
    dims: int = 0
    max_input_tokens: int = 512
    supports_dimension_reduction: bool = False

    def __init__(self, use_cache: bool = True, batch_size: int = 64):
        self.batch_size = batch_size
        self._cache = EmbeddingCache(self.model) if use_cache else None
        self._truncations = 0

    # ------------------------------------------------------------------- provider hooks
    @abstractmethod
    def _embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Provider-specific call. Receives already-prepared text."""

    async def _aembed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        import asyncio

        return await asyncio.to_thread(self._embed_batch, texts, is_query)

    # ------------------------------------------------------------------- public surface
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embeds passages, using the cache and batching provider calls."""
        if not texts:
            return []

        prepared = [self._prepare(t, is_query=False) for t in texts]
        vectors: list[list[float] | None] = self._cache.get_many(prepared) if self._cache else [None] * len(prepared)

        missing_idx = [i for i, v in enumerate(vectors) if v is None]
        if missing_idx:
            missing_texts = [prepared[i] for i in missing_idx]
            fresh: list[list[float]] = []
            with stage_timer(
                "embedding", self.name, count=len(missing_texts), cached=len(prepared) - len(missing_texts)
            ):
                for start in range(0, len(missing_texts), self.batch_size):
                    fresh.extend(self._embed_batch(missing_texts[start : start + self.batch_size], is_query=False))
            for idx, vector in zip(missing_idx, fresh, strict=True):
                vectors[idx] = vector
            if self._cache:
                self._cache.put_many(missing_texts, fresh)

        return [v for v in vectors if v is not None]

    def embed_query(self, text: str) -> list[float]:
        """Embeds a single query.

        Separate from `embed_documents` because the provider treatment differs — see the module
        docstring. Queries are deliberately not cached here; the retrieval cache upstream already
        covers the repeated-query case at a more useful granularity.
        """
        return self._embed_batch([self._prepare(text, is_query=True)], is_query=True)[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        return await asyncio.to_thread(self.embed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        import asyncio

        return await asyncio.to_thread(self.embed_query, text)

    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        """Embeds `embed_text` — never `context_text`.

        Getting this backwards silently destroys the entire point of two-field chunks: you would
        embed the large context and lose the precision that sentence-window and parent-document
        strategies exist to provide.
        """
        vectors = self.embed_documents([c.embed_text for c in chunks])
        return [
            EmbeddedChunk(chunk=chunk, vector=vector, model=self.model, dims=self.dims)
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

    # ------------------------------------------------------------------------- helpers
    def _prepare(self, text: str, is_query: bool) -> str:
        """Applies the provider's prefix convention and enforces the length limit."""
        from sentinel.chunking.base import ntokens, truncate_tokens

        text = self._apply_prefix(text, is_query)
        if ntokens(text) > self.max_input_tokens:
            self._truncations += 1
            if self._truncations in (1, 10, 100, 1000):
                log.warning(
                    "embedding_truncated",
                    provider=self.name,
                    model=self.model,
                    limit=self.max_input_tokens,
                    total_truncations=self._truncations,
                )
            text = truncate_tokens(text, self.max_input_tokens)
        return text

    def _apply_prefix(self, text: str, is_query: bool) -> str:
        """Default: no prefix. Overridden by adapters whose models expect one."""
        return text

    @property
    def truncation_count(self) -> int:
        """Exposed so ingestion can report it; a high count means chunks are sized wrongly."""
        return self._truncations

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "dims": self.dims,
            "max_input_tokens": self.max_input_tokens,
            "supports_dimension_reduction": self.supports_dimension_reduction,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} model={self.model} dims={self.dims}>"
