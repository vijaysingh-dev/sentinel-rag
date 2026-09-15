"""SSE event framing for `POST /chat/message`.

One tiny helper so `api.py` never hand-builds a `data: ...\\n\\n` frame inline, and one flat
`ChatEvent` union so the frontend never needs to know what a LangGraph checkpoint or interrupt is
— `awaiting_input`/`awaiting_approval` are the only RCA-specific event types, and both carry plain
JSON.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict


class ChatEvent(TypedDict, total=False):
    type: Literal[
        "session", "route", "token", "step", "awaiting_input", "awaiting_approval", "refs", "error", "done"
    ]
    session_id: str
    route: str
    text: str
    question: str
    thread_id: str
    draft: dict
    degraded: bool
    retrieval_mode: str
    evidence: list[dict]
    usd_spent: float
    actions: list[str]
    refs: list[str]
    message: str
    node: str
    summary: str


def format_event(event: ChatEvent) -> bytes:
    """Renders one `ChatEvent` as an SSE frame (`data: <json>\\n\\n`).

    A single `data:` line rather than named SSE `event:` types — the frontend's manual
    `fetch`+reader parser only needs to split on blank lines and JSON-decode, and `type` inside
    the payload is enough to dispatch on. Named SSE events would buy nothing here since there is
    exactly one stream per request, not several event channels multiplexed on one connection.
    """
    return f"data: {json.dumps(event)}\n\n".encode()


def format_error(message: str) -> bytes:
    return format_event({"type": "error", "message": message})


def format_done() -> bytes:
    return format_event({"type": "done"})
