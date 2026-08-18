"""The three specialists.

Each is a small ReAct agent with a **disjoint tool scope**. That isolation is the reason this is a
multi-agent system rather than one agent with eleven tools:

* Tool-selection accuracy degrades as the tool count grows. Three tools chosen from three beats
  three chosen from eleven.
* A compromised or confused specialist can only reach its own tools. The knowledge specialist
  cannot execute SQL because it does not hold that tool, not because a prompt told it not to.
* Each specialist's context holds only its own tool output, so transcripts stay small and cheap.

The supervisor holds **no** tools at all, by construction.

Agents are built lazily inside the node rather than at module import. That matters more than it
looks: `graph.py` imports these nodes, so eager construction would make `build_graph` — and every
test that imports it — require a reachable LLM provider. Import-time side effects that reach the
network are a recurring source of untestable code.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sentinel.core.registry import get_provider
from sentinel.rca.agents.common import make_specialist
from sentinel.rca.prompts import load_prompt
from sentinel.rca.tools.change import corporate_actions, feed_status, recent_deployments
from sentinel.rca.tools.retrieval import search_knowledge
from sentinel.rca.tools.warehouse import run_readonly_sql

# The tool scope of each specialist, declared as data so the disjointness is checkable rather than
# scattered across three constructors. `tests/test_trajectory.py` asserts these sets do not overlap.
TOOL_SCOPES: dict[str, list[Any]] = {
    "knowledge": [search_knowledge],
    "data": [run_readonly_sql],
    "change": [recent_deployments, corporate_actions, feed_status],
}

PROMPT_VERSIONS = {"knowledge": "v4", "data": "v4", "change": "v4"}

FOCUS = {
    "knowledge": "documented explanations, methodology and prior incidents matching this symptom",
    "data": "what the numbers show: the index against its history, its peers, and its upstream feeds",
    "change": "what changed shortly before the anomaly: deployments, config, corporate actions",
}


@lru_cache
def build_agent(name: str) -> Any:
    """Compiles a specialist on first use.

    The analysis model is deliberately the strong one rather than the cheap one: specialists do the
    actual reasoning about evidence, and this is not where to economise. The cheap model handles
    routing and extraction instead, which is where the call volume is.
    """
    from langgraph.prebuilt import create_react_agent

    return create_react_agent(
        get_provider("llm").chat,
        tools=TOOL_SCOPES[name],
        prompt=load_prompt(name, PROMPT_VERSIONS[name]),
    )


knowledge_node = make_specialist("knowledge", lambda: build_agent("knowledge"), FOCUS["knowledge"])
data_node = make_specialist("data", lambda: build_agent("data"), FOCUS["data"])
change_node = make_specialist("change", lambda: build_agent("change"), FOCUS["change"])
