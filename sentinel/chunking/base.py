"""Chunker interface and registry.

Chunking exists to resolve a conflict between two consumers of the same text:

* The **retriever** wants small, single-topic units. A vector averaging five topics is close to
  nothing in embedding space.
* The **generator** wants enough surrounding context to answer — the definition, the preceding
  setup, the table header, the antecedent of "it".

The resolution is the central idea of this package, and it is why `Chunk` carries two text fields:
**the retrieval unit and the generation unit do not have to be the same thing.** Embed something
small and specific; return something larger and self-contained. Sentence-window, parent-document
and contextual strategies are all variations on that one move.

Everything registered here is swappable by name so `benchmarks/chunking_sweep.py` can enumerate
strategies and produce a leaderboard rather than requiring a code change per experiment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from sentinel.core.types import Chunk, ParsedDocument


class _ApproximateEncoder:
    """Character-ratio fallback used when tiktoken's encoding file cannot be fetched.

    tiktoken downloads `cl100k_base` on first use, which fails in air-gapped or restricted-egress
    environments — a real deployment constraint, not just a test-sandbox quirk. Crashing chunking
    over a missing tokenizer would be a poor trade, so this degrades to ~4 characters per token,
    which is a reasonable average for English technical prose.

    The consequence is honest but worth knowing: chunk sizes become approximate, so numbers from a
    run using this fallback are not comparable with numbers from a run using the real encoder. A
    warning is emitted once for exactly that reason.
    """

    CHARS_PER_TOKEN = 4

    def encode(self, text: str) -> list[int]:
        # Byte values stand in for token ids. Only the *count* and the ability to round-trip a
        # prefix matter to the callers, both of which this preserves.
        return list(text.encode("utf-8", errors="replace"))

    def decode(self, ids: list[int]) -> str:
        return bytes(ids).decode("utf-8", errors="ignore")

    def count(self, text: str) -> int:
        return max(1, len(text) // self.CHARS_PER_TOKEN)


@lru_cache
def _encoder():
    """Token counter.

    tiktoken's cl100k_base is used even when the target model is Claude or Titan. It is not exact
    for those, but chunk sizing needs a *consistent* ruler far more than an exact one — switching
    tokenizers between strategies would make a chunking benchmark meaningless.
    """
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        from sentinel.core.observability import log

        log.warning(
            "tokenizer_fallback",
            reason=str(exc)[:160],
            impact="chunk sizes are approximate and not comparable across runs",
        )
        return _ApproximateEncoder()


def ntokens(text: str) -> int:
    encoder = _encoder()
    counter = getattr(encoder, "count", None)
    return counter(text) if counter else len(encoder.encode(text))


def truncate_tokens(text: str, limit: int) -> str:
    """Hard-truncates to a token budget.

    Used as a safety net before embedding: every embedding model silently truncates input beyond
    its max sequence length, so a 2000-token chunk sent to a 512-token model loses three quarters
    of its content with no error anywhere. Truncating deliberately at least makes it visible.
    """
    encoder = _encoder()
    if ntokens(text) <= limit:
        return text
    ids = encoder.encode(text)
    # The fallback encoder works in bytes, so its budget is scaled to match the same token limit.
    budget = limit * _ApproximateEncoder.CHARS_PER_TOKEN if isinstance(encoder, _ApproximateEncoder) else limit
    return encoder.decode(ids[:budget])


class BaseChunker(ABC):
    """Contract every chunking strategy implements."""

    name: str = "base"

    @abstractmethod
    def split(self, doc: ParsedDocument) -> list[Chunk]:
        """Turns a parsed document into retrievable chunks."""

    def _finalise(self, doc: ParsedDocument, chunks: list[Chunk]) -> list[Chunk]:
        """Stamps shared metadata and drops empties.

        Centralised so that every strategy produces chunks carrying the fields retrieval, security
        filtering and citation rendering depend on. A strategy that forgets `tenant_id` produces
        chunks that are invisible to a filtered search, and the failure looks like poor recall
        rather than a missing field.
        """
        import hashlib

        out: list[Chunk] = []
        for chunk in chunks:
            if not chunk.embed_text.strip():
                continue
            chunk.doc_id = doc.doc_id
            # A strategy may pin a type (RunbookChunker does); otherwise inherit the document's.
            chunk.doc_type = chunk.doc_type or doc.doc_type or "page"
            chunk.tenant_id = doc.tenant_id
            chunk.source_uri = doc.source_uri
            chunk.effective_date = chunk.effective_date or doc.effective_date
            chunk.strategy = self.name
            chunk.token_count = ntokens(chunk.embed_text)
            chunk.content_hash = hashlib.sha256(chunk.embed_text.encode()).hexdigest()[:16]
            out.append(chunk)
        return out

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name}>"


REGISTRY: dict[str, Callable[..., BaseChunker]] = {}


def register(name: str) -> Callable[[type], type]:
    """Class decorator that adds a strategy to the registry."""

    def decorator(cls: type) -> type:
        REGISTRY[name] = cls
        cls.name = name  # type: ignore[attr-defined]
        return cls

    return decorator


def get_chunker(name: str | None = None, **kwargs: Any) -> BaseChunker:
    # Importing the strategies module is what runs the @register decorators. Done here as well as
    # in the package __init__ so a direct `from sentinel.chunking.base import get_chunker` still
    # finds a populated registry.
    from sentinel.chunking import strategies as _strategies  # noqa: F401
    from sentinel.core.config import settings

    name = name or settings.chunk_strategy
    if name not in REGISTRY:
        raise KeyError(f"unknown chunking strategy '{name}'; known: {sorted(REGISTRY)}")
    return REGISTRY[name](**kwargs)


def strategies() -> list[str]:
    return sorted(REGISTRY)
