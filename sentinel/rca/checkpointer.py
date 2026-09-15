"""Database handles the RCA graph itself depends on.

Two pools, both against the `sentinel` Postgres database as the `sentinelapp` role — chat's own
tables (`chat_sessions`/`chat_messages`) live in `sentinel/chat/db.py` instead, since this module
is specifically what the graph needs to run and checkpoint, not a shared catch-all:

* **Warehouse pool** — connects as a role with SELECT-only grants. Dormant since ADR-027 moved
  the agent's numeric evidence from a SQL warehouse to Prometheus (`tools/metrics.py`), but kept
  intact: if a SQL warehouse tool is wired back in for a use the metrics API cannot serve, this
  is the layer that guarantees it is read-only — the query validation is only defence in depth on
  top of the grant.
* **Checkpoint saver** — Postgres-backed LangGraph checkpointer, so an interrupted run survives a
  restart and a human can approve it hours later from a different process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log


@lru_cache
def warehouse_pool() -> Any:
    """Read-only connection pool for generated SQL.

    `default_transaction_read_only` is belt and braces alongside the role grant: even a mistakenly
    over-privileged role cannot write through this pool.
    """
    from psycopg_pool import ConnectionPool

    dsn = settings.pgvector_dsn.replace("postgresql+psycopg://", "postgresql://")
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=6,
        open=True,
        kwargs={"options": "-c default_transaction_read_only=on -c statement_timeout=30000"},
    )


_checkpointer_cache: Any = None


async def checkpointer() -> Any:
    """Durable checkpointer for the agent graph.

    Falls back to an in-memory saver when Postgres is unreachable so the graph still runs on a
    laptop — but with a loud warning, because in-memory checkpointing silently breaks the human
    interrupt across restarts, which is the kind of thing that works in dev and fails in prod.

    Async, not `@lru_cache`, because every caller (`sentinel/chat/api.py`, the eval harness) is
    itself async and every graph invocation goes through `ainvoke`/`astream` — `PostgresSaver`'s
    `aget_tuple`/etc. are unimplemented stubs (`NotImplementedError`) by design; only
    `AsyncPostgresSaver` supports the async path a LangGraph `ainvoke()` actually takes. A plain
    module-level cache (not `@lru_cache`, which cannot wrap a coroutine function usefully) keeps
    this a process-lifetime singleton over one kept-open `AsyncConnection`, the async analogue of
    `warehouse_pool()`'s kept-open sync pool above.

    `AsyncPostgresSaver.from_conn_string` is an async contextmanager, meant for a short-lived
    `async with` block — not what a process-lifetime singleton needs. A directly-constructed
    `AsyncPostgresSaver(conn)` over a connection this function keeps open avoids exiting that
    context (and closing the connection under the checkpointer) the moment this function returns.
    """
    global _checkpointer_cache
    if _checkpointer_cache is not None:
        return _checkpointer_cache

    try:
        import psycopg
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import DictRow, dict_row

        dsn = settings.pgvector_dsn.replace("postgresql+psycopg://", "postgresql://")
        conn = await psycopg.AsyncConnection[DictRow].connect(
            dsn,
            autocommit=True,
            row_factory=dict_row,
            options=f"-c search_path={settings.pg_schema}",
        )
        if settings.debug:
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {settings.pg_schema}")
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
        log.info("checkpointer_ready", kind="postgres", schema=settings.pg_schema)
        _checkpointer_cache = saver
    except Exception as exc:
        from langgraph.checkpoint.memory import MemorySaver

        log.warning(
            "checkpointer_fallback",
            kind="memory",
            reason=str(exc)[:160],
            impact="interrupted runs will not survive a restart",
        )
        _checkpointer_cache = MemorySaver()

    return _checkpointer_cache
