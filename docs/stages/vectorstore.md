# Vector store

> Stage 4 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#4-vector-store); this file is the deep version.
>
> Code: `sentinel/vectorstore/` · Scripts: `scripts/vectorstore/` · Benchmark: none dedicated — measured indirectly by `benchmarks/retrieval_sweep.py` (see [Result](#result))

---

## What we used

| Provider | Backend | Hybrid search |
|---|---|---|
| local (only provider) | Postgres + pgvector | reciprocal rank fusion, computed inside a single SQL statement |

Since ADR-029, pgvector is the *only* maintained vector store — there is no Azure/AWS counterpart to pick between. Azure AI Search and OpenSearch adapters existed earlier in this project's history and are preserved at `.archive/providers/vectorstore/` for reference (their SigV4 signing, OData filter syntax, and native-hybrid mechanics are real and worth reading if you ever need to build a cloud adapter), but they are not wired into the provider registry today.

---

## The big picture

Every stage before this one — parsing, chunking, embedding — produces things that exist only in memory for the length of one script run. The vector store is the first stage in the pipeline that actually **keeps** anything. It's the filing cabinet: once a chunk and its embedding go in, they sit there until someone re-indexes or deletes them, and every future query has to search through what's already filed rather than re-deriving it from scratch.

That makes this stage responsible for three jobs at once, and the interesting design decisions in this file are mostly about how those three jobs share one system without fighting each other:

1. **Storage.** Hold the chunk's text, its metadata (which document it came from, what role can see it, which page it was on), and its embedding vector, durably, and let you write to it without corrupting what's already there if you write the same chunk twice.
2. **Search.** Given a new query, find the stored chunks that are relevant to it — both by *meaning* (does this chunk's vector point in roughly the same direction as the query's vector?) and by *exact words* (does this chunk contain the literal string "INC-2621"?). Those are two different kinds of "relevant" and this stage has to support both, because they fail on different queries.
3. **Access control.** Make sure that when someone without clearance to see an admin-only incident report searches for it, the system behaves as if that document doesn't exist for them — not "found it, but you can't read it," which is itself a leak (see [ACL, in depth](#acl-in-depth) below).

Two ways to do each of those jobs exist and this codebase picked specific ones. Storage: a **dedicated vector database** (Pinecone, Weaviate, Qdrant, Azure AI Search) versus **an extension bolted onto a database you probably already run** (pgvector on Postgres). Search: fuse two search engines at the *application* layer (query each separately, combine the ranked lists in Python) versus fuse them *inside the database* in one query. Access control: filter results *after* they come back from the database versus filter them *as part of* the database query itself. This codebase picked the second option in all three cases, and the reasoning is consistent across all three: fewer moving parts, and doing the risky thing (loading data, mixing search modes, checking permissions) as early and as close to the data as possible rather than after it has already left the database and entered your application's memory.

### Why pgvector, specifically

If you already run Postgres for anything else in your stack (and most backends do — user accounts, billing, audit logs), pgvector turns "add a vector database" into "add an extension to the database you already operate." That buys you things a dedicated vector database usually can't give you for free: transactions (a chunk write and its metadata write either both happen or neither does), joins against your other tables, and — the detail that matters most for this codebase — row-level filtering that's as fast and as battle-tested as any other Postgres `WHERE` clause, because it *is* an ordinary Postgres `WHERE` clause.

The common objection is "but a dedicated vector database is *faster* at scale." True, eventually. pgvector's HNSW index (explained below) stays comfortable into the tens of millions of rows on ordinary hardware — most teams reach for a dedicated vector database long before they've actually outgrown what Postgres can do, because "you need a dedicated vector database" is a claim that becomes true later than most people assume it will.

---

## The output contract: what a `SearchFilter` and a search result actually are

Before the code, the two shapes every function in this stage passes around.

### `SearchFilter` (`base.py`)

```python
@dataclass
class SearchFilter:
    tenant_id: str | None = None
    roles: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    doc_type: str | None = None
    corpus: str | None = None
    index_version: str | None = None
    updated_after: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
```

Think of this as a request for "search, but only within this fenced-off area." `roles` and `tenant_id` describe the fence — they come from *who is asking* (the authenticated session), never from the query text or a model-generated argument, because anything a language model can write is something a cleverly-worded prompt could try to manipulate. `doc_type`, `corpus`, and `updated_after` are narrowing hints a caller can add on top — a caller can ask for a smaller slice of what they're already allowed to see, but nothing here can ever *widen* the fence past what `roles` allows.

**`require_scope()`** is one method, four lines, and it is the single most important function in this entire stage:

```python
def require_scope(self) -> None:
    if not self.roles:
        raise AuthorizationViolation("empty role set; refusing to search unfiltered")
```

Why this exists, in plain terms: further down, the actual SQL filter is built as `allowed_roles && %(roles)s` — "does this chunk's list of allowed roles overlap with the caller's list of roles at all?" If `roles` is an empty list, that overlap check becomes vacuously true for a chunk with no roles, but more importantly the code path that *builds* the clause has to decide what to do with "no roles were given" — and the two possible answers are wildly different in consequence. One answer is "then this caller can't be authorized for anything, refuse the whole search." The other, wrong, answer is "then don't filter at all" — which reads as reasonable code (`if filters.roles: clause += ...`) but actually means an uninitialized or buggy caller sees the *entire corpus*, including every admin-only incident report. `require_scope()` exists purely to make the first answer the only possible one: it raises before a query is ever built, so there is no code path where "empty roles" silently becomes "no restriction."

### `chunk_to_record` / `record_to_chunk` (`base.py`)

```python
def chunk_to_record(embedded: EmbeddedChunk, index_version: str) -> dict[str, Any]:
    chunk = embedded.chunk
    return {
        "chunk_id": chunk.chunk_id, "parent_id": chunk.parent_id, "doc_id": chunk.doc_id,
        "content": chunk.context_text, "embed_content": chunk.embed_text,
        ...
        "index_version": index_version, "embedding_model": embedded.model, "embedding_dims": embedded.dims,
    }
```

This is the translation layer between a `Chunk` (the in-memory dataclass every other stage works with) and a database row (a flat dictionary of columns). It looks like boilerplate, and mechanically it is, but it's centralized on purpose: if every adapter wrote its own version of this mapping, a rename or a typo in one adapter's field list would silently desync it from the others, and the bug would show up as "a filter that matches nothing" or "a chunk that comes back with a blank field" — not as an error anywhere. One function that every adapter calls means there is exactly one place a field name can be wrong, and fixing it fixes every backend at once. Note the field rename: `Chunk.context_text` becomes the row's `content` (what a human or the LLM reads), and `Chunk.embed_text` becomes `embed_content` (what got vectorized and what full-text search runs against) — the two-field split from the chunking stage survives all the way into storage.

`record_to_chunk` is the exact inverse, used every time a row comes back from a query and needs to become a `Chunk` object again for the rest of the pipeline to work with.

### `RetrievedChunk` (`core/types.py`)

```python
@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float
    dense_score: float | None = None
    sparse_score: float | None = None
    rerank_score: float | None = None
    rank: int = 0
```

A search result carries **every score that touched it**, not just the final one. This is a diagnostic decision, not a data-modeling one: a chunk with a high `dense_score` but a low `rerank_score` tells you the embedding model was fooled by surface-level similarity (words that *sound* related but aren't actually relevant); a chunk that only has a `sparse_score` (no `dense_score` at all) tells you the query was lexical — an exact identifier or error code — and the embedding arm contributed nothing for it. Collapsing these into one number at search time would make retrieval-quality debugging much harder later, for the cost of a few extra fields on a dataclass.

---

## `BaseVectorStore` — the interface every backend implements (`base.py`)

```python
class BaseVectorStore(ABC):
    @abstractmethod
    def ensure_index(self, dims: int, recreate: bool = False) -> None: ...
    @abstractmethod
    def upsert(self, embedded: list[EmbeddedChunk]) -> int: ...
    @abstractmethod
    def dense_search(self, vector, k, filters=None) -> list[RetrievedChunk]: ...
    @abstractmethod
    def sparse_search(self, query, k, filters=None) -> list[RetrievedChunk]: ...
    @abstractmethod
    def delete(self, chunk_ids: list[str]) -> int: ...
    @abstractmethod
    def chunk_ids_for_doc(self, doc_id: str) -> set[str]: ...

    def hybrid_search(self, query, vector, k, filters=None) -> list[RetrievedChunk]:
        from sentinel.retrieval.fusion import reciprocal_rank_fusion
        dense = self.dense_search(vector, k, filters)
        sparse = self.sparse_search(query, k, filters)
        return reciprocal_rank_fusion([dense, sparse])[:k]
```

Six methods every backend must implement, plus one — `hybrid_search` — that has a working *default* implementation and only needs overriding if a backend can do better. That default is the honest fallback: run `dense_search` and `sparse_search` as two separate queries, get two separate ranked lists back, and fuse them in Python with reciprocal rank fusion (the same algorithm explained in the [retrieval stage](retrieval.md) — see that doc for the fusion math itself).

**Why `hybrid_search` is overridden here.** pgvector *can* do better: because both arms live in the same table, a single SQL statement can run both searches and fuse them before either list is even materialized as Python objects. The docstring in `pgvector_store.py` states the concrete advantage plainly: fusion happens *before truncation* rather than after. If you fuse in Python, you first have to decide how many candidates to pull back from each arm (say, top 50), and if the true best fused result only ranked 60th in one arm, you've already thrown it away before fusion ever saw it. Fusing inside the database means the fusion query can reason about a wider candidate pool without ever pulling all of it into your application process.

`chunk_ids_for_doc` exists for one purpose: **tombstoning orphans**. When a document is edited and re-ingested, some of its old chunks may no longer exist in the new version (a section was deleted). Content-derived chunk ids mean the *unchanged* chunks upsert cleanly in place, but nothing automatically deletes chunks that vanished. The ingest pipeline calls `chunk_ids_for_doc(doc_id)` to get the *previous* set of ids, diffs it against the *new* set, and deletes whatever's left over. Skip this step and the index accumulates stale chunks from every past version of every edited document forever — including, eventually, a superseded runbook that the system will confidently cite as current.

---

## `PgVectorStore` — function by function (`pgvector_store.py`)

### The schema (`DDL`)

```sql
CREATE TABLE IF NOT EXISTS {table} (
    chunk_id        TEXT PRIMARY KEY,
    ...
    allowed_roles   TEXT[] NOT NULL DEFAULT ARRAY['admin'],
    components      TEXT[] NOT NULL DEFAULT '{}',
    ...
    embedding       vector({dims}) NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', embed_content)) STORED,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

A few choices worth pulling out individually:

- **`allowed_roles TEXT[] DEFAULT ARRAY['admin']`.** The default is the *most restrictive* value, not the least. If some future code path ever inserts a chunk without explicitly setting `allowed_roles`, that chunk becomes admin-only by accident rather than world-readable by accident. A bug in "which roles can see this" should fail toward "too few people can see it" (annoying, discoverable, fixable) rather than "too many people can see it" (a silent data leak that might never be noticed).
- **`tsv tsvector GENERATED ALWAYS AS (...) STORED`.** This is Postgres's built-in full-text search column — a pre-processed, searchable representation of `embed_content` (lower-cased, stemmed, stripped of stopwords). `GENERATED ALWAYS ... STORED` means Postgres recomputes this column automatically, on every insert and update, from `embed_content`. The comment in the code explains the alternative that was rejected: a database *trigger* could do the same recomputation, but a trigger only fires on statements that go through it — a generated column is enforced by the table definition itself, so there's no code path (a bulk `UPDATE`, a migration script, a future adapter) that can leave `tsv` stale relative to the text it's supposed to represent. It cannot drift, by construction, which is worth more than the marginal flexibility a trigger would offer.
- **`vector({dims})`.** The dimensionality isn't hardcoded — it's a parameter, filled in with whatever the active embedder produces (768 for the local model, up to 3072 for OpenAI's largest, or less if Matryoshka-truncated — see the [embedding stage](embedding.md)). This is why `ensure_index(dims, ...)` takes `dims` as an argument rather than reading it from a constant.

Five indexes follow the table. Four are ordinary B-tree or GIN indexes on the columns you'd expect to filter or search on (`doc_id`, `tenant_id`, `index_version`, plus GIN on `tsv`, `allowed_roles`, and `components` — GIN is the index type Postgres uses for "does this array/set-like column contain X," which is exactly the operation `allowed_roles && %(roles)s` needs). The fifth is the interesting one:

```sql
CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200);
```

**HNSW** stands for Hierarchical Navigable Small World — it's the algorithm pgvector uses to make nearest-neighbor search over embedding vectors fast. Without an index, "find the 10 chunks whose vectors are closest to this query vector" means comparing the query against *every single row* in the table — exact, but it gets slow as the table grows. HNSW builds a multi-layer graph structure at index-creation time that lets a search skip most of the table and still find (almost always) the true nearest neighbors — this is called **approximate nearest neighbor (ANN)** search, and the "approximate" is deliberate: you trade a tiny, tunable amount of accuracy for a very large amount of speed. `vector_cosine_ops` tells pgvector to build the graph around *cosine distance* (how different two vectors' *directions* are, ignoring their length) rather than Euclidean distance — cosine is the right notion of "similar" for embeddings, where what matters is the direction the vector points, not how long it is.

### `__init__`

```python
def __init__(self, dsn: str | None = None, table: str | None = None, ef_search: int = 100):
    ...
    raw_dsn = (dsn or settings.pgvector_dsn).replace("postgresql+psycopg://", "postgresql://")
    self._pool = ConnectionPool(raw_dsn, min_size=1, max_size=8, open=True)
```

Two small things worth noticing. First, the DSN string replacement: `settings.pgvector_dsn` is written in SQLAlchemy's connection-string style (`postgresql+psycopg://...`), because that's the convention the rest of this codebase's settings use, but the raw `psycopg` driver this adapter uses directly doesn't understand the `+psycopg` dialect suffix — it wants plain `postgresql://`. That one `.replace()` call is the seam between "how config is written" and "what the driver actually needs." Second, a **connection pool** (`min_size=1, max_size=8`) is created once, at construction, rather than opening a fresh database connection per query. Opening a Postgres connection is relatively expensive (TCP handshake, auth, session setup); a pool keeps a small number of connections open and hands them out to whichever query needs one, which is the standard fix for "don't pay connection-setup cost on every single search."

### `ensure_index(dims, recreate=False)`

Runs the `DDL` above, substituting in the table name and the embedder's dimensionality. `recreate=True` drops the table first — used in scripts and tests that want a clean slate, never called automatically in production ingestion, since it would delete the entire corpus.

### `upsert(embedded)` — why `ON CONFLICT DO UPDATE`, not `DO NOTHING`

```python
sql = (
    f"INSERT INTO {self.table} (...) VALUES (...) "
    f"ON CONFLICT (chunk_id) DO UPDATE SET {updates}"
)
```

`ON CONFLICT (chunk_id)` fires when a chunk with that exact id already exists in the table — which, because `chunk_id` is content-derived (same document position, same text), means "this exact chunk was already indexed once." The easy choice here would be `DO NOTHING`: if it's already there, skip it. That's *wrong* for a subtle reason: the chunk's **text** hasn't changed (that's what made the id collide), but its **metadata** might have — someone might have changed which roles are allowed to see this document since the last ingest, or the `index_version` might have changed because the embedding model was upgraded. `DO UPDATE` means every re-ingestion refreshes the full row, including ACLs and the embedding vector itself, even when the id collided. This is also what makes re-running ingestion over an unchanged corpus a genuinely safe, idempotent operation — `scripts/vectorstore/01_index_and_search.py` demonstrates this directly by calling `upsert` twice in a row and showing the second call creates zero new rows.

### `delete` / `chunk_ids_for_doc`

Straightforward `DELETE ... WHERE chunk_id = ANY(%s)` and `SELECT chunk_id WHERE doc_id = %s`. The interesting part is what calls `chunk_ids_for_doc` and why — see [orphan tombstoning](#basevectorstore--the-interface-every-backend-implements-basepy) above.

### `_where(filters)` — building the security filter

```python
def _where(self, filters: SearchFilter | None) -> tuple[str, dict[str, Any]]:
    if filters is None:
        return "TRUE", {}
    filters.require_scope()

    clauses = ["allowed_roles && %(roles)s"]
    params: dict[str, Any] = {"roles": filters.roles}

    if filters.components:
        clauses.append("(components = '{}' OR components && %(components)s)")
        params["components"] = filters.components
    ...
    return " AND ".join(clauses), params
```

This is the function that turns a backend-neutral `SearchFilter` into an actual Postgres `WHERE` clause, and it is where the [`SearchFilter.require_scope()`](#the-output-contract-what-a-searchfilter-and-a-search-result-actually-are) check is actually invoked — every single search, no matter which method, routes through `_where` first, so there is no query path in this file that can skip the empty-roles check.

Two details matter more than they look like they should:

- **`allowed_roles && %(roles)s` is the array-overlap operator.** It reads as "does the chunk's list of allowed roles share *any* element with the caller's list of roles." A chunk tagged `["admin"]` and a caller with roles `["oncall", "sre"]` don't overlap, so that chunk is excluded — the caller simply never sees a row for it, the same as if it never existed.
- **`(components = '{}' OR components && %(components)s)`.** This is the "untagged chunks are platform-wide" rule made concrete: a chunk with an *empty* `components` array (a document that isn't scoped to any particular service) is visible to anyone who already has role-level access, regardless of what `components` filter they searched with. Only a chunk that *does* have a components tag gets narrowed by this clause. Get this backwards — require an exact components match even for untagged chunks — and every platform-wide document (glossaries, general methodology docs) silently disappears from every component-scoped search.
- **Every value is a bound parameter (`%(roles)s`), never string-interpolated into the SQL text.** This matters more here than almost anywhere else in the codebase: this is the *security* filter, and if a role name were ever interpolated directly into a SQL string instead of passed as a parameter, you'd have built a SQL injection hole inside the exact mechanism meant to keep people out of data they shouldn't see — the worst possible place in the whole system for that class of bug to exist.

### `dense_search`, `sparse_search`, `hybrid_search`

`dense_search` builds `SELECT *, 1 - (embedding <=> %(vec)s::vector) AS score ... ORDER BY embedding <=> %(vec)s::vector`. `<=>` is pgvector's cosine-distance operator (smaller = more similar); the code negates it (`1 - distance`) to report a *similarity* score instead, which is more intuitive to read (higher = better, same direction as every other score in the system). Before running the query, it sets a per-session tuning knob:

```python
conn.execute(f"SET LOCAL hnsw.ef_search = {int(self.ef_search)}")
```

**`ef_search`** is HNSW's recall/speed dial, and it's set with `SET LOCAL` — meaning the setting applies only for the rest of the current transaction, then reverts, so one caller's tuning choice can never leak into another query on a pooled connection. A higher `ef_search` makes the graph traversal explore more candidates before returning its answer — slower, but closer to what an *exact* (brute-force) search would return. A lower value is faster but risks missing a true nearest neighbor that the approximate search happened to skip past. `ef_search=100` is the default here; it's a real dial an operator can turn up if recall ever looks suspiciously low under heavy filtering (see [What breaks in production](#what-breaks-in-production)).

`sparse_search` is the lexical arm: `ts_rank_cd(tsv, plainto_tsquery('english', %(q)s))`, Postgres's built-in full-text ranking function, filtered by `tsv @@ plainto_tsquery(...)` — "does the query's parsed terms match this row's pre-computed search vector at all." This is what catches exact identifiers, error codes, and jargon that an embedding model might not represent distinctly from similar-sounding text.

`hybrid_search` is the one PgVectorStore overrides with something better than the generic base-class fallback — the `HYBRID_SQL` statement:

```sql
WITH dense AS (
    SELECT chunk_id, 1 - (embedding <=> %(vec)s::vector) AS score,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(vec)s::vector) AS rank
    FROM {table} WHERE {where} ORDER BY ... LIMIT %(k)s
),
sparse AS ( ... same shape, but ts_rank_cd instead ... ),
fused AS (
    SELECT COALESCE(d.chunk_id, s.chunk_id) AS chunk_id,
           COALESCE(1.0 / (%(rrf_k)s + d.rank), 0) + COALESCE(1.0 / (%(rrf_k)s + s.rank), 0) AS fused_score,
           d.score AS dense_score, s.score AS sparse_score
    FROM dense d FULL OUTER JOIN sparse s ON d.chunk_id = s.chunk_id
)
SELECT t.*, f.fused_score, f.dense_score, f.sparse_score
FROM fused f JOIN {table} t ON t.chunk_id = f.chunk_id
ORDER BY f.fused_score DESC LIMIT %(k)s;
```

Reading it top to bottom: `dense` and `sparse` are two **CTEs** (`WITH ... AS (...)`, temporary named result sets scoped to this one query) that each run one of the two search modes and, critically, compute a `rank` — 1st place, 2nd place, and so on — via `ROW_NUMBER() OVER (ORDER BY ...)`. `fused` then combines them with a `FULL OUTER JOIN` on `chunk_id`, meaning a chunk that appears in *either* list (not just both) survives into the fused result. The actual fusion formula, `1.0 / (rrf_k + rank)` summed across both arms, is **reciprocal rank fusion (RRF)** — the same algorithm described in depth in the [retrieval stage doc](retrieval.md). The short version of why RRF and not just averaging the two scores: cosine similarity lives in `[0, 1]` and the full-text rank score is unbounded and depends on the corpus, so the two numbers aren't on comparable scales and can't be added meaningfully — but *ranks* (1st, 2nd, 3rd...) always mean the same thing regardless of what produced them, so fusing by rank sidesteps the scale mismatch entirely. `COALESCE(..., 0)` handles the case where a chunk appeared in only one arm (its rank in the other is `NULL`, contributing `0` to that arm's term rather than breaking the sum).

The whole point of doing this as one SQL statement instead of two Python-level queries plus a Python fusion step: the fusion happens over the *full* `LIMIT %(k)s` candidate set from each arm, computed and combined entirely inside Postgres, before a single Python object gets built. Nothing about ranking or fusing pays a network round-trip per intermediate step.

### `health()`

```python
def health(self) -> dict[str, Any]:
    try:
        cur.execute(f"SELECT count(*), count(DISTINCT doc_id) FROM {self.table}")
        chunks, docs = cur.fetchone()
        return {"provider": self.name, "ok": True, "chunks": chunks, "documents": docs}
    except Exception as exc:
        return {"provider": self.name, "ok": False, "error": str(exc)[:200]}
```

A cheap, always-safe status check — used by monitoring and by the demo scripts to print "is the store actually populated and reachable" without running a real search. It never raises; a failure becomes `{"ok": False, "error": ...}` rather than an exception, because a health check that can itself crash the caller isn't a useful health check.

---

## ACL, in depth

This is the one stage in the whole pipeline where CLAUDE.md's non-negotiable security invariants live most concretely, so it's worth being explicit about the layers, because there are more of them than just `_where`:

1. **`SearchFilter.require_scope()`** — fails closed on empty roles, before any query is built.
2. **`_where()`'s `allowed_roles && %(roles)s` clause** — the filter is inside the SQL query, so a restricted row is never read off disk into this process's memory in the first place. This is ADR-006's whole argument: a *post*-filter (query everything, throw away what the caller shouldn't see, in Python) has already loaded the restricted content somewhere a log line, an exception traceback, or a debugger session could leak it. Filtering inside the query means the restricted row genuinely never crosses that boundary.
3. **`RetrievalPipeline._assert_authorized()`** (in `retrieval/pipeline.py`, not this stage's own code, but the second line of defense against this stage's filter): re-checks every result *after* it comes back and raises `AuthorizationViolation` — logged as `log.critical`, a page-worthy event — if a chunk's `allowed_roles` doesn't actually overlap the caller's roles. This exists for the case ADR-005 calls out explicitly: "a filter bug is a data leak rather than an empty result." If `_where()` ever had a bug, or a chunk was mistagged at ingest, this is the safety net that catches it before the chunk reaches a user.
4. **The refusal must not disclose that the restricted document exists.** This one lives outside this stage — in the tool layer that talks to the RCA agent — but it's tested against exactly this stage's data in `tests/test_acl.py::test_refusal_does_not_disclose_existence`, which asserts that a denial message never contains the forbidden document's identifying details ("INC-2642", "payment unreachable", "post-mortem"). Saying "you don't have access to the payment-outage post-mortem" has *already* leaked that a payment outage happened, to someone who may not have been meant to know that.

`tests/test_acl.py` is the release gate for all four layers, and per CLAUDE.md these are treated as hard gates, never averaged into a quality score — a 98% pass rate on "did not leak restricted content" is a failing grade, not a good score.

---

## Why these defaults

| constant | value | derivation |
|---|---|---|
| `ef_construction` | `200` | Build-time HNSW parameter — how thorough the graph-building search is when the index is first created. Higher values produce a better-connected (more accurate) graph at the cost of slower index builds. `200` is pgvector's own commonly recommended starting point: expensive enough to build a good graph, cheap enough that rebuilding an index over this corpus's size is not a production event. |
| `m` | `16` | The maximum number of graph edges per node in the HNSW structure — roughly, how many neighbors each vector is connected to. Higher `m` improves recall and lookup speed but uses more memory and takes longer to build; `16` is pgvector's documented default and the standard starting point across the wider HNSW literature (not just this codebase's choice). |
| `ef_search` | `100` | The runtime recall/speed dial, set per query via `SET LOCAL`. Higher than `ef_construction`'s implicit search width would be overkill for typical queries; `100` is comfortably above the `k` values this pipeline actually requests (`k=50` at retrieval, `top_n=6` after reranking — see the [retrieval stage](retrieval.md)), giving the graph traversal enough headroom to find true nearest neighbors reliably without paying for exploring far more of the graph than a `top-50` query needs. |
| `rrf_k` (`settings.rrf_k`) | `60` | The RRF damping constant, shared with the [retrieval stage](retrieval.md)'s Python-side fusion so the *same* fusion math runs whether pgvector fuses natively or the generic base-class path is used. `60` is the value from the original RRF paper (Cormack et al.) and is what most systems that use RRF at all default to; it damps the influence of whichever ranker's #1 result is confidently wrong, without needing per-corpus tuning. |
| full-text language | `english` (`to_tsvector('english', ...)`, `plainto_tsquery('english', ...)`) | Matches this corpus's actual content. A multi-language corpus would need per-document language detection and a `tsvector` per language, which this codebase does not currently do — see [What breaks in production](#what-breaks-in-production). |
| connection pool size | `min_size=1, max_size=8` | A conservative default sized for a single-process demo/ops workload, not a high-throughput production deployment — the ceiling most teams hit first is Postgres's own `max_connections`, not this pool. Worth raising deliberately (and coordinating with `max_connections`) before putting this behind real concurrent traffic. |

---

## Common questions

**Why not just use a dedicated vector database (Pinecone, Weaviate, Qdrant)?** Because the marginal capability a dedicated vector database adds over pgvector at this corpus's size — tens of thousands to low millions of chunks — is small, and the cost of running a second system (a second thing to provision, monitor, back up, and keep in sync with the source of truth) is not. pgvector gets you row-level filtering (the ACL mechanism above) essentially for free because it's just Postgres, and Postgres is a system most teams already know how to operate. Read `docs/design-decisions.md`'s ADR-029 for the full reasoning, including when this call should be revisited.

**What happens if I search with a role that has access to nothing?** The query runs, the `WHERE` clause is real and non-empty (`allowed_roles && ARRAY['some_unrecognized_role']`), and it simply returns zero rows — not an error, not a special case. This is the correct behavior: a role that legitimately has no matching content should look identical, from the caller's perspective, to a role that has content but none of it matched this particular query.

**Why `&&` (array overlap) instead of requiring an exact role match?** Because a chunk can be visible to *multiple* roles (`["admin", "workflow"]`) and a caller can *hold* multiple roles (`["workflow", "support"]`) — the question is "is there at least one role in common," which is exactly what array overlap answers. An exact-match requirement would mean a chunk tagged for two roles is invisible to anyone who doesn't hold *both* simultaneously, which isn't the intended semantics.

**Why does `_where()` return `"TRUE", {}` when `filters is None`?** Some call sites (internal tooling, certain benchmark scripts) intentionally search unfiltered — an unfiltered search is a valid, explicit choice (`filters=None`), which is different from an *empty-but-present* filter (`SearchFilter(roles=[])`, which is what `require_scope()` catches). The distinction matters: "I deliberately didn't pass a filter" and "I passed a filter that happens to be empty, probably by mistake" are different failure classes, and this stage treats them differently on purpose.

**What does re-ingesting an unchanged document actually cost?** One `UPSERT` per chunk, each of which hits `ON CONFLICT (chunk_id) DO UPDATE` and rewrites the row with identical data — cheap, and it never creates a duplicate, because the id is content-derived. `scripts/vectorstore/01_index_and_search.py` demonstrates this directly: it calls `store.upsert(embedded)` twice back to back and the second call reports the same row count with zero new chunks created.

**How does deduplication at retrieval time relate to this stage?** It doesn't happen here — pgvector returns exactly what the SQL asked for, including near-duplicate chunks if the same content shows up from multiple retrieval arms or multiple similar sections. Deduplication (`retrieval/fusion.dedupe_by_parent`, `dedupe_by_content`) is a [retrieval-stage](retrieval.md) concern that runs *after* this stage hands back results — this stage's job is to answer "what matches," not "what's the best final set to show a human."

**Can two different embedding models' vectors live in the same table safely?** Not meaningfully comparably, no — cosine distance between a 768-dimension local-model vector and a 3072-dimension OpenAI vector isn't even computable (different dimensionality), and even same-dimension vectors from different models don't share a coordinate system. This is exactly what `index_version` (stamped on every chunk, filtered on every query — see the [embedding stage](embedding.md)) exists to prevent: querying only ever compares vectors produced by the same model.

**What's the actual difference between `dense_search`, `sparse_search`, and `hybrid_search` in when you'd want each?** `dense_search` wins on paraphrase and vocabulary mismatch ("bringing checkout back online safely" finding a runbook titled "Checkout Restart Procedure"). `sparse_search` wins on exact identifiers and jargon an embedding model might blur ("INC-2621"). `hybrid_search` is the default because most real queries benefit from both — `scripts/vectorstore/01_index_and_search.py` runs the same three queries against all three modes side by side specifically to make this visible rather than asserted.

**Is `SET LOCAL hnsw.ef_search` actually safe with a connection pool?** Yes — `SET LOCAL` is scoped to the current transaction and reverts automatically when it ends, which is exactly why it's used instead of plain `SET` (which would persist for the lifetime of the pooled connection and could leak one query's tuning into the next unrelated query that happens to reuse the same pooled connection).

---

## Result

Unlike parsing, chunking, embedding and reranking, this stage doesn't have its own dedicated bake-off script — there's only one provider to compare against, so a leaderboard has nothing to rank. Its effect is measured indirectly, two ways:

1. **`benchmarks/retrieval_sweep.py`** exercises the full retrieval pipeline — which calls straight through to this stage's `hybrid_search` — and reports `recall@5`, `recall@50`, `mrr`, and `p50 ms` at various configurations. See the [retrieval stage](retrieval.md)'s Result table; the `p50 ms` column is the closest thing this stage has to a latency benchmark, since query latency here is dominated by the HNSW traversal and the SQL fusion query.
2. **`store.health()`** and Postgres's own `EXPLAIN ANALYZE` on the `HYBRID_SQL` statement are the practical tools for understanding *this* stage's own behavior in isolation — chunk/document counts, whether the HNSW index is actually being used by the query planner, and where time is spent inside the fused query.

---

## What breaks in production

- **HNSW recall degradation under heavy filtering.** If a `WHERE` clause is extremely selective (say, a component filter that only matches 0.1% of the table), HNSW's approximate graph traversal can struggle to find enough matching candidates within its normal search budget, and recall drops. `ef_search` is the per-session dial to raise when this is suspected — pgvector exposes it specifically so an operator can trade query latency for recall without rebuilding the index.
- **Re-ingestion duplicating the corpus.** Prevented by content-derived chunk ids plus `ON CONFLICT DO UPDATE` — but only for chunks whose *content* is unchanged. A document whose structure changed enough that its chunk boundaries shifted will produce genuinely new chunk ids for the shifted sections, and the *old* ids for that document are now orphans unless the ingest pipeline calls `chunk_ids_for_doc` and deletes what's no longer present. Skip that step and the corpus accumulates chunks from every past edit of every document — including superseded runbooks the system will happily cite as current.
- **A filter bug becomes a data leak, not an empty result.** This is the framing ADR-005 states explicitly, and it's why this stage has more than one layer of enforcement (see [ACL, in depth](#acl-in-depth)) rather than trusting `_where()` alone.
- **Non-English content in `tsv`.** `to_tsvector('english', ...)` applies English-specific stemming and stopword removal. A corpus with meaningful non-English text would see degraded (not necessarily zero, but meaningfully worse) lexical search for that content — this codebase's corpus is English-only, so this hasn't been addressed, but it would need either a per-document language column and a matching `tsvector` generation, or a language-agnostic full-text approach.
- **Connection pool exhaustion under real concurrent load.** `max_size=8` is a demo-appropriate default. A production deployment serving real concurrent traffic needs this raised deliberately, coordinated with Postgres's own `max_connections`, rather than discovered as a wall of "pool exhausted" errors during a traffic spike.
- **The DSN dialect-prefix mismatch.** `settings.pgvector_dsn` is written for SQLAlchemy's `postgresql+psycopg://` convention (shared with the rest of this codebase's config style), but the raw `psycopg` driver used here needs the plain `postgresql://` form. `PgVectorStore.__init__` strips the prefix; a new call site that constructs a DSN by hand and forgets this would fail immediately and loudly at connection time — not a silent failure, but worth knowing about if you see a `psycopg` connection error mentioning an unrecognized scheme.

---

## Files

| path | what |
|---|---|
| `sentinel/vectorstore/base.py` | `BaseVectorStore`, `SearchFilter`, `chunk_to_record`/`record_to_chunk`, the generic `hybrid_search` fallback |
| `sentinel/vectorstore/pgvector_store.py` | `PgVectorStore` — schema DDL, upsert, dense/sparse/hybrid search, ACL filter construction |
| `sentinel/core/types.py` | `Chunk`, `EmbeddedChunk`, `RetrievedChunk` — the shapes this stage stores and returns |
| `sentinel/core/resilience.py` | `AuthorizationViolation` — raised by `require_scope()` and `_assert_authorized()` |
| `sentinel/retrieval/pipeline.py` | `_assert_authorized()` — the second ACL enforcement layer, outside this stage |
| `scripts/vectorstore/01_index_and_search.py` | index + upsert + all three search modes side by side, on one small demo corpus |
| `scripts/vectorstore/02_acl_filtering.py` | the ACL demonstration script — same query, three roles, three different result sets, plus the fail-closed check |
| `config/init-sentinel-db.sql` | creates the `sentinelapp` role and `sentinel` database this table lives in |
| `config/init-readonly-role.sql` | grants a read-only role against the `sentinel` database |
| `tests/test_acl.py` | the release-gating ACL tests — restricted-content exclusion and non-disclosure of restricted content's existence |
| `docs/design-decisions.md` | ADR-005 (one index, filtered), ADR-006 (filters inside the query), ADR-029 (pgvector as the only maintained provider) |
