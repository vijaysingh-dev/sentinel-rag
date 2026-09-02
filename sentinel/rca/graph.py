"""Topology.

Control flow is expressed as `Command(goto=...)` returned from nodes rather than as static edges,
because the supervisor decides the path at runtime. The `destinations` hints keep the compiled
graph drawable and make the intended topology reviewable in one place — which matters, because a
dynamic graph is otherwise only understandable by reading every node.

    intake → supervisor ⇄ {knowledge, data, change}
                      ↘ verifier ⇄ supervisor
                      ↘ writer → approval → publish
"""

from __future__ import annotations

import contextlib
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, RetryPolicy

from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.rca.agents.specialists import change_node, knowledge_node, metrics_node, tracing_node
from sentinel.rca.agents.supervisor import supervisor_node
from sentinel.rca.agents.verifier import verifier_node
from sentinel.rca.agents.writer import approval_node, publish_node, writer_node
from sentinel.rca.state import SentinelState

REQUIRED_ANOMALY_FIELDS = ("alerting_service", "started_at", "signal")


class TransientError(Exception):
    """Retryable failure class for the node-level retry policy."""


async def intake_node(state: SentinelState) -> Command:
    """Deterministic entry: validate the payload and normalise what the agents rely on.

    Kept free of LLM calls so a malformed alert fails fast and for nothing. An alerting system
    misconfiguration should not cost a dollar per event to discover.
    """
    anomaly = state.get("anomaly") or {}
    missing = [f for f in REQUIRED_ANOMALY_FIELDS if f not in anomaly]
    if missing:
        return Command(
            goto="__end__",
            update={
                "outcome": "invalid",
                "errors": [{"node": "intake", "kind": "missing_fields", "detail": ",".join(missing)}],
            },
        )

    if settings.kill_switch:
        # A per-environment switch that stops all agent execution without a deploy. Worth having
        # before you need it, because the moment you need it you will not want to wait for CI.
        log.warning("kill_switch_active", anomaly=state["anomaly_id"])
        return Command(goto="__end__", update={"outcome": "disabled"})

    # An optional numeric magnitude, when the alert carried one — "error ratio rose to 6%". Kept
    # if present so the supervisor can gauge severity, but not required: many alerts are a
    # threshold crossing with no clean before/after number.
    magnitude = None
    with contextlib.suppress(KeyError, TypeError, ValueError):
        magnitude = abs(float(anomaly["observed"]) - float(anomaly["expected"]))

    return Command(goto="supervisor", update={"anomaly": {**anomaly, "magnitude": magnitude}})


def _builder() -> Any:
    """The node and edge wiring, in one place so every caller compiles the same graph."""
    builder = StateGraph(SentinelState)

    # Retries here cover transport-level flakiness only. Provider retries live in
    # core/resilience.py, and layering both would compound the backoff.
    retry = RetryPolicy(
        max_attempts=3,
        initial_interval=1.0,
        backoff_factor=2.0,
        retry_on=(TransientError, TimeoutError, ConnectionError),
    )

    builder.add_node("intake", intake_node, destinations=("supervisor", "__end__"))
    builder.add_node(
        "supervisor",
        supervisor_node,
        destinations=(
            "knowledge_node",
            "metrics_node",
            "tracing_node",
            "change_node",
            "verifier",
            "writer",
        ),
    )
    builder.add_node("knowledge_node", knowledge_node, retry=retry, destinations=("supervisor",))
    builder.add_node("metrics_node", metrics_node, retry=retry, destinations=("supervisor",))
    builder.add_node("tracing_node", tracing_node, retry=retry, destinations=("supervisor",))
    builder.add_node("change_node", change_node, retry=retry, destinations=("supervisor",))
    builder.add_node("verifier", verifier_node, destinations=("supervisor", "writer"))
    builder.add_node("writer", writer_node, destinations=("approval", "__end__"))
    builder.add_node("approval", approval_node, destinations=("publish", "__end__"))
    builder.add_node("publish", publish_node, destinations=("__end__",))

    builder.add_edge(START, "intake")
    return builder


def build_graph(checkpointer: BaseCheckpointSaver) -> Any:
    """Compiles the investigation graph with an explicit checkpointer.

    The checkpointer is not optional here. Without durable checkpointing the human interrupt
    cannot work: the run would have to be held in memory for however long the analyst takes, and
    any restart would lose it. This is what `api.py` and the eval harness use.
    """
    return _builder().compile(checkpointer=checkpointer)


def graph() -> Any:
    """Zero-argument factory for the LangGraph platform (`langgraph dev`, `langgraph.json`).

    The platform supplies and manages persistence itself, so this compiles without a checkpointer
    and the dev server injects one. Same nodes, same interrupt at `approval`. The `web/` chat UI
    connects here.
    """
    return _builder().compile()
