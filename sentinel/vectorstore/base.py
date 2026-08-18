"""Vector store interface.

Every backend exposes the same four capabilities: upsert, dense search, sparse (keyword) search,
and delete. Hybrid retrieval is then assembled in `retrieval/`, except where the backend performs
fusion natively — Azure AI Search does, so its adapter overrides `hybrid_search` and the generic
path is skipped.

The `SearchFilter` type is the important part of this file. Security filtering must happen **inside**
the search, not after it: a post-filter has already loaded restricted content into your process,
where a logging statement or an exception traceback can leak it. Every adapter is therefore
required to translate `SearchFilter` into its backend's native filter language and apply it as part
of the query.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sentinel.core.types import Chunk, EmbeddedChunk, RetrievedChunk


@dataclass
class SearchFilter:
    """Backend-neutral filter.

    Roles and components come from the authenticated session, never from a tool argument or a
    model-generated field. `corpus` and `doc_type` are narrowing hints a caller may set; they can
    only restrict the result set further, never widen it past the ACL.
    """

    tenant_id: str | None = None
    roles: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    doc_type: str | None = None
    corpus: str | None = None
    index_version: str | None = None
    updated_after: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def require_scope(self) -> None:
        """Fails closed.

        An empty role list would render as an empty filter clause, which matches everything. That
        is the difference between "this caller sees nothing" and "this caller sees the entire
        corpus", so it raises rather than proceeding.
        """
        from sentinel.core.resilience import AuthorizationViolation

        if not self.roles:
            raise AuthorizationViolation("empty role set; refusing to search unfiltered")


class BaseVectorStore(ABC):
    name: str = "base"
    supports_native_hybrid: bool = False
    supports_native_rerank: bool = False

    @abstractmethod
    def ensure_index(self, dims: int, recreate: bool = False) -> None:
        """Creates or updates the index schema."""

    @abstractmethod
    def upsert(self, embedded: list[EmbeddedChunk]) -> int:
        """Writes chunks; must be idempotent on chunk_id."""

    @abstractmethod
    def dense_search(
        self, vector: list[float], k: int, filters: SearchFilter | None = None
    ) -> list[RetrievedChunk]: ...

    @abstractmethod
    def sparse_search(self, query: str, k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]: ...

    @abstractmethod
    def delete(self, chunk_ids: list[str]) -> int: ...

    @abstractmethod
    def chunk_ids_for_doc(self, doc_id: str) -> set[str]:
        """Existing chunk ids for a document, used to tombstone orphans on re-ingest."""

    def hybrid_search(
        self, query: str, vector: list[float], k: int, filters: SearchFilter | None = None
    ) -> list[RetrievedChunk]:
        """Generic hybrid: run both arms and fuse with RRF.

        Overridden by backends that fuse natively, which is preferable when available because the
        fusion happens before the result set is truncated rather than after.
        """
        from sentinel.retrieval.fusion import reciprocal_rank_fusion

        dense = self.dense_search(vector, k, filters)
        sparse = self.sparse_search(query, k, filters)
        return reciprocal_rank_fusion([dense, sparse])[:k]

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "ok": True}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name}>"


def chunk_to_record(embedded: EmbeddedChunk, index_version: str) -> dict[str, Any]:
    """Flattens a chunk into the field set every backend stores.

    Kept in one place so the three adapters cannot drift apart on field names — a mismatch there
    surfaces as a filter that silently matches nothing.
    """
    chunk = embedded.chunk
    return {
        "chunk_id": chunk.chunk_id,
        "parent_id": chunk.parent_id,
        "doc_id": chunk.doc_id,
        "content": chunk.context_text,
        "embed_content": chunk.embed_text,
        "heading_path": chunk.heading_path,
        "doc_type": chunk.doc_type,
        "tenant_id": chunk.tenant_id,
        "source_uri": chunk.source_uri,
        "page": chunk.page,
        "effective_date": chunk.effective_date,
        "allowed_roles": chunk.allowed_roles,
        "components": chunk.components,
        "strategy": chunk.strategy,
        "token_count": chunk.token_count,
        "content_hash": chunk.content_hash,
        "index_version": index_version,
        "embedding_model": embedded.model,
        "embedding_dims": embedded.dims,
    }


def record_to_chunk(record: dict[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=record["chunk_id"],
        doc_id=record.get("doc_id", ""),
        embed_text=record.get("embed_content", ""),
        context_text=record.get("content", ""),
        parent_id=record.get("parent_id", ""),
        heading_path=record.get("heading_path", ""),
        doc_type=record.get("doc_type", "page"),
        tenant_id=record.get("tenant_id", "default"),
        source_uri=record.get("source_uri", ""),
        page=record.get("page"),
        effective_date=record.get("effective_date"),
        allowed_roles=list(record.get("allowed_roles") or []),
        components=list(record.get("components") or []),
        strategy=record.get("strategy", ""),
        token_count=record.get("token_count", 0),
        content_hash=record.get("content_hash", ""),
    )
