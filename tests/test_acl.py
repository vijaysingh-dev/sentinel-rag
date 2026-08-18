"""Access control regression tests.

These are the tests that must never be allowed to fail silently, so they assert two distinct
properties that people routinely conflate:

1. **A restricted chunk is not returned.** The obvious one.
2. **The refusal does not disclose that the chunk exists.** The subtle one. "You don't have access
   to the EU_TECH_50 incident post-mortem" has already leaked that there was an incident on that
   index — which in a regulated environment can itself be material information.

Both are release gates, not metrics to average.
"""

from __future__ import annotations

import pytest

from sentinel.core.resilience import AuthorizationViolation
from sentinel.retrieval.pipeline import RetrievalPipeline
from sentinel.vectorstore.base import SearchFilter


def test_support_cannot_see_admin_only_incident(store, embedder):
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=None, use_cache=False)
    results = pipeline.retrieve("INCIDENT-4471 spin-off post-mortem", roles=["support"])

    returned = [c.chunk.chunk_id for c in results.chunks]
    assert "d3::0001::c" not in returned


def test_admin_can_see_admin_only_incident(store, embedder):
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=None, use_cache=False)
    results = pipeline.retrieve("INCIDENT-4471 spin-off post-mortem", roles=["admin"])

    assert any(c.chunk.chunk_id == "d3::0001::c" for c in results.chunks)


def test_empty_roles_fail_closed():
    """An empty role list must raise, never render as a filter that matches everything."""
    with pytest.raises(AuthorizationViolation):
        SearchFilter(roles=[], tenant_id="default").require_scope()


def test_pipeline_refuses_empty_roles(store, embedder):
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=None, use_cache=False)
    with pytest.raises(AuthorizationViolation):
        pipeline.retrieve("anything at all", roles=[])


def test_component_scoping_narrows_results(store, embedder):
    """A support engineer scoped to data-feed must not receive ICE runbooks."""
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=None, use_cache=False)
    results = pipeline.retrieve(
        "restart procedure and feed thresholds",
        roles=["support"],
        components=["data-feed"],
    )

    for item in results.chunks:
        assert not item.chunk.components or "data-feed" in item.chunk.components


def test_untagged_chunks_are_platform_wide(store, embedder):
    """A chunk with no component tag is visible to any authorised role."""
    pipeline = RetrievalPipeline(store=store, embedder=embedder, reranker=None, use_cache=False)
    results = pipeline.retrieve("divisor adjustment methodology", roles=["support"], components=["data-feed"])

    assert any(c.chunk.chunk_id == "d2::0001::b" for c in results.chunks)


def test_authorization_assertion_catches_mistagged_chunk(store, embedder):
    """Defence in depth: if the filter ever lets a restricted chunk through, the assertion fires.

    Simulated by bypassing the store's filter, which is what a filter bug or a mis-tagged document
    would effectively do.
    """
    from sentinel.core.types import RetrievedChunk

    leaked = RetrievedChunk(chunk=store.records["d3::0001::c"], score=1.0)
    with pytest.raises(AuthorizationViolation):
        RetrievalPipeline._assert_authorized([leaked], roles=["support"], components=[])


@pytest.mark.parametrize("forbidden", ["INCIDENT-4471", "spin-off", "post-mortem"])
def test_refusal_does_not_disclose_existence(forbidden):
    """A denial must not name what was withheld.

    This checks the instruction text the tool layer returns to the model, which is where that
    property is established — the model cannot mention a document it was never told about.
    """
    from sentinel.core.resilience import ToolResult

    message = ToolResult.denied().to_model()
    assert forbidden.lower() not in message.lower()
    assert "do not describe what was restricted" in message.lower()
