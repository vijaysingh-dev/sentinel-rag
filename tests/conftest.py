"""Shared fixtures.

The tests here run without any cloud credentials and without a database, because a test suite that
needs infrastructure is a test suite that stops being run. Provider adapters are exercised against
their contract with fakes; the real adapters are covered by the scripts in `scripts/`, which are
deliberately manual.
"""

from __future__ import annotations

import pytest

from sentinel.core.types import Chunk, EmbeddedChunk, RetrievedChunk
from sentinel.vectorstore.base import BaseVectorStore, SearchFilter


def make_chunk(
    chunk_id: str, text: str, roles: list[str], components: list[str] | None = None, doc_type: str = "page"
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("::")[0],
        embed_text=text,
        context_text=text,
        parent_id=chunk_id.split("::")[0],
        heading_path=text[:40],
        doc_type=doc_type,
        allowed_roles=roles,
        components=components or [],
        token_count=len(text.split()),
        content_hash=chunk_id,
    )


class FakeEmbedder:
    """Deterministic hash-based vectors.

    Not semantically meaningful, which is fine: these tests check plumbing, filtering and ordering,
    not retrieval quality. Quality is measured by the golden set, not by unit tests.
    """

    name = "fake"
    model = "fake-embed"
    dims = 8

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [
            EmbeddedChunk(chunk=c, vector=self._vec(c.embed_text), model=self.model, dims=self.dims) for c in chunks
        ]

    @staticmethod
    def _vec(text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode()).digest()
        return [(b - 128) / 128 for b in digest[:8]]


class FakeStore(BaseVectorStore):
    """In-memory store that enforces the ACL exactly as a real backend must.

    The important part is that filtering happens during the scan, before anything is returned —
    the same property the SQL, OData and bool-query filters provide in the real adapters. A fake
    that filters afterwards would let an ACL bug pass the test suite.
    """

    name = "fake"

    def __init__(self) -> None:
        self.records: dict[str, Chunk] = {}

    def ensure_index(self, dims: int, recreate: bool = False) -> None:
        if recreate:
            self.records.clear()

    def upsert(self, embedded: list[EmbeddedChunk]) -> int:
        for item in embedded:
            self.records[item.chunk.chunk_id] = item.chunk
        return len(embedded)

    def delete(self, chunk_ids: list[str]) -> int:
        removed = 0
        for cid in chunk_ids:
            removed += bool(self.records.pop(cid, None))
        return removed

    def chunk_ids_for_doc(self, doc_id: str) -> set[str]:
        return {cid for cid, c in self.records.items() if c.doc_id == doc_id}

    def _visible(self, filters: SearchFilter | None) -> list[Chunk]:
        if filters is None:
            return list(self.records.values())
        filters.require_scope()

        roles = set(filters.roles)
        components = set(filters.components)
        out = []
        for chunk in self.records.values():
            chunk_roles = set(chunk.allowed_roles or [])
            if chunk_roles and not (chunk_roles & roles):
                continue
            chunk_components = set(chunk.components or [])
            if components and chunk_components and not (chunk_components & components):
                continue
            if filters.doc_type and chunk.doc_type != filters.doc_type:
                continue
            out.append(chunk)
        return out

    def dense_search(self, vector, k, filters=None):
        return [
            RetrievedChunk(chunk=c, score=1.0 - i * 0.01, dense_score=1.0 - i * 0.01, rank=i, retriever="fake_dense")
            for i, c in enumerate(self._visible(filters)[:k])
        ]

    def sparse_search(self, query, k, filters=None):
        terms = set(query.lower().split())
        scored = [(len(terms & set(c.embed_text.lower().split())), c) for c in self._visible(filters)]
        scored = [(s, c) for s, c in scored if s > 0]
        scored.sort(key=lambda pair: -pair[0])
        return [
            RetrievedChunk(chunk=c, score=float(s), sparse_score=float(s), rank=i, retriever="fake_bm25")
            for i, (s, c) in enumerate(scored[:k])
        ]


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def store(embedder: FakeEmbedder) -> FakeStore:
    fake = FakeStore()
    chunks = [
        make_chunk(
            "d1::0001::a",
            "checkout runbook rule out upstream first payment shipping cart dependencies trace",
            ["admin", "sre", "oncall"],
            ["platform"],
            "runbook",
        ),
        make_chunk(
            "d2::0001::b",
            "RED metrics rate errors duration glossary observability terminology",
            ["admin", "sre", "oncall", "viewer"],
            [],
            "glossary",
        ),
        make_chunk(
            "d3::0001::c",
            "INC-2642 payment unreachable post-mortem customer impact admin only",
            ["admin"],
            ["platform"],
            "incident",
        ),
        make_chunk(
            "d4::0001::d",
            "capacity review product-catalog read replica headroom sre only",
            ["admin", "sre"],
            [],
            "reference",
        ),
    ]
    fake.upsert(embedder.embed_chunks(chunks))
    return fake
