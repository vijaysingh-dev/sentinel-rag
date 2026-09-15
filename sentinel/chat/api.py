"""HTTP+SSE API for the chat mediator — the only module here that knows about HTTP framing.

Sentinel sits between `web/` and everything else (system-monitor/incident-generator, the
retrieval pipeline, the RCA graph). `web/` never talks to Postgres, never talks to `:2024`
(LangGraph's own dev server), and never sees a LangGraph checkpoint or interrupt as a concept —
this module and `sse.py`'s flat `ChatEvent` union are the seam that hides all of that.

Every message is persisted before classification runs and again after the backend call
completes, regardless of which of the four routes it took — "log before, log after" from the
user's own requirement, satisfied by `session.append_message` bracketing the whole handler.

Each route handler (`_tool_call_reply`, `_rag_reply`, `_rca_reply`) is an async generator that
yields only SSE-frame bytes and returns a `RouteResult` (via `StopAsyncIteration`'s value is not
used — instead each handler appends its result to the caller-supplied `out` list) once it is
exhausted. Keeping "what to stream" and "what to persist" as two separate channels, rather than
overloading the yielded value to carry both, is what keeps `_stream_message` readable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from sentinel.chat import session
from sentinel.chat.db import ensure_chat_schema
from sentinel.chat.router import Route, RouteDecision, ToolTarget, classify
from sentinel.chat.sse import format_done, format_error, format_event
from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.rca.runtime import request_context


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await ensure_chat_schema()
    yield


app = FastAPI(title="sentinel-chat", lifespan=_lifespan)


class MessageRequest(BaseModel):
    session_id: str | None = None
    role: str
    message: str


class ResumeRequest(BaseModel):
    session_id: str
    thread_id: str
    action: str  # "approve" | "edit" | "reject"
    reviewer: str
    reason: str | None = None
    edited_draft: dict | None = None


@app.get("/chat/sessions")
async def get_sessions() -> list[dict]:
    return await session.list_sessions(tenant_id=settings.chat_default_tenant_id)


@app.get("/chat/sessions/{session_id}/messages")
async def get_messages(session_id: str) -> list[dict]:
    return await session.all_messages(session_id)


@app.post("/chat/message")
async def post_message(body: MessageRequest, request: Request) -> StreamingResponse:
    if body.role not in session.ROLES:
        raise HTTPException(400, f"unknown role: {body.role}")

    return StreamingResponse(_stream_message(body, request), media_type="text/event-stream")


@dataclass
class RouteResult:
    reply_text: str = ""
    rca_thread_id: str | None = None
    rca_status: str | None = None
    meta: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = {}


async def _stream_message(body: MessageRequest, request: Request):
    tenant_id = settings.chat_default_tenant_id
    session_id = body.session_id
    if session_id is None:
        session_id = await session.create_session(tenant_id=tenant_id, role=body.role)
        yield format_event({"type": "session", "session_id": session_id})

    await session.append_message(session_id, turn_role="user", content=body.message)

    try:
        context = await session.build_context(session_id)
        decision = await classify(body.message, context)
        yield format_event({"type": "route", "route": decision.route.value})

        result = RouteResult(meta={"reason": decision.reason})

        if decision.route == Route.TOOL_CALL:
            async for frame in _tool_call_reply(decision, result):
                yield frame
        elif decision.route == Route.RAG:
            async for frame in _rag_reply(body.message, context, body.role, tenant_id, result):
                yield frame
        elif decision.route == Route.RCA:
            async for frame in _rca_reply(body.message, decision, body.role, tenant_id, result, request):
                yield frame
        else:
            async for frame in _refuse_reply(result):
                yield frame

        # The user may have clicked stop while the route handler above was still running — an
        # investigation stopped mid-flight has no reply worth persisting or announcing as done.
        if await request.is_disconnected():
            return

        await session.append_message(
            session_id,
            turn_role="assistant",
            content=result.reply_text,
            route=decision.route.value,
            rca_thread_id=result.rca_thread_id,
            rca_status=result.rca_status,
            meta=result.meta,
        )
        await session.maybe_resummarize(session_id)
        yield format_done()

    except Exception as exc:
        from sentinel.core.resilience import SentinelError

        log.error("chat_message_failed", session_id=session_id, error=type(exc).__name__)
        if isinstance(exc, SentinelError):
            message_text = exc.user_message
        else:
            message_text = "Something went wrong handling that message."
        yield format_error(message_text)


async def _refuse_reply(result: RouteResult):
    result.reply_text = (
        "I can help with questions about the OpenTelemetry Demo platform — its services, health, "
        "incidents, and runbooks. That's outside what I can help with."
    )
    yield format_event({"type": "token", "text": result.reply_text})


async def _tool_call_reply(decision: RouteDecision, result: RouteResult):
    """Deterministic template, no LLM call — the fastest possible answer for structured data."""
    from sentinel.rca.clients import incident_log_client, system_monitor_client

    if decision.tool_target == ToolTarget.SYSTEM_HEALTH:
        health = await system_monitor_client().nodes()
        nodes = health.get("nodes", {})
        unhealthy = [n["display_name"] for n in nodes.values() if n["status"] != "healthy"]
        if unhealthy:
            text = f"{len(unhealthy)} of {len(nodes)} services need attention: {', '.join(unhealthy)}."
        else:
            text = f"All {len(nodes)} services are healthy as of just now."
    else:
        incidents = await incident_log_client().recent_incidents(5)
        if not incidents:
            text = "No incidents have been triggered recently."
        else:
            latest = incidents[-1]
            text = f"Most recent incident: {latest['scenario']} ({latest['event']}) at {latest['ts']}."

    result.reply_text = text
    yield format_event({"type": "token", "text": text})


async def _rag_reply(message: str, context: str, role: str, tenant_id: str, result: RouteResult):
    import asyncio

    from sentinel.retrieval.pipeline import RetrievalPipeline

    # A short follow-up ("what about payment specifically?") carries no retrievable signal on its
    # own — the service/topic it refers back to lives in the prior turn, not in this message.
    # Folding recent context into the retrieval query itself (not just the generation prompt)
    # gives the dense/BM25 search something to match against; the generation prompt below still
    # gets `context` separately since a search query and a chat prompt want different framing.
    retrieval_query = f"{context}\n\n{message}" if context else message

    with request_context(tenant_id, [role]):
        # RetrievalPipeline.retrieve is sync; run it off the event loop so one slow retrieval
        # doesn't stall every other session's SSE stream on this process.
        results = await asyncio.to_thread(
            RetrievalPipeline().retrieve,
            retrieval_query,
            roles=[role],
            tenant_id=tenant_id,
            context_budget_tokens=4000,
        )

    if not results.chunks:
        # Same discipline as rca/tools/retrieval.py's search_knowledge: an empty result is not
        # licence to answer from general knowledge.
        result.reply_text = "That's not documented in what I have indexed."
        yield format_event({"type": "token", "text": result.reply_text})
        return

    from sentinel.core.registry import get_provider
    from sentinel.core.resilience import with_retry

    prompt = (
        f"Conversation so far:\n{context}\n\n"
        f"Documentation:\n{results.context}\n\n"
        f"Question: {message}\n\n"
        "Answer using only the documentation above. Cite refs inline where you use them."
    )

    # A transient 503/429 almost always happens on the connection, before the first token comes
    # back — retrying there is safe (nothing has been sent to the client yet). Once streaming has
    # started, a mid-stream failure can't be silently retried without either replaying already-sent
    # tokens or risking a duplicated answer, so it surfaces as an error event instead.
    async def _open_stream():
        stream = get_provider("llm").chat.astream(prompt)
        first = await stream.__anext__()
        return stream, first

    reply_parts: list[str] = []
    try:
        stream, first_token = await with_retry(_open_stream, operation="rag_reply")
    except StopAsyncIteration:
        stream, first_token = None, None
    except Exception as exc:
        log.error("rag_reply_failed", error=type(exc).__name__)
        yield format_error("The language model isn't responding right now. Please try again.")
        return

    if first_token is not None:
        reply_parts.append(first_token.content)
        yield format_event({"type": "token", "text": first_token.content})

    if stream is not None:
        try:
            async for token in stream:
                reply_parts.append(token.content)
                yield format_event({"type": "token", "text": token.content})
        except Exception as exc:
            log.error("rag_reply_stream_interrupted", error=type(exc).__name__)
            yield format_error("The response was interrupted. Please try again.")
            return

    result.reply_text = "".join(reply_parts)
    result.meta["refs"] = results.refs
    yield format_event({"type": "refs", "refs": results.refs})


_STEP_LABELS = {
    "intake": "Validating the alert",
    "supervisor": "Deciding what to check next",
    "knowledge_node": "Searching documentation and runbooks",
    "metrics_node": "Querying Prometheus metrics",
    "tracing_node": "Querying Jaeger traces",
    "change_node": "Checking recent flags, deploys and incidents",
    "verifier": "Checking evidence against the claims",
    "writer": "Drafting the root-cause summary",
    "approval": "Waiting for human approval",
    "publish": "Publishing the finding",
}


def _step_summary(node: str, update: dict) -> str:
    """One line describing what a node just did, for the UI's thinking loader.

    Reuses the same evidence/reasoning fields `supervisor.py`'s `routed` structlog event logs —
    this is that same information, just addressed to the chat stream instead of stderr.
    """
    label = _STEP_LABELS.get(node, node)
    new_evidence = update.get("evidence")
    if new_evidence:
        return f"{label} — found {len(new_evidence)} piece(s) of evidence"
    errors = update.get("errors")
    if errors:
        return f"{label} — {errors[-1].get('kind', 'error')}"
    return label


async def _rca_reply(
    message: str, decision: RouteDecision, role: str, tenant_id: str, result: RouteResult, request: Request
):
    if decision.clarifying_question:
        result.reply_text = decision.clarifying_question
        result.rca_status = "needs_service"
        yield format_event({"type": "awaiting_input", "question": decision.clarifying_question})
        return

    from sentinel.chat.anomaly import build_from_decision
    from sentinel.rca.checkpointer import checkpointer
    from sentinel.rca.graph import build_graph
    from sentinel.rca.state import new_state

    anomaly = build_from_decision(message, decision)
    thread_id = str(uuid4())

    final: dict = {}
    with request_context(tenant_id, [role]):
        state = new_state(thread_id, tenant_id, anomaly, [role])
        graph = build_graph(await checkpointer())
        stream = graph.astream(
            state, config={"configurable": {"thread_id": thread_id}}, stream_mode="updates"
        )
        try:
            async for chunk in stream:
                # An investigation can run for minutes across several specialist rounds; checking
                # here (rather than only after the loop) is what makes "stop" actually stop the
                # graph mid-run instead of just abandoning a client no one is pumping tokens to.
                if await request.is_disconnected():
                    log.info("rca_reply_client_disconnected", anomaly=state["anomaly_id"])
                    result.rca_status = "stopped"
                    return
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        final["__interrupt__"] = update
                        continue
                    final.update(update)
                    yield format_event({"type": "step", "node": node, "summary": _step_summary(node, update)})
        finally:
            await stream.aclose()

    # A dict-returning "updates" stream merges interrupts under the "__interrupt__" key when the
    # run paused inside approval_node — the stable, documented signal (importing the INTERRUPT
    # constant directly from langgraph.constants is deprecated as of LangGraph V1.0).
    interrupts = final.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value
        result.rca_thread_id = thread_id
        result.rca_status = "awaiting_approval"
        result.reply_text = f"Draft ready for review: {payload['draft'].get('summary', '')}"
        yield format_event(
            {
                "type": "awaiting_approval",
                "thread_id": thread_id,
                "draft": payload["draft"],
                "degraded": payload["degraded"],
                "retrieval_mode": payload["retrieval_mode"],
                "evidence": payload["evidence"],
                "usd_spent": payload["usd_spent"],
                "actions": payload["actions"],
            }
        )
        return

    outcome = final.get("outcome", "unknown")
    draft = final.get("draft")
    text = draft.summary if draft else f"Investigation ended: {outcome}."
    result.reply_text = text
    result.rca_thread_id = thread_id
    result.rca_status = outcome
    yield format_event({"type": "token", "text": text})


@app.post("/chat/resume")
async def post_resume(body: ResumeRequest) -> JSONResponse:
    from langgraph.types import Command

    from sentinel.rca.checkpointer import checkpointer
    from sentinel.rca.graph import build_graph

    parent = await session.get_session(body.session_id)
    if parent is None:
        raise HTTPException(404, "unknown session")

    with request_context(parent["tenant_id"], [parent["role"]]):
        graph = build_graph(await checkpointer())
        final = await graph.ainvoke(
            Command(
                resume={
                    "action": body.action,
                    "reviewer": body.reviewer,
                    "reason": body.reason,
                    "draft": body.edited_draft,
                }
            ),
            config={"configurable": {"thread_id": body.thread_id}},
        )

    outcome = final.get("outcome", "unknown")
    await session.append_message(
        body.session_id,
        turn_role="assistant",
        content=f"Investigation {outcome}.",
        route="rca",
        rca_thread_id=body.thread_id,
        rca_status=outcome,
    )
    return JSONResponse({"outcome": outcome})
