"""`contextual` — not registered; wraps any other strategy.

Last in build order. Anthropic's contextual-retrieval technique: a cheap LLM writes one sentence
situating each chunk, prepended before embedding. Not in the registry because it costs one LLM
call per chunk — enable via `settings.contextual_chunking` and wrap the inner strategy explicitly.
"""

from __future__ import annotations

from typing import Any

from sentinel.chunking.base import BaseChunker
from sentinel.core.types import Chunk, ParsedDocument


class ContextualChunker(BaseChunker):
    """Wraps any strategy and prepends an LLM-written sentence situating each chunk.

    Anthropic's contextual-retrieval technique. For each chunk, a cheap model writes one or two
    sentences explaining where it sits in the document, and that is prepended before embedding.
    It recovers most of the context that splitting destroys — pronouns, implicit subjects,
    "as described above".

    What makes it affordable is prompt caching: the document occupies the static prefix of the
    prompt, so after the first chunk the provider charges roughly a tenth for those tokens. Without
    caching this is expensive enough to be impractical on a large corpus.

    Not registered by default because it costs one LLM call per chunk; enable via
    `settings.contextual_chunking` and wrap explicitly.
    """

    PROMPT = (
        "<document>\n{doc}\n</document>\n\n"
        "Here is a chunk from that document:\n<chunk>\n{chunk}\n</chunk>\n\n"
        "Give one short sentence situating this chunk within the document so it can be understood "
        "on its own in a search result. Output only that sentence."
    )

    def __init__(self, inner: BaseChunker, llm: Any = None, doc_char_limit: int = 40_000):
        self.inner = inner
        self.llm = llm
        self.doc_char_limit = doc_char_limit
        self.name = f"contextual_{inner.name}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        """Synchronous path returns un-enriched chunks; use `asplit` to actually contextualise."""
        return self.inner.split(doc)

    async def asplit(self, doc: ParsedDocument, max_concurrent: int = 8) -> list[Chunk]:
        import asyncio

        from sentinel.core.registry import get_provider

        chunks = self.inner.split(doc)
        llm = self.llm or get_provider("llm").cheap
        document = doc.markdown[: self.doc_char_limit]
        semaphore = asyncio.Semaphore(max_concurrent)

        async def enrich(chunk: Chunk) -> None:
            async with semaphore:
                try:
                    response = await llm.ainvoke(self.PROMPT.format(doc=document, chunk=chunk.embed_text[:2000]))
                    situating = (response.content or "").strip()
                    if situating:
                        chunk.embed_text = f"{situating}\n\n{chunk.embed_text}"
                        chunk.meta["situating_context"] = situating
                except Exception:
                    # Failing to contextualise one chunk must not fail ingestion; the chunk is
                    # still usable, just less retrievable.
                    pass

        await asyncio.gather(*(enrich(c) for c in chunks))
        return self._finalise(doc, chunks)
