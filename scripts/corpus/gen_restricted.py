"""Generate the admin-only restricted documents.

    uv run python scripts/corpus/gen_restricted.py

These exist so tests/test_acl.py has real role-scoped content to prove two properties against:
a lower-privileged role does not get these chunks back, and the refusal does not reveal they
exist. Everything under knowledge/restricted/ is admin-only by the last rule in
config/acl_rules.yaml, regardless of doc_type.

Content is deliberately the kind of thing that is genuinely sensitive in a real platform — a
security review with unremediated findings, and a vendor/cost note — rather than a fault-injection
post-mortem that only looks sensitive. None of it names a real person or a real third party.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from _model import Platform, load, rel, write

sys.path.insert(0, str(Path(__file__).parent))


def _w(text: str) -> str:
    return textwrap.fill(" ".join(text.split()), 98)


def _security_review(p: Platform) -> str:
    w = _w
    L = [
        "# Astroshop security review — 2026 H1",
        "",
        "**Classification:** Internal — restricted. Admin access only.",
        "",
    ]
    L += ["## Scope", "", w(
        "Threat model review of the checkout path and the flag control plane. Covers the "
        "synchronous order pipeline (cart, currency, product-catalog, shipping, payment) and "
        "flagd's configuration source."
    ), ""]
    L += ["## Findings", ""]
    findings = [
        ("SEC-1", "High", "flagd config is writable by any process that mounts the shared volume",
         "The incident-generator and flagd-ui both write demo.flagd.json directly. Any workload "
         "with that mount can flip a fault flag, including one that should not. There is no "
         "authentication on the write path and no audit of who changed a flag.",
         "Move flag writes behind flagd's own API with a token; emit an audit event per change. "
         "Not yet scheduled."),
        ("SEC-2", "Medium", "Generated SQL in the RCA warehouse tool relies on parse-time checks plus a read-only role",
         "The read-only database role is the real control; the sqlglot parse and single-statement "
         "check are defence in depth. If the role were ever misconfigured with write access, the "
         "parser checks alone would not stop a crafted statement that parses as a SELECT but has "
         "side effects via a function call.",
         "Add a periodic assertion that the role cannot write, alerting if it can. Open."),
        ("SEC-3", "Medium",
         "Restricted-document figures are enriched at ingest by an external vision model",
         "The figure-description path sends images to a vision model at ingest time. A restricted "
         "document's diagram is described and that description becomes a chunk. The chunk inherits "
         "the document's ACL, so retrieval is safe, but the image itself transited an external "
         "service during ingest.",
         "Confirm the vision endpoint is in-tenant; document the data flow in docs/adapters.md. Done."),
        ("SEC-4", "Low", "No rate limit on the RCA investigation API",
         "POST /v1/investigations has no per-caller quota. A misbehaving client could start "
         "unbounded investigations, each of which runs LLM calls and warehouse queries.",
         "Add a token-bucket limit per tenant. Backlog."),
    ]
    for fid, sev, title, detail, remediation in findings:
        L += [f"### {fid} ({sev}) — {title}", "", w(detail), "", f"**Remediation.** {w(remediation)}", ""]
    L += ["## Sign-off", "", w(
        "Two findings (SEC-1, SEC-2) remain open at the time of writing and are the reason this "
        "document is restricted: they describe an exploitable gap that is not yet closed. Review "
        "again once SEC-1 is scheduled."
    ), ""]
    return "\n".join(L)


def _cost_note() -> str:
    w = _w
    L = ["# Parsing and embedding cost review — restricted", "",
         "**Classification:** Internal — restricted. Admin access only.", "",
         "## Why this is restricted", "", w(
             "It contains per-provider unit pricing under our negotiated contract terms, which we "
             "are not permitted to share outside the account team."
         ), "", "## Summary", "", w(
             "On the current corpus mix, hybrid_escalate routes about a fifth of documents to a "
             "cloud parser. The scanned escalation card and the two-column topology PDF account "
             "for most of the escalations; the runbooks and ADRs never escalate. Moving the "
             "topology document to a single-column template would drop the escalation rate "
             "materially at no quality cost."
         ), "", "## Recommendation", "", w(
             "Keep hybrid_escalate. Do not switch to hybrid_merge for the whole corpus — paying "
             "the cloud cost on every runbook to improve two documents is the wrong trade. Fix "
             "the two documents instead."
         ), ""]
    return "\n".join(L)


def main() -> int:
    p = load()
    written = [
        rel(write("restricted", "security-review-2026h1", _security_review(p))),
        rel(write("restricted", "parsing-cost-review", _cost_note())),
    ]
    print(f"wrote {len(written)} restricted documents (admin-only):")
    for x in written:
        print(f"  {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
