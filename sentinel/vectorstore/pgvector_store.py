"""Postgres + pgvector.

The default, and a defensible production choice far longer than people assume. If Postgres is
already in the stack you get transactions, joins to your metadata, row-level security for tenant
isolation, and one fewer system to operate — and it stays comfortable into the tens of millions of
rows.

Two implementation details worth understanding:

**Hybrid search is a single SQL statement.** Postgres has full-text search built in (`tsvector` /
`ts_rank`), so the BM25-equivalent arm needs no second system. The query below runs both arms as
CTEs and fuses them with reciprocal rank fusion in SQL, which means fusion happens before
truncation rather than after — the same advantage Azure AI Search's native hybrid gives you.

**The ACL is enforced in the WHERE clause**, so a restricted row is never read off disk into the
process. `allowed_roles && %(roles)s` is a Postgres array-overlap operator: true when the two
arrays share any element. It is indexable with GIN, so it stays cheap.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import RETRIEVAL_SCORE, log
from sentinel.core.types import EmbeddedChunk, RetrievedChunk
from sentinel.vectorstore.base import (
    BaseVectorStore,
    SearchFilter,
    chunk_to_record,
    record_to_chunk,
)

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS {table} (
    chunk_id        TEXT PRIMARY KEY,
    parent_id       TEXT NOT NULL,
    doc_id          TEXT NOT NULL,
    content         TEXT NOT NULL,
    embed_content   TEXT NOT NULL,
    heading_path    TEXT DEFAULT '',
    doc_type        TEXT DEFAULT 'page',
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    source_uri      TEXT DEFAULT '',
    page            INT,
    effective_date  DATE,
    allowed_roles   TEXT[] NOT NULL DEFAULT ARRAY['admin'],
    components      TEXT[] NOT NULL DEFAULT '{{}}',
    strategy        TEXT DEFAULT '',
    token_count     INT DEFAULT 0,
    content_hash    TEXT DEFAULT '',
    index_version   TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dims  INT NOT NULL,
    embedding       vector({dims}) NOT NULL,
    -- Generated column keeps the search vector in sync automatically; a trigger would work too,
    -- but a generated column cannot drift out of date after a manual UPDATE.
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', embed_content)) STORED,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS {table}_tsv_idx     ON {table} USING GIN (tsv);
CREATE INDEX IF NOT EXISTS {table}_roles_idx   ON {table} USING GIN (allowed_roles);
CREATE INDEX IF NOT EXISTS {table}_comp_idx    ON {table} USING GIN (components);
CREATE INDEX IF NOT EXISTS {table}_doc_idx     ON {table} (doc_id);
CREATE INDEX IF NOT EXISTS {table}_tenant_idx  ON {table} (tenant_id, doc_type);
CREATE INDEX IF NOT EXISTS {table}_version_idx ON {table} (index_version);

-- HNSW over cosine distance. m and ef_construction trade build time and memory for recall;
-- ef_search is the runtime dial and is set per-session in dense_search.
CREATE INDEX IF NOT EXISTS {table}_hnsw_idx ON {table}
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200);
"""

HYBRID_SQL = """
WITH dense AS (
    SELECT chunk_id, 1 - (embedding <=> %(vec)s::vector) AS score,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(vec)s::vector) AS rank
    FROM {table}
    WHERE {where}
    ORDER BY embedding <=> %(vec)s::vector
    LIMIT %(k)s
),
sparse AS (
    SELECT chunk_id, ts_rank_cd(tsv, plainto_tsquery('english', %(q)s)) AS score,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %(q)s)) DESC
           ) AS rank
    FROM {table}
    WHERE {where} AND tsv @@ plainto_tsquery('english', %(q)s)
    LIMIT %(k)s
),
fused AS (
    -- Reciprocal rank fusion: rank-based, so the two incomparable score scales never meet.
    SELECT COALESCE(d.chunk_id, s.chunk_id) AS chunk_id,
           COALESCE(1.0 / (%(rrf_k)s + d.rank), 0) + COALESCE(1.0 / (%(rrf_k)s + s.rank), 0)
               AS fused_score,
           d.score AS dense_score, s.score AS sparse_score
    FROM dense d FULL OUTER JOIN sparse s ON d.chunk_id = s.chunk_id
)
SELECT t.*, f.fused_score, f.dense_score, f.sparse_score
FROM fused f JOIN {table} t ON t.chunk_id = f.chunk_id
ORDER BY f.fused_score DESC
LIMIT %(k)s;
"""


class PgVectorStore(BaseVectorStore):
    name = "local"
    supports_native_hybrid = True

    def __init__(self, dsn: str | None = None, table: str | None = None, ef_search: int = 100):
        import psycopg
        from psycopg_pool import ConnectionPool

        raw_dsn = (dsn or settings.pgvector_dsn).replace("postgresql+psycopg://", "postgresql://")
        self.table = table or settings.pgvector_collection
        self.ef_search = ef_search
        self._pool = ConnectionPool(raw_dsn, min_size=1, max_size=8, open=True)
        self._psycopg = psycopg

    def ensure_index(self, dims: int, recreate: bool = False) -> None:
        with self._pool.connection() as conn:
            if recreate:
                conn.execute(f"DROP TABLE IF EXISTS {self.table} CASCADE")
            conn.execute(DDL.format(table=self.table, dims=dims))
        log.info("index_ready", provider=self.name, table=self.table, dims=dims)

    # -------------------------------------------------------------------------- writes
    def upsert(self, embedded: list[EmbeddedChunk]) -> int:
        """Idempotent by chunk_id.

        `ON CONFLICT DO UPDATE` rather than DO NOTHING because a chunk id is content-derived: a
        collision means the same text at the same position, but metadata such as ACLs may have
        changed and must be refreshed.
        """
        if not embedded:
            return 0

        records = [chunk_to_record(e, settings.index_version) for e in embedded]
        columns = list(records[0]) + ["embedding"]
        placeholders = ", ".join(f"%({c})s" for c in columns)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "chunk_id")

        sql = (
            f"INSERT INTO {self.table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT (chunk_id) DO UPDATE SET {updates}"
        )

        rows = [{**rec, "embedding": str(emb.vector)} for rec, emb in zip(records, embedded)]
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(sql, rows)
        log.info("upserted", provider=self.name, count=len(rows))
        return len(rows)

    def delete(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE chunk_id = ANY(%s)", (chunk_ids,))
            return cur.rowcount

    def chunk_ids_for_doc(self, doc_id: str) -> set[str]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT chunk_id FROM {self.table} WHERE doc_id = %s", (doc_id,))
            return {row[0] for row in cur.fetchall()}

    # -------------------------------------------------------------------------- reads
    def _where(self, filters: SearchFilter | None) -> tuple[str, dict[str, Any]]:
        """Builds the WHERE clause and its parameters.

        Parameterised throughout — string interpolation of a role name into SQL would be an
        injection hole in the security filter itself, which is the worst possible place for one.
        """
        if filters is None:
            return "TRUE", {}
        filters.require_scope()

        clauses = ["allowed_roles && %(roles)s"]
        params: dict[str, Any] = {"roles": filters.roles}

        if filters.components:
            # A chunk with no component tag is platform-wide and visible to any authorised role.
            clauses.append("(components = '{}' OR components && %(components)s)")
            params["components"] = filters.components
        if filters.tenant_id:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = filters.tenant_id
        if filters.doc_type:
            clauses.append("doc_type = %(doc_type)s")
            params["doc_type"] = filters.doc_type
        if filters.index_version:
            clauses.append("index_version = %(index_version)s")
            params["index_version"] = filters.index_version
        if filters.updated_after:
            clauses.append("effective_date >= %(updated_after)s")
            params["updated_after"] = filters.updated_after

        return " AND ".join(clauses), params

    def dense_search(self, vector: list[float], k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]:
        where, params = self._where(filters)
        sql = (
            f"SELECT *, 1 - (embedding <=> %(vec)s::vector) AS score FROM {self.table} "
            f"WHERE {where} ORDER BY embedding <=> %(vec)s::vector LIMIT %(k)s"
        )

        with self._pool.connection() as conn:
            # ef_search is per-session and is the recall/latency dial for HNSW at query time.
            conn.execute(f"SET LOCAL hnsw.ef_search = {int(self.ef_search)}")
            with conn.cursor(row_factory=self._dict_row()) as cur:
                cur.execute(sql, {**params, "vec": str(vector), "k": k})
                rows = cur.fetchall()

        return [
            RetrievedChunk(
                chunk=record_to_chunk(r),
                score=float(r["score"]),
                dense_score=float(r["score"]),
                rank=i,
                retriever="pgvector_dense",
            )
            for i, r in enumerate(rows)
        ]

    def sparse_search(self, query: str, k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]:
        where, params = self._where(filters)
        sql = (
            f"SELECT *, ts_rank_cd(tsv, plainto_tsquery('english', %(q)s)) AS score "
            f"FROM {self.table} WHERE {where} AND tsv @@ plainto_tsquery('english', %(q)s) "
            f"ORDER BY score DESC LIMIT %(k)s"
        )

        with self._pool.connection() as conn, conn.cursor(row_factory=self._dict_row()) as cur:
            cur.execute(sql, {**params, "q": query, "k": k})
            rows = cur.fetchall()

        return [
            RetrievedChunk(
                chunk=record_to_chunk(r),
                score=float(r["score"]),
                sparse_score=float(r["score"]),
                rank=i,
                retriever="pgvector_bm25",
            )
            for i, r in enumerate(rows)
        ]

    def hybrid_search(
        self, query: str, vector: list[float], k: int, filters: SearchFilter | None = None
    ) -> list[RetrievedChunk]:
        """Single-statement hybrid with RRF fusion in SQL."""
        where, params = self._where(filters)
        sql = HYBRID_SQL.format(table=self.table, where=where)

        with self._pool.connection() as conn:
            conn.execute(f"SET LOCAL hnsw.ef_search = {int(self.ef_search)}")
            with conn.cursor(row_factory=self._dict_row()) as cur:
                cur.execute(sql, {**params, "vec": str(vector), "q": query, "k": k, "rrf_k": settings.rrf_k})
                rows = cur.fetchall()

        results = [
            RetrievedChunk(
                chunk=record_to_chunk(r),
                score=float(r["fused_score"]),
                dense_score=float(r["dense_score"]) if r.get("dense_score") is not None else None,
                sparse_score=float(r["sparse_score"]) if r.get("sparse_score") is not None else None,
                rank=i,
                retriever="pgvector_hybrid",
            )
            for i, r in enumerate(rows)
        ]
        if results:
            RETRIEVAL_SCORE.labels(self.name).observe(results[0].score)
        return results

    def _dict_row(self):
        from psycopg.rows import dict_row

        return dict_row

    def health(self) -> dict[str, Any]:
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT count(*), count(DISTINCT doc_id) FROM {self.table}")
                chunks, docs = cur.fetchone()
            return {"provider": self.name, "ok": True, "chunks": chunks, "documents": docs}
        except Exception as exc:
            return {"provider": self.name, "ok": False, "error": str(exc)[:200]}
