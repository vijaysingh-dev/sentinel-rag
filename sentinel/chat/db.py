"""Database handle for the chat mediator's own tables.

Async connection pool for `chat_sessions`/`chat_messages`, used only by `sentinel/chat/session.py`
and `sentinel/chat/api.py` (schema bootstrap). Same `sentinel` Postgres database and `sentinelapp`
role as `sentinel/rca/checkpointer.py`'s pools — there is no separate chat database, just a
separate set of tables — but this module owns writing to those tables, the same way `rca/` owns
its checkpointer, so the two stay in the package that actually calls them rather than sharing one
`db.py` with no clear owner.

Async, not `psycopg_pool.ConnectionPool`'s sync flavour, because `sentinel/chat/api.py` is an
async FastAPI app serving concurrent SSE streams, where a blocking DB call would stall every other
in-flight session, not just the one making the call.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log

CHAT_DDL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    role             TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    context_summary  TEXT,
    summary_upto_seq BIGINT NOT NULL DEFAULT 0,
    title            TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    seq           BIGINT NOT NULL,
    turn_role     TEXT NOT NULL,
    content       TEXT NOT NULL,
    route         TEXT,
    rca_thread_id TEXT,
    rca_status    TEXT,
    latency_ms    INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_messages_session_seq_uidx ON chat_messages (session_id, seq);
"""


@lru_cache
def session_pool() -> Any:
    """Async connection pool for `chat_sessions`/`chat_messages`.

    Same `sentinel` database, same `sentinelapp` role as `rca/checkpointer.py`'s pools — async
    instead of sync because its only caller, `sentinel/chat/api.py`, is an async FastAPI app
    serving concurrent SSE streams.

    Every connection's `search_path` is pinned to `settings.pg_schema` (`test` under
    `settings.debug`, `public` otherwise) via connection `options`, rather than qualifying every
    query with a schema name — this is what keeps a locally-tested chat session out of the same
    tables the real UI's sidebar reads, without every call site in `session.py` needing to know
    about it.
    """
    from psycopg_pool import AsyncConnectionPool

    dsn = settings.pgvector_dsn.replace("postgresql+psycopg://", "postgresql://")
    return AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"options": f"-c search_path={settings.pg_schema}"},
    )


async def ensure_chat_schema() -> None:
    """Creates `settings.pg_schema` if needed and runs `CHAT_DDL` once against it.

    Called at `sentinel/chat/api.py` startup, not on every request. `config/init-chat-tables.sql`
    carries the same DDL for a human to run by hand against the `public` schema — keep the two
    textually identical, the same convention `vectorstore/pgvector_store.py`'s self-provisioning
    DDL follows for its own table. The `test` schema has no such checked-in file: it is meant to
    be disposable, created and dropped on a dev machine, never something a human runs by hand.
    """
    pool = session_pool()
    if pool.closed:
        await pool.open(wait=True)
    async with pool.connection() as conn:
        if settings.debug:
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {settings.pg_schema}")
        await conn.execute(CHAT_DDL)
    log.info("chat_schema_ready", schema=settings.pg_schema)
