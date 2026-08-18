"""HTTP surface.

Three endpoints, and the split between them is the architecture:

* ``POST /v1/investigations`` starts a run and returns **immediately**. An RCA takes tens of
  seconds to minutes; a synchronous request would time out at every proxy between the client and
  the graph.
* ``GET  /v1/investigations/{id}/stream`` streams progress over SSE, so the analyst sees the
  investigation happening rather than staring at a spinner. Perceived latency is a real product
  property, not a cosmetic one.
* ``POST /v1/investigations/{id}/decision`` resumes an interrupted run with the human's decision.

The decision endpoint is what makes the interrupt work end to end: the graph state was checkpointed
when `interrupt()` fired, and `Command(resume=...)` picks it up from there — possibly hours later,
possibly in a different process.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sentinel.core.observability import configure_logging, log
from sentinel.rca.db import checkpointer
from sentinel.rca.graph import build_graph
from sentinel.rca.runtime import request_context
from sentinel.rca.state import new_state

app = FastAPI(title="Sentinel RCA", version="2.0")


class StartRequest(BaseModel):
    anomaly_id: str
    tenant_id: str = "default"
    anomaly: dict
    roles: list[str] = Field(default_factory=lambda: ["admin"])


class DecisionRequest(BaseModel):
    action: str = Field(pattern="^(approve|edit|reject)$")
    reviewer: str = "unknown"
    reason: str = ""
    draft: dict | None = None


@app.on_event("startup")
async def startup() -> None:
    configure_logging()
    app.state.graph = build_graph(checkpointer())
    log.info("api_ready")


def _config(anomaly_id: str) -> dict:
    """Thread id keys the checkpoint.

    Using the anomaly id makes a re-submitted alert resume the existing investigation rather than
    starting a duplicate — idempotency for free, which matters because alerting systems retry.
    """
    return {"configurable": {"thread_id": anomaly_id}}


@app.post("/v1/investigations", status_code=202)
async def start(request: StartRequest, background: BackgroundTasks) -> dict:
    async def run() -> None:
        with request_context(request.tenant_id, request.roles):
            try:
                await app.state.graph.ainvoke(
                    new_state(request.anomaly_id, request.tenant_id, request.anomaly, request.roles),
                    config=_config(request.anomaly_id),
                )
            except Exception:
                log.exception("investigation_failed", anomaly=request.anomaly_id)

    background.add_task(run)
    return {"anomaly_id": request.anomaly_id, "status": "started"}


@app.get("/v1/investigations/{anomaly_id}")
async def get_state(anomaly_id: str) -> dict:
    snapshot = await app.state.graph.aget_state(_config(anomaly_id))
    if not snapshot.values:
        raise HTTPException(404, "no such investigation")

    values = snapshot.values
    draft = values.get("draft")
    return {
        "anomaly_id": anomaly_id,
        "outcome": values.get("outcome", "running"),
        "degraded": values.get("degraded", False),
        "retrieval_mode": values.get("retrieval_mode", "full"),
        "evidence_count": len(values.get("evidence", [])),
        "usd_spent": round(values.get("usd_spent", 0.0), 4),
        "draft": draft.model_dump() if draft else None,
        # A non-empty `next` on an interrupted graph means it is waiting for a human.
        "awaiting_decision": bool(snapshot.next) and values.get("draft") is not None,
    }


@app.get("/v1/investigations/{anomaly_id}/stream")
async def stream(anomaly_id: str) -> StreamingResponse:
    """Server-sent events carrying node-level progress.

    Only node names and counts are emitted, never evidence content: this endpoint is easier to
    expose than the full state and should not become an accidental data-leak path.
    """

    async def events() -> Any:
        seen: set[str] = set()
        for _ in range(600):  # ~10 minutes at one poll per second
            snapshot = await app.state.graph.aget_state(_config(anomaly_id))
            values = snapshot.values or {}

            for agent in values.get("completed_agents", []):
                if agent not in seen:
                    seen.add(agent)
                    yield f"data: {json.dumps({'event': 'agent_complete', 'agent': agent})}\n\n"

            outcome = values.get("outcome", "running")
            if outcome != "running":
                yield f"data: {json.dumps({'event': 'done', 'outcome': outcome})}\n\n"
                return

            if values.get("draft") and snapshot.next:
                yield f"data: {json.dumps({'event': 'awaiting_decision'})}\n\n"
                return

            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/v1/investigations/{anomaly_id}/decision")
async def decide(anomaly_id: str, request: DecisionRequest) -> dict:
    """Resumes an interrupted run with the analyst's decision."""
    from langgraph.types import Command

    snapshot = await app.state.graph.aget_state(_config(anomaly_id))
    if not snapshot.values:
        raise HTTPException(404, "no such investigation")
    if not snapshot.next:
        raise HTTPException(409, "investigation is not awaiting a decision")

    payload = {"action": request.action, "reviewer": request.reviewer, "reason": request.reason, "draft": request.draft}

    tenant = snapshot.values.get("tenant_id", "default")
    roles = snapshot.values.get("roles", ["admin"])

    with request_context(tenant, roles):
        result = await app.state.graph.ainvoke(Command(resume=payload), config=_config(anomaly_id))

    log.info("decision_applied", anomaly=anomaly_id, action=request.action, reviewer=request.reviewer)
    return {"anomaly_id": anomaly_id, "outcome": result.get("outcome")}


@app.get("/health")
async def health() -> dict:
    from sentinel.core.registry import get_provider

    try:
        store_health = get_provider("vectorstore").health()
    except Exception as exc:
        store_health = {"ok": False, "error": str(exc)[:160]}
    return {"ok": store_health.get("ok", False), "vectorstore": store_health}
