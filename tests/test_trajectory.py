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
        "container_memory_usage_bytes",  # cAdvisor, not exposed here
        'label_replace(up, "x", "$1", "job", "(.*)")',  # forbidden function
    ],
)
def test_promql_validator_rejects_unknown_metrics_and_forbidden_funcs(expr):
    """A metric outside the platform allowlist, or a forbidden function, is rejected unrun."""
    from sentinel.rca.tools.metrics import UnsafeQuery, validate_promql

    with pytest.raises(UnsafeQuery):
        validate_promql(expr, range_hours=0.5)


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
