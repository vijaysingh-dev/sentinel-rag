"""The generated corpus is labelled the way config/acl_rules.yaml intends.

Unlike test_acl.py, which proves the retrieval pipeline enforces a filter, this proves the
*labelling* is right: every generated document resolves to the role set its doc_type and path
should give it. A mislabelled document is a leak that the pipeline enforcement cannot catch,
because the pipeline faithfully enforces whatever label ingestion attached.

Runs with no infrastructure — it only exercises the ACL resolver against real file paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.ingest.acl import AclResolver

REPO = Path(__file__).resolve().parents[1]
KNOWLEDGE = REPO / "knowledge"
ACL_RULES = REPO / "config" / "acl_rules.yaml"

pytestmark = pytest.mark.skipif(
    not KNOWLEDGE.exists() or not any(KNOWLEDGE.rglob("*.md")),
    reason="corpus not generated — run `make corpus`",
)


@pytest.fixture(scope="module")
def resolver() -> AclResolver:
    return AclResolver(ACL_RULES)


def _resolve(resolver: AclResolver, path: Path):
    return resolver.resolve(str(path), path.parent.name)


def _iter(doc_type: str):
    return sorted((KNOWLEDGE / doc_type).glob("*"))


def test_every_document_resolves_to_a_nonempty_role_set(resolver):
    for path in KNOWLEDGE.rglob("*"):
        if not path.is_file() or path.name.startswith("_"):
            continue
        roles, _ = _resolve(resolver, path)
        assert roles, f"{path} resolved to an empty role set — would fail closed at retrieval"
        assert "admin" in roles, f"{path} does not grant admin — admin must see everything"


def test_restricted_is_admin_only(resolver):
    docs = _iter("restricted")
    assert docs, "expected generated restricted documents"
    for path in docs:
        roles, _ = _resolve(resolver, path)
        assert roles == ["admin"], f"{path} is under /restricted/ but resolved to {roles}"


def test_glossary_and_architecture_are_broadly_readable(resolver):
    for doc_type in ("glossary", "architecture"):
        for path in _iter(doc_type):
            roles, _ = _resolve(resolver, path)
            assert "viewer" in roles, f"{path} should be viewer-readable, got {roles}"


def test_runbooks_are_oncall_not_viewer(resolver):
    docs = _iter("runbook")
    assert docs
    for path in docs:
        roles, _ = _resolve(resolver, path)
        assert "oncall" in roles and "viewer" not in roles, f"{path} -> {roles}"


def test_p1_customer_impact_postmortems_stay_admin_only(resolver):
    """The payment-outage post-mortems name customer impact and must not open to on-call."""
    sensitive = [
        p for p in _iter("incident")
        if "paymentfailure" in p.name or "paymentunreachable" in p.name
    ]
    assert sensitive, "expected the payment post-mortems"
    for path in sensitive:
        roles, _ = _resolve(resolver, path)
        assert roles == ["admin"], f"{path} -> {roles}, expected admin-only"


def test_low_severity_postmortems_open_to_oncall(resolver):
    learnable = [p for p in _iter("incident") if "adfailure" in p.name or "imageslowload" in p.name]
    assert learnable
    for path in learnable:
        roles, _ = _resolve(resolver, path)
        assert "oncall" in roles, f"{path} -> {roles}, expected on-call to be able to learn from it"


def test_capacity_and_slo_internals_are_sre_not_oncall(resolver):
    for name in ("capacity-review.pdf", "quarterly-slo-report.pdf"):
        path = KNOWLEDGE / "reference" / name
        if not path.exists():
            pytest.skip(f"{name} not generated")
        roles, _ = _resolve(resolver, path)
        assert "sre" in roles and "oncall" not in roles, f"{path} -> {roles}"
