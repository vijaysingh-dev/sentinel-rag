"""Emit hand-labelled parser ground truth for the benchmark-critical documents.

    uv run python scripts/corpus/gen_ground_truth.py

Writes data/ground_truth/<stem>.json for the documents where a parser's quality actually matters
to a downstream decision — the multi-column PDF, the split-table PDF, the running-header PDF, the
two PDF post-mortems, the scanned escalation card, and one DOCX runbook. Format is the one
sentinel/evaluation/parsing_eval.py expects: {"text", "tables", "blocks", "headings"}.

Why generate the truth rather than hand-write it: the generators already know exactly what text,
tables and headings they emitted — that IS the ground truth. Re-deriving it here from the same
model keeps truth and document in lockstep, so editing services.yaml and re-running `make corpus`
updates both. The one document whose truth is genuinely hand-transcribed is the scanned card,
because its "source" is an image with no machine-readable structure — that transcription lives in
SCANNED_CARD_TEXT below and is the human-labelled reference the OCR parsers are scored against.

This does not replace judgement: a real ground-truth set is transcribed by a person reading the
rendered document. This is the reproducible-fixture version of that, honest about which parts are
derived and which are transcribed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _model import KNOWLEDGE, REPO_ROOT, Platform, load, rel
from _promql import error_ratio

sys.path.insert(0, str(Path(__file__).parent))

TRUTH_DIR = REPO_ROOT / "data" / "ground_truth"

# Hand-transcribed from the rendered image in knowledge/oncall/escalation-card-scanned.pdf.
# This is the human reference the OCR parsers are measured against; keep it in sync by eye if the
# card layout in gen_reference.py changes.
SCANNED_CARD_TEXT = """\
ASTROSHOP — ESCALATION CARD
Pin by the on-call desk. Full policy: escalation-guide.docx
WHO OWNS WHAT
PD-CATALOG catalog: product-catalog, recommendation, ad, quote
PD-CHECKOUT checkout: checkout, cart, payment, shipping, currency, email
PD-DATA data: accounting, fraud-detection, kafka
PD-PLATFORM platform-eng: otel-collector, flagd, valkey-cart, astronomy-db
PD-STOREFRONT storefront: frontend, frontend-proxy, image-provider
CHECKOUT IS FAILING
Page Checkout (PD-CHECKOUT) + owners of cart, product-catalog,
shipping, payment NOW. Do not wait to localise.
First: is a fault flag set? Query flagd before paging a 2nd team.
COMMS
P1: incident channel <5min, status page <15min, updates /30min
P2: incident channel, updates /60min
Async faults (kafka lag): no status page, no customer impact
"""


def _write(stem: str, truth: dict) -> str:
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    path = TRUTH_DIR / f"{stem}.json"
    path.write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n")
    return rel(path)


def _dependency_model_truth(p: Platform) -> dict:
    """knowledge/architecture/dependency-model.pdf — the two-column reading-order test."""
    headings = ["Tiers", "Reading the graph", "Fault ownership", "Service dependency table"]
    table = [["Service", "Tier", "Team", "Calls", "Blast radius"]]
    for name, svc in sorted(p.services.items()):
        table.append(
            [
                name,
                str(svc.tier),
                svc.team,
                ", ".join(svc.depends_on) or "—",
                ", ".join(p.blast_radius(name)) or "—",
            ]
        )
    # block order as authored, top to bottom, left column then right column per page
    blocks = [
        f"{p.name} dependency and tier model",
        p.summary,
        "Tiers",
        "Tier 0 is the customer-facing critical path",
        "Reading the graph",
        "The table on the following page lists every service",
        "Fault ownership",
        "Service dependency table",
    ]
    return {"tables": [table], "blocks": blocks, "headings": headings}


def _slo_report_truth(p: Platform) -> dict:
    """knowledge/reference/quarterly-slo-report.pdf — the split-table test."""
    burn = {0: "62%", 1: "35%", 2: "18%"}
    att = {0: "99.87%", 1: "99.94%", 2: "99.7%"}
    table = [["Service", "Tier", "SLO target", "Q3 attainment", "Budget burned", "Worst incident"]]
    for name, svc in sorted(p.services.items()):
        table.append(
            [
                name,
                str(svc.tier),
                str(svc.slo.get("availability", "—")),
                att[svc.tier],
                burn[svc.tier],
                next((f.flag for f in svc.faults), "none"),
            ]
        )
    return {"tables": [table], "headings": [], "blocks": []}


def _capacity_review_truth() -> dict:
    """knowledge/reference/capacity-review.pdf — the running-header test.

    The running header/footer text must NOT appear in the expected text: a good parser drops it.
    """
    headings = [
        "Scope", "Method", "Frontend", "Checkout", "Product-catalog",
        "Recommendation", "Kafka and consumers", "Recommendations",
    ]
    return {
        "headings": headings,
        "tables": [],
        "blocks": [
            "Astroshop capacity review — Q3 2026",
            "Scope",
            "Method",
            "Frontend",
            "Checkout",
            "Product-catalog",
            "Recommendation",
            "Kafka and consumers",
            "Recommendations",
        ],
        "must_not_contain": [
            "CONFIDENTIAL — DO NOT DISTRIBUTE",
            "page 1 of 4",
            "generated 2026-07-15",
        ],
    }


def _incident_pdf_truth(p: Platform, flag: str) -> dict:
    """The two post-mortem PDFs. Truth = the timeline table + section headings."""
    fault = p.faults[flag]
    svc = fault.service
    headings = [
        "Summary", "Customer impact", "Timeline", "Root cause",
        "Contributing factors", "What went well", "Action items", "Detection signature",
    ]
    return {
        "headings": headings,
        # the timeline is rendered as a real 2-col table (Time | Event)
        "tables": [[["Time", "Event"]]],
        "blocks": ["Summary", "Customer impact", "Timeline", "Root cause"],
        "must_contain": [flag, svc, fault.signature.split(";")[0].strip()],
    }


def _runbook_docx_truth(p: Platform, service: str) -> dict:
    svc = p.services[service]
    headings = [
        "When this fires", "Thresholds", "Dashboards and queries", "Diagnose",
        "Mitigate", "Escalate", "After the incident",
    ]
    thresholds = [["Signal", "Objective", "Page at"]]
    if "availability" in svc.slo:
        thresholds.append(["Availability (28d)", svc.slo["availability"], "error budget 50% burned in 1h"])
    if "latency_p95_ms" in svc.slo:
        p95 = svc.slo["latency_p95_ms"]
        thresholds.append(["Duration p95", f"< {p95} ms", f"> {int(p95 * 1.5)} ms for 5m"])
    if "latency_p99_ms" in svc.slo:
        p99 = svc.slo["latency_p99_ms"]
        thresholds.append(["Duration p99", f"< {p99} ms", f"> {int(p99 * 1.5)} ms for 5m"])
    return {
        "headings": headings,
        "tables": [thresholds],
        "blocks": headings,
        "must_contain": [service, svc.team, (error_ratio(service) or svc.key_metrics[0]).split("{")[0].strip()],
    }


def _scanned_card_truth() -> dict:
    return {
        "text": SCANNED_CARD_TEXT.strip(),
        "tables": [],
        "headings": ["WHO OWNS WHAT", "CHECKOUT IS FAILING", "COMMS"],
        "blocks": ["ASTROSHOP — ESCALATION CARD", "WHO OWNS WHAT", "CHECKOUT IS FAILING", "COMMS"],
    }


def main() -> int:
    p = load()
    written: list[str] = []

    written.append(_write("dependency-model", _dependency_model_truth(p)))
    written.append(_write("quarterly-slo-report", _slo_report_truth(p)))
    written.append(_write("capacity-review", _capacity_review_truth()))
    written.append(_write("deployment-topology", _dependency_model_truth(p) | {"tables": []}))
    written.append(_write("escalation-card-scanned", _scanned_card_truth()))

    # the two incident PDFs, by the flags gen_incidents.py renders as PDF
    for flag in ("paymentUnreachable", "kafkaQueueProblems"):
        # stem must match gen_incidents.py's naming: inc-<n>-<svc>-<flag>. We don't know <n> here,
        # so glob for it.
        svc = p.faults[flag].service
        matches = list((KNOWLEDGE / "incident").glob(f"inc-*-{svc}-{flag.lower()}.pdf"))
        if matches:
            written.append(_write(matches[0].stem, _incident_pdf_truth(p, flag)))

    for service in ("checkout", "payment"):
        written.append(_write(service, _runbook_docx_truth(p, service)))

    _ensure_provenance_note()

    print(f"wrote {len(written)} ground-truth files to {rel(TRUTH_DIR)}:")
    for w in written:
        print(f"  {w}")
    return 0


PROVENANCE_MARKER = "<!-- corpus-provenance -->"
PROVENANCE_NOTE = f"""
{PROVENANCE_MARKER}
## Provenance of the generated files

Every `<stem>.json` in this directory is produced by `scripts/corpus/gen_ground_truth.py` from
the same model (`scripts/corpus/services.yaml`) that generates the documents in `knowledge/`, so
document and truth stay in lockstep — edit the model, run `make corpus`, both update.

The one genuinely hand-labelled file is `escalation-card-scanned.json`: that document is an image
with no machine-readable source, so its `text` is transcribed by hand (`SCANNED_CARD_TEXT` in the
generator) and is the human reference the OCR parsers are scored against.

Two non-standard keys appear in some files — `must_contain` and `must_not_contain` — used by
`tests/` to assert boilerplate (running headers) is dropped and key facts survive. `parsing_eval.py`
ignores them; it reads only `text`, `tables`, `blocks`, `headings`.
"""


def _ensure_provenance_note() -> None:
    """Append the provenance section to the existing README once, without clobbering it."""
    readme = TRUTH_DIR / "README.md"
    existing = readme.read_text() if readme.exists() else "# Ground truth for parser evaluation\n"
    if PROVENANCE_MARKER in existing:
        return
    readme.write_text(existing.rstrip() + "\n\n" + PROVENANCE_NOTE.lstrip())


if __name__ == "__main__":
    sys.exit(main())
