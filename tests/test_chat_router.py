"""Chat router rule pass: which of the four routes a message takes before any LLM sees it.

Every case below is a message that was misrouted by an earlier version of the rules, found by
driving the chat API with real questions: a platform-inventory question refused as off-topic, a
design "why" question sent into a 150-second investigation that then wrote an RCA for an anomaly
nobody reported, a conceptual question answered by the live health tool. Only the rule pass is
tested — it is deterministic and free; the LLM fallback is not reachable without a model.
"""

from __future__ import annotations

import pytest

from sentinel.chat.router import Route, _rule_pass


@pytest.mark.parametrize(
    "message, route",
    [
        # platform reference questions -> documentation
        ("What services exist?", Route.RAG),
        ("How many services does the platform run?", Route.RAG),
        ("Is Kafka running?", Route.RAG),
        ("How is tracing set up?", Route.RAG),
        ("What does the incident-generator do?", Route.RAG),
        ("How do I query the error ratio for checkout?", Route.RAG),
        # design "why" questions carry no symptom -> documentation, not an investigation
        ("Why is otel-collector left out of the health tree?", Route.RAG),
        ("Why are astronomy-db and valkey-cart disabled?", Route.RAG),
        # conceptual questions that contain live-status words
        ("Can a service be failing while system-monitor says healthy?", Route.RAG),
        ("How does the system monitor compute health?", Route.RAG),
        ("What happens if postgres goes down?", Route.RAG),
        # live problems -> investigation
        ("why is checkout failing", Route.RCA),
        ("why is checkout slow", Route.RCA),
        ("payment errors are spiking", Route.RCA),
        ("checkout traces show errors", Route.RCA),
        ("is cart down?", Route.RCA),
        # live state -> deterministic tool call
        ("which services are unhealthy right now?", Route.TOOL_CALL),
        ("Is an incident running?", Route.TOOL_CALL),
        ("is the platform healthy", Route.TOOL_CALL),
    ],
)
def test_rule_pass_routes(message, route):
    decision = _rule_pass(message)
    assert decision is not None, "expected a rule to match"
    assert decision.route is route


def test_rca_route_carries_the_service_and_signal():
    decision = _rule_pass("payment errors are spiking")
    assert decision.inferred_service == "payment"
    assert decision.inferred_signal == "error_ratio"


def test_deny_list_is_only_a_candidate_refusal():
    """A keyword refusal is confirmed by the LLM fallback in classify(), never returned alone."""
    decision = _rule_pass("write a python script to sort a list")
    assert decision.route is Route.REFUSE
    assert decision.reason == "deny_list"
