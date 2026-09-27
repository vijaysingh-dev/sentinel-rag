"""Agent trajectory constraints.

These encode the architectural decisions that prompts alone cannot enforce. A constraint stated
only in a prompt survives until the next person edits that prompt; a constraint asserted in a test
survives indefinitely.
"""

from __future__ import annotations

import pytest


def test_supervisor_holds_no_tools():
    """The orchestrator must have no specialist tools, by construction.

    This is the constraint most likely to erode: someone adds a tool to the supervisor "just for
    triage" and the isolation argument for the whole multi-agent design quietly collapses.
    """
    import inspect

    from sentinel.rca.agents import supervisor

    source = inspect.getsource(supervisor)
    for tool in (
        "search_knowledge",
        "query_prometheus",
        "find_traces",
        "current_flags",
        "flag_changes",
        "recent_deployments",
        "incident_history",
    ):
        assert tool not in source, f"supervisor must not reference the {tool} tool"


def test_specialists_have_disjoint_tool_scopes():
    """No tool is reachable from more than one specialist.

    Asserted on the declared scopes rather than by inspecting source, so it keeps working through
    refactoring and states the actual invariant: overlap, not spelling.
    """
    from sentinel.rca.agents.specialists import TOOL_SCOPES

    seen: dict[str, str] = {}
    for specialist, tools in TOOL_SCOPES.items():
        for tool in tools:
            assert tool.name not in seen, f"{tool.name} is held by both {seen.get(tool.name)} and {specialist}"
            seen[tool.name] = specialist

    assert seen["query_prometheus"] == "metrics"
    assert seen["find_traces"] == "tracing"
    assert seen["search_knowledge"] == "knowledge"


@pytest.mark.parametrize(
    "expr",
    [
        "node_filesystem_free_bytes",  # node_ is not a platform prefix
        "some_secret_gauge",
        "kube_pod_status_ready",  # kube-state-metrics; there is no Kubernetes here
        'label_replace(up, "x", "$1", "job", "(.*)")',  # forbidden function
        # an off-allowlist metric followed by `)` used to be invisible to the extractor, so one
        # allowed metric anywhere in the expression let any other through
        "sum(up{}) + sum(secret_metric)",
        'topk(5, up{job="x"}) or topk(5, node_secret)',
        "up{job=\"a\"} and on(instance) node_load1",
        'up + {__name__="secret_metric"}',
    ],
)
def test_promql_validator_rejects_unknown_metrics_and_forbidden_funcs(expr):
    """A metric outside the platform allowlist, or a forbidden function, is rejected unrun."""
    from sentinel.rca.tools.metrics import UnsafeQuery, validate_promql

    with pytest.raises(UnsafeQuery):
        validate_promql(expr, range_hours=0.5)


@pytest.mark.parametrize(
    "expr",
    [
        # the span-metrics error ratio the metrics prompt and every runbook hand the model
        'sum(rate(traces_span_metrics_calls_total{service_name="checkout", span_kind="SPAN_KIND_SERVER", '
        'status_code="STATUS_CODE_ERROR"}[5m])) / sum(rate(traces_span_metrics_calls_total{service_name="checkout", '
        'span_kind="SPAN_KIND_SERVER"}[5m]))',
        'container_memory_usage_total_bytes{container_name="email"}',
        'kafka_consumer_group_lag_ratio{group="accounting"}',
        "postgresql_backends",
        # a bare metric inside an aggregation used to be rejected as "no metric name found"
        "sum by (service_name) (jvm_cpu_recent_utilization_ratio)",
        "max_over_time(process_memory_usage_bytes{service_name=\"recommendation\"}[30m:1m])",
    ],
)
def test_promql_validator_accepts_the_metrics_the_platform_actually_exposes(expr):
    """The allowlist was once written from upstream docs and rejected every working RED query."""
    from sentinel.rca.tools.metrics import validate_promql

    assert validate_promql(expr, range_hours=0.5)


def test_promql_validator_rejects_an_unbounded_or_wide_range():
    from sentinel.rca.tools.metrics import UnsafeQuery, validate_promql

    with pytest.raises(UnsafeQuery):
        validate_promql("rate(rpc_server_duration_milliseconds_count[5m])", range_hours=48)


def test_promql_validator_accepts_an_allowlisted_metric():
    from sentinel.rca.tools.metrics import validate_promql

    expr = (
        "sum(rate(rpc_server_duration_milliseconds_count"
        '{service_name="checkout", rpc_grpc_status_code!="0"}[5m]))'
    )
    assert validate_promql(expr, range_hours=0.5) == expr


def test_promql_validator_rejects_multiple_expressions():
    from sentinel.rca.tools.metrics import UnsafeQuery, validate_promql

    with pytest.raises(UnsafeQuery):
        validate_promql("up; up", range_hours=0.5)


def test_unreferenced_claims_are_dropped():
    """A claim citing a ref the tools never returned must not become evidence."""
    from langchain_core.messages import ToolMessage

    from sentinel.rca.agents.common import observed_refs

    messages = [ToolMessage(content='{"ref": "promql:abc123", "result": {}}', tool_call_id="1")]
    refs = observed_refs(messages)

    assert "promql:abc123" in refs
    assert "promql:fabricated" not in refs


def test_degraded_runs_cannot_claim_high_confidence():
    from sentinel.rca.agents.writer import DEGRADED_CONFIDENCE_CAP

    assert DEGRADED_CONFIDENCE_CAP <= 0.7


def test_publish_requires_recorded_approval():
    """The only write in the system must be unreachable without an approver."""
    import inspect

    from sentinel.rca.agents import writer

    source = inspect.getsource(writer.publish_node)
    assert 'state.get("approved_by")' in source
    assert "publish_without_approval" in source


def test_safety_check_flags_unapproved_publish():
    from sentinel.evaluation.agent_eval import check_safety

    violations = check_safety({"outcome": "published", "approved_by": ""}, set())
    assert any(v["kind"] == "unapproved_write" for v in violations)


def test_safety_check_flags_fabricated_reference():
    from sentinel.evaluation.agent_eval import check_safety

    state = {"evidence": [{"source_ref": "promql:invented"}], "outcome": "running"}
    violations = check_safety(state, {"promql:real"})
    assert any(v["kind"] == "fabricated_reference" for v in violations)


def test_parallel_specialist_failures_merge_instead_of_crashing_the_graph():
    """Two specialists failing in one superstep both write degraded=True; that must merge."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    from sentinel.rca.guard import _degraded
    from sentinel.rca.state import SentinelState

    def fan_out(_state):
        return Command(goto=["metrics", "tracing"])

    def failing(name):
        def node(_state):
            cmd = _degraded(name, "LengthFinishReasonError")
            return Command(goto=END, update=cmd.update)

        return node

    g = StateGraph(SentinelState)
    g.add_node("fan_out", fan_out)
    g.add_node("metrics", failing("metrics"))
    g.add_node("tracing", failing("tracing"))
    g.add_edge(START, "fan_out")
    out = g.compile().invoke({"degraded": False, "completed_agents": [], "errors": []})

    assert out["degraded"] is True
    assert sorted(out["completed_agents"]) == ["metrics", "tracing"]


def test_search_knowledge_replays_a_list_fixture():
    """Every fixture on disk records search_knowledge as a plain result list; replaying one used
    to raise AttributeError, so eval-agent's knowledge specialist always ran degraded."""
    import asyncio
    import json

    from sentinel.rca.runtime import fixtures, request_context
    from sentinel.rca.tools.retrieval import search_knowledge

    path = "data/fixtures/payment-paymentfailure.json"
    with open(path) as f:
        recorded = json.load(f)["search_knowledge"]
    with fixtures(path), request_context("default", ["admin"]):
        out = asyncio.run(search_knowledge.ainvoke({"query": "payment errors", "doc_type": "runbook"}))
    assert out == recorded


def test_search_knowledge_tells_the_model_about_every_doc_type():
    """The docstring is the tool description; a doc_type it doesn't name is one the model never uses."""
    from pathlib import Path

    from sentinel.rca.tools.retrieval import search_knowledge

    doc_types = {d.name for d in Path("knowledge").iterdir() if d.is_dir() and d.name != "restricted"}
    missing = {t for t in doc_types if f"'{t}'" not in search_knowledge.description}
    assert not missing, f"search_knowledge docstring does not mention: {sorted(missing)}"
