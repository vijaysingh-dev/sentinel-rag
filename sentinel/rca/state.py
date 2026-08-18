"""Graph state.

The `Annotated` reducers are the load-bearing detail. Knowledge, data and change specialists are
dispatched concurrently; without a merge function their simultaneous writes to the same state key
raise a concurrent-update error rather than accumulating. `operator.add` on a list means "append
both", which is exactly the semantics parallel evidence gathering needs.

Unchanged from the original Sentinel design except for `retrieval_mode` and `index_version`, which
are carried through so a degraded retrieval path or a stale index is visible in the final RCA
rather than being invisible context for a wrong conclusion.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

Severity = Literal["p1", "p2", "p3", "noise"]
AgentName = Literal["knowledge", "data", "change"]


class Evidence(BaseModel):
    """A single fact produced by a specialist, tied back to the tool output that produced it.

    `source_ref` is the join key that makes grounding mechanically checkable: the verifier drops
    any claim whose ref never appeared in a real tool result.
    """

    id: str
    claim: str
    source_tool: str
    source_ref: str
    raw_excerpt: str = Field(max_length=1200)
    agent: str
    confidence: float = Field(ge=0, le=1)


class RCADraft(BaseModel):
    summary: str = Field(max_length=600)
    root_cause: str = Field(max_length=1500)
    severity: Severity
    recommended_action: str = Field(max_length=800)
    evidence_ids: list[str] = []
    confidence: float = Field(ge=0, le=1)
    unresolved_questions: list[str] = []


class SentinelState(TypedDict, total=False):
    anomaly_id: str
    tenant_id: str
    anomaly: dict
    roles: list[str]  # authenticated scope; drives every retrieval filter

    messages: Annotated[list, add_messages]
    evidence: Annotated[list[Evidence], add]
    completed_agents: Annotated[list[str], add]

    plan: list[str]
    verifier_rounds: int
    verifier_notes: list[str]
    draft: RCADraft | None

    tokens_used: Annotated[int, add]
    usd_spent: Annotated[float, add]
    step_count: int
    errors: Annotated[list[dict], add]
    degraded: bool
    retrieval_mode: str  # full | dense_only | bm25_only | cache
    index_version: str
    approved_by: str
    outcome: str


def new_state(anomaly_id: str, tenant_id: str, anomaly: dict, roles: list[str] | None = None) -> SentinelState:
    from sentinel.core.config import settings

    return {
        "anomaly_id": anomaly_id,
        "tenant_id": tenant_id,
        "anomaly": anomaly,
        "roles": roles or ["admin"],
        "messages": [],
        "evidence": [],
        "completed_agents": [],
        "plan": [],
        "verifier_rounds": 0,
        "verifier_notes": [],
        "draft": None,
        "tokens_used": 0,
        "usd_spent": 0.0,
        "step_count": 0,
        "errors": [],
        "degraded": False,
        "retrieval_mode": "full",
        "index_version": settings.index_version,
        "approved_by": "",
        "outcome": "running",
    }
