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
    for tool in ("search_knowledge", "run_readonly_sql", "recent_deployments", "corporate_actions", "feed_status"):
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

    assert seen["run_readonly_sql"] == "data"
    assert seen["search_knowledge"] == "knowledge"


def test_sql_validator_rejects_writes():
    from sentinel.rca.tools.warehouse import UnsafeQuery, validate_sql

    for statement in (
        "DELETE FROM index_values",
        "UPDATE index_values SET close = 0",
        "DROP TABLE index_values",
        "SELECT 1; DROP TABLE index_values",
        "INSERT INTO index_values VALUES (1)",
    ):
        with pytest.raises(UnsafeQuery):
            validate_sql(statement)


def test_sql_validator_forces_a_row_limit():
    from sentinel.rca.tools.warehouse import validate_sql

    result = validate_sql("SELECT * FROM index_values WHERE trade_date = '2026-03-04'")
    assert "LIMIT" in result.upper()


def test_sql_validator_accepts_a_bounded_select():
    from sentinel.rca.tools.warehouse import validate_sql

    query = "SELECT index_id, close FROM index_values WHERE trade_date = '2026-03-04' LIMIT 10"
    assert validate_sql(query) == query


def test_sql_validator_uses_a_parser_not_a_regex():
    """A comment or a string literal must not defeat the check.

    This is why `sqlglot` parses the statement rather than a regex scanning for keywords.
    """
    from sentinel.rca.tools.warehouse import UnsafeQuery, validate_sql

    with pytest.raises(UnsafeQuery):
        validate_sql("SELECT 1 /* harmless */; DELETE FROM index_values")


def test_unreferenced_claims_are_dropped():
    """A claim citing a ref the tools never returned must not become evidence."""
    from langchain_core.messages import ToolMessage

    from sentinel.rca.agents.common import observed_refs

    messages = [ToolMessage(content='{"ref": "sql:abc123", "rows": 4}', tool_call_id="1")]
    refs = observed_refs(messages)

    assert "sql:abc123" in refs
    assert "sql:fabricated" not in refs


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

    state = {"evidence": [{"source_ref": "sql:invented"}], "outcome": "running"}
    violations = check_safety(state, {"sql:real"})
    assert any(v["kind"] == "fabricated_reference" for v in violations)
