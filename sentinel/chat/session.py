"""Session/message persistence and context-window construction.

This is "Sentinel owns history": `sentinel/chat/api.py` never replays a growing raw message list
into the LLM, and the frontend never sees the raw table — this module is the one place that
answers "what goes to the model this turn."

`seq`, not `created_at`, orders messages: wall-clock timestamps from the app server are not
guaranteed strictly increasing under concurrent writes, and `seq` is what "last N messages" means
unambiguously. It is assigned inside the same transaction that inserts the row, so "log before any
backend call, log again after" (the user's own requirement) falls out of calling `append_message`
at the start and end of the request handler — nothing here needs to track that separately.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sentinel.chat.db import session_pool
from sentinel.core.observability import log

ROLES = ("viewer", "oncall", "sre", "admin")


async def create_session(*, tenant_id: str, role: str) -> str:
    session_id = str(uuid.uuid4())
    async with session_pool().connection() as conn:
        await conn.execute(
            "INSERT INTO chat_sessions (session_id, tenant_id, role) VALUES (%s, %s, %s)",
            (session_id, tenant_id, role),
        )
    log.info("chat_session_created", session_id=session_id, role=role)
    return session_id


async def get_session(session_id: str) -> dict[str, Any] | None:
    async with session_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, tenant_id, role, context_summary, summary_upto_seq, title "
            "FROM chat_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row, strict=True))


async def list_sessions(*, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with session_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, role, title, updated_at FROM chat_sessions "
            "WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT %s",
            (tenant_id, limit),
        )
        rows = await cur.fetchall()
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in rows]


async def append_message(
    session_id: str,
    *,
    turn_role: Literal["user", "assistant"],
    content: str,
    route: str | None = None,
    rca_thread_id: str | None = None,
    rca_status: str | None = None,
    latency_ms: int | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    """Inserts one turn, assigning `seq` atomically within the same transaction.

    Also sets the session's `title` from the first user message and bumps `updated_at` — both
    side effects of the same write, not a separate query, since every append already touches this
    session row.
    """
    import json

    async with session_pool().connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_messages WHERE session_id = %s",
            (session_id,),
        )
        (seq,) = await cur.fetchone()
        await conn.execute(
            """
                INSERT INTO chat_messages
                    (session_id, seq, turn_role, content, route, rca_thread_id, rca_status,
                     latency_ms, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
            (
                session_id,
                seq,
                turn_role,
                content,
                route,
                rca_thread_id,
                rca_status,
                latency_ms,
                json.dumps(meta or {}),
            ),
        )
        await conn.execute(
            "UPDATE chat_sessions SET updated_at = now(), "
            "title = COALESCE(title, CASE WHEN %s = 'user' THEN %s END) "
            "WHERE session_id = %s",
            (turn_role, content[:120], session_id),
        )
    return seq


async def recent_messages(session_id: str, *, limit: int) -> list[dict[str, Any]]:
    async with session_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT seq, turn_role, content, route FROM chat_messages "
            "WHERE session_id = %s ORDER BY seq DESC LIMIT %s",
            (session_id, limit),
        )
        rows = await cur.fetchall()
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in reversed(rows)]


async def all_messages(session_id: str) -> list[dict[str, Any]]:
    async with session_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT seq, turn_role, content, route, meta, created_at FROM chat_messages "
            "WHERE session_id = %s ORDER BY seq ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in rows]


async def build_context(session_id: str, *, raw_window: int | None = None) -> str:
    """Last `raw_window` raw turns + the session's rolling summary, rendered as one text block.

    Fed to both the router's LLM fallback (as `recent_context`) and the rag/tool_call generation
    prompt. RCA doesn't consume this the same way — each investigation is seeded from one message
    plus a synthesized anomaly dict, not the running chat history — except when recovering from a
    router clarifying question, where the prior turn is exactly what supplies the service name.
    """
    from sentinel.core.config import settings

    window = raw_window or settings.chat_raw_window
    session = await get_session(session_id)
    turns = await recent_messages(session_id, limit=window)

    parts = []
    if session and session.get("context_summary"):
        parts.append(f"Summary of earlier conversation:\n{session['context_summary']}")
    for t in turns:
        parts.append(f"{t['turn_role']}: {t['content']}")
    return "\n\n".join(parts)


async def maybe_resummarize(session_id: str) -> None:
    """No-ops unless the unsummarized backlog crosses `settings.chat_resummarize_threshold`.

    Runs synchronously, right after the assistant turn is persisted — this repo has no task-queue
    infrastructure, and standing one up for a single periodic per-session step isn't proportionate.
    The cost is one cheap COUNT on every turn, and one LLM call only on the rare turn that crosses
    the threshold; that tail latency lands on the conversation that just got long, which is a
    defensible place for it to land.

    Cumulative, not "re-read everything": only messages past `summary_upto_seq` are folded onto
    the *existing* summary, so the work done per call is bounded by `threshold`, not by how long
    the conversation has grown.
    """
    from sentinel.chat.prompts import load_prompt
    from sentinel.core.config import settings
    from sentinel.core.registry import get_provider

    session = await get_session(session_id)
    if session is None:
        return

    async with session_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT seq, turn_role, content FROM chat_messages "
            "WHERE session_id = %s AND seq > %s ORDER BY seq ASC",
            (session_id, session["summary_upto_seq"]),
        )
        rows = await cur.fetchall()

    if len(rows) <= settings.chat_resummarize_threshold:
        return

    cols = ["seq", "turn_role", "content"]
    unsummarized = [dict(zip(cols, r, strict=True)) for r in rows]
    transcript = "\n".join(f"{m['turn_role']}: {m['content']}" for m in unsummarized)
    prompt = load_prompt("session_summary", "v1").format(
        prior_summary=session.get("context_summary") or "(none yet)",
        transcript=transcript,
    )

    summary = await get_provider("llm").cheap.ainvoke(prompt)
    new_upto = unsummarized[-1]["seq"]

    async with session_pool().connection() as conn:
        await conn.execute(
            "UPDATE chat_sessions SET context_summary = %s, summary_upto_seq = %s "
            "WHERE session_id = %s",
            (summary.content, new_upto, session_id),
        )
    log.info("chat_session_resummarized", session_id=session_id, folded=len(unsummarized))
