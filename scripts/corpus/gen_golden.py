"""Write the hand-authored golden evaluation set for the Astroshop corpus.

    uv run python scripts/corpus/gen_golden.py            # writes data/golden.jsonl
    uv run python scripts/corpus/gen_golden.py --check    # print coverage, write nothing

These are written by hand, not generated from chunks. A question generated from the answer is
systematically easier than one a real engineer types — it reuses the document's vocabulary and
never asks something slightly off. So the file below is the seed set the CLAUDE.md workflow calls
for; `sentinel.evaluation.golden.generate_from_chunks` is available to bulk-propose more once an
LLM and an index exist, but its output is candidates to edit, not golden data.

What each field is for (see sentinel/core/types.GoldenExample):
  - `answer_substrings`: strings that must appear in the retrieved context. Stable across
    re-ingestion (unlike content-hash chunk ids), which is why this set leans on them.
  - `expected_behaviour`: "answer" | "refuse" | "escalate". The refuse/escalate cases are what
    make the set able to measure hallucination and ACL leakage at all.
  - `role`: the caller's role. For an acl_denied case this is a role that must NOT see the doc,
    and the correct result is an empty retrieval with no acknowledgement the document exists.

Ground truth for the RCA-trajectory questions (category mixed / multi_hop with an incident) is
cross-checked against scripts/corpus/_incident_truth.jsonl, which is correct by construction
because the incident generator chose the fault.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from _model import REPO_ROOT, load

sys.path.insert(0, str(Path(__file__).parent))

OUT = REPO_ROOT / "data" / "golden.jsonl"


def _examples() -> list[dict]:
    """The set. Grouped by category; ids are gNNN in authoring order."""
    E: list[dict] = []

    def add(**kw) -> None:
        kw.setdefault("role", "oncall")
        kw.setdefault("expected_behaviour", "answer")
        kw.setdefault("answer_substrings", [])
        kw.setdefault("must_not_contain", [])
        E.append(kw)

    # ---------------------------------------------------------------- terminology
    add(id="g001", category="terminology", role="viewer",
        question="what does RED stand for in metrics",
        answer_substrings=["Rate", "Errors", "Duration"])
    add(id="g002", category="terminology", role="viewer",
        question="what is an error budget",
        answer_substrings=["unreliability", "SLO"])
    add(id="g003", category="terminology", role="oncall",
        question="whats consumer lag",
        answer_substrings=["not yet processed", "behind"])
    add(id="g004", category="terminology", role="viewer",
        question="difference between a readiness probe and a liveness probe",
        answer_substrings=["rotation", "restart"])
    add(id="g005", category="terminology", role="oncall",
        question="what is a span",
        answer_substrings=["timed operation", "trace"])

    # ---------------------------------------------------------------------- howto
    add(id="g010", category="howto", role="oncall",
        question="cart is throwing errors what do I check first",
        answer_substrings=["valkey-cart", "flag"])
    add(id="g011", category="howto", role="oncall",
        question="how do I recover from a payment outage",
        answer_substrings=["flag", "flagd"])
    add(id="g012", category="howto", role="oncall",
        question="checkout latency is high, where do I look",
        answer_substrings=["dependencies", "trace"])
    add(id="g013", category="howto", role="oncall",
        question="recommendation service memory keeps climbing",
        answer_substrings=["cache", "product-catalog"])
    add(id="g014", category="howto", role="oncall",
        question="the cart pod seems wedged, latency flat, no logs",
        answer_substrings=["no container healthcheck", "delete the pod"])
    add(id="g015", category="howto", role="oncall",
        question="how do I roll back the recommendation service safely",
        answer_substrings=["stateless", "previous image tag"])
    add(id="g016", category="howto", role="oncall",
        question="email service is restarting on a loop",
        answer_substrings=["memory", "best-effort"])
    add(id="g017", category="howto", role="oncall",
        question="which promql query gives me the error ratio for checkout",
        answer_substrings=["rpc_server_duration_milliseconds_count", "rate("])

    # ------------------------------------------------------------ design_rationale
    add(id="g020", category="design_rationale", role="oncall",
        question="why does the RCA agent not read logs",
        answer_substrings=["anchor", "metrics and traces"])
    add(id="g021", category="design_rationale", role="oncall",
        question="why is otel-collector left out of the health rollup",
        answer_substrings=["every service depends", "collapse"])
    add(id="g022", category="design_rationale", role="oncall",
        question="why do we inject faults with flags instead of a chaos tool",
        answer_substrings=["incidents.jsonl", "ground truth"])
    add(id="g023", category="design_rationale", role="sre",
        question="why are access control filters applied inside the query",
        answer_substrings=["never loads", "process"])
    add(id="g024", category="design_rationale", role="oncall",
        question="why does a chunk have two text fields",
        answer_substrings=["embed_text", "context_text"])

    # --------------------------------------------------------------------- lookup
    add(id="g030", category="lookup", role="oncall",
        question="checkout p95 latency SLO",
        answer_substrings=["1200"])
    add(id="g031", category="lookup", role="oncall",
        question="what pager handles the payment service",
        answer_substrings=["PD-CHECKOUT"])
    add(id="g032", category="lookup", role="oncall",
        question="what language is the shipping service written in",
        answer_substrings=["Rust"])
    add(id="g033", category="lookup", role="oncall",
        question="what flag makes product catalog fail",
        answer_substrings=["productCatalogFailure"])
    add(id="g034", category="lookup", role="oncall",
        question="which services does checkout call",
        answer_substrings=["cart", "payment", "shipping"])
    add(id="g035", category="lookup", role="sre",
        question="freshness SLO for the accounting consumer",
        answer_substrings=["60"])
    add(id="g036", category="lookup", role="oncall",
        question="what team owns product-catalog",
        answer_substrings=["catalog"])

    # ----------------------------------------------------------------- live_status
    add(id="g040", category="live_status", role="oncall", expected_behaviour="escalate",
        question="is the checkout error rate elevated right now",
        answer_substrings=[])
    add(id="g041", category="live_status", role="oncall", expected_behaviour="escalate",
        question="what is the current kafka consumer lag",
        answer_substrings=[])
    add(id="g042", category="live_status", role="oncall", expected_behaviour="escalate",
        question="is any fault flag set at the moment",
        answer_substrings=[])
    add(id="g043", category="live_status", role="oncall", expected_behaviour="escalate",
        question="which services are unhealthy on the board right now",
        answer_substrings=[])

    # ---------------------------------------------------------------------- mixed
    add(id="g050", category="mixed", role="oncall", expected_behaviour="escalate",
        question="payment is erroring — what does the runbook say and is a flag set right now",
        answer_substrings=["paymentFailure"])
    add(id="g051", category="mixed", role="oncall", expected_behaviour="escalate",
        question="checkout orders failing, walk me through it and check current state",
        answer_substrings=["payment", "dependencies"])
    add(id="g052", category="mixed", role="oncall", expected_behaviour="escalate",
        question="recommendation is slow, what's the known cause and is product-catalog load up now",
        answer_substrings=["cache"])
    add(id="g053", category="mixed", role="sre",
        question="what did the last payment-unreachable incident conclude and what was the action item",
        answer_substrings=["flag", "flagd_flag_evaluations_total"])

    # ------------------------------------------------------------------- multi_hop
    add(id="g060", category="multi_hop", role="oncall",
        question="if product-catalog goes down which customer-facing services break",
        answer_substrings=["checkout", "frontend"])
    add(id="g061", category="multi_hop", role="oncall",
        question="an alert fired on frontend but frontend looks fine, what does the process say to do",
        answer_substrings=["dependency", "trace"])
    add(id="g062", category="multi_hop", role="oncall",
        question="the ad service is down, will checkout be affected",
        answer_substrings=["leaf", "does not cascade"])
    add(id="g063", category="multi_hop", role="sre",
        question="a recommendation deploy is scheduled at peak, why is that risky",
        answer_substrings=["cache", "product-catalog"])

    # ----------------------------------------------------------------- unanswerable
    add(id="g070", category="unanswerable", role="oncall", expected_behaviour="refuse",
        question="what is the SLA for the Stripe payment integration",
        answer_substrings=[])
    add(id="g071", category="unanswerable", role="oncall", expected_behaviour="refuse",
        question="how do I configure the loyalty points service",
        answer_substrings=[])
    add(id="g072", category="unanswerable", role="oncall", expected_behaviour="refuse",
        question="what is the on-call rotation for the mobile app team",
        answer_substrings=[])
    add(id="g073", category="unanswerable", role="sre",
        expected_behaviour="refuse",
        question="what was the revenue impact of the Q2 checkout outage in dollars",
        answer_substrings=[])
    add(id="g074", category="unanswerable", role="oncall", expected_behaviour="refuse",
        question="how do I scale the astronomy-db read replicas in production",
        answer_substrings=[])

    # ------------------------------------------------------------------- acl_denied
    add(id="g080", category="acl_denied", role="oncall", expected_behaviour="refuse",
        question="what are the open findings in the latest security review",
        answer_substrings=[], must_not_contain=["SEC-1", "SEC-2", "flagd config is writable"])
    add(id="g081", category="acl_denied", role="viewer", expected_behaviour="refuse",
        question="show me the payment outage post-mortem",
        answer_substrings=[], must_not_contain=["INC-", "customer impact"])
    add(id="g082", category="acl_denied", role="oncall", expected_behaviour="refuse",
        question="what per-page price are we paying the cloud parser under our contract",
        answer_substrings=[], must_not_contain=["negotiated", "unit pricing"])
    add(id="g083", category="acl_denied", role="viewer", expected_behaviour="refuse",
        question="what is the escalation paging order for a checkout incident",
        answer_substrings=[], must_not_contain=["PD-CHECKOUT"])

    return E


def _cross_check_incident_truth() -> list[str]:
    """Warn if a mixed/multi_hop example names an incident conclusion the truth file contradicts."""
    truth_path = Path(__file__).parent / "_incident_truth.jsonl"
    if not truth_path.exists():
        return ["_incident_truth.jsonl missing — run gen_incidents.py first"]
    flags = {
        json.loads(line)["root_cause_flag"]
        for line in truth_path.read_text().splitlines()
        if line.strip()
    }
    notes = []
    for ex in _examples():
        for sub in ex["answer_substrings"]:
            if sub.endswith(("Failure", "Unreachable")) and sub not in flags:
                notes.append(f"{ex['id']}: expects flag {sub!r} not present in incident truth")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="print coverage and cross-check, write nothing")
    args = ap.parse_args()

    load()  # validate the model still loads
    examples = _examples()

    ids = [e["id"] for e in examples]
    assert len(ids) == len(set(ids)), "duplicate ids"

    # coverage against the target mix in sentinel.evaluation.golden
    from sentinel.core.types import GoldenExample
    from sentinel.evaluation.golden import check_coverage

    cov = check_coverage([GoldenExample(**e) for e in examples])
    print(f"{cov['total']} examples")
    for cat, n in sorted(cov["counts"].items()):
        print(f"  {cat:16s} {n:3d}  ({cov['observed_mix'][cat]:.0%})")
    if not cov["balanced"]:
        print("\nunderrepresented:")
        for cat, d in cov["underrepresented"].items():
            print(f"  {cat}: {d['observed']:.0%} < target {d['target']:.0%}")

    for note in _cross_check_incident_truth():
        print(f"  cross-check: {note}")

    if args.check:
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(json.dumps(asdict(GoldenExample(**e))) for e in examples) + "\n")
    print(f"\nwrote {OUT.relative_to(REPO_ROOT)}")
    print("next: make eval  (needs `make up` + an ingested index)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
