"""Database handles for the RCA subsystem.

Two separate pools, and the separation is the point:

* **Warehouse pool** — connects as a role with SELECT-only grants. Dormant since ADR-027 moved
  the agent's numeric evidence from a SQL warehouse to Prometheus (`tools/metrics.py`), but kept
  intact: if a SQL warehouse tool is wired back in for a use the metrics API cannot serve, this
  is the layer that guarantees it is read-only — the query validation is only defence in depth on
  top of the grant.
* **Checkpoint pool** — Postgres-backed LangGraph checkpointer, so an interrupted run survives a
  restart and a human can approve it hours later from a different process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sentinel.core.config import settings


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


@lru_cache
def checkpointer() -> Any:
    """Durable checkpointer for the agent graph.

    Falls back to an in-memory saver when Postgres is unreachable so the graph still runs on a
    laptop — but with a loud warning, because in-memory checkpointing silently breaks the human
    interrupt across restarts, which is the kind of thing that works in dev and fails in prod.
    """
    from sentinel.core.observability import log

    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        dsn = settings.pgvector_dsn.replace("postgresql+psycopg://", "postgresql://")
        saver = PostgresSaver.from_conn_string(dsn)
        saver.setup()
        log.info("checkpointer_ready", kind="postgres")
        return saver
    except Exception as exc:
        from langgraph.checkpoint.memory import MemorySaver

        log.warning(
            "checkpointer_fallback",
            kind="memory",
            reason=str(exc)[:160],
            impact="interrupted runs will not survive a restart",
        )
        return MemorySaver()
