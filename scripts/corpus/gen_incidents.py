"""Generate incident post-mortems from a synthetic-but-deterministic incident history.

    uv run python scripts/corpus/gen_incidents.py [--seed 7] [--count 14]

Each post-mortem is built from one fault in scripts/corpus/services.yaml: the fault's `signature`
becomes the detection story, the blast radius becomes the customer-impact section, and the owning
team becomes the responders. The timeline is generated so that the *first* alert fires on a
downstream critical-path service and the cause is only localised a few minutes in — the pattern
the RCA agent is built to short-circuit.

Two outputs beyond the Markdown documents:

* ``scripts/corpus/_incident_index.json`` — {service: [{id,title,date,cause}]}, consumed by
  gen_runbooks.py for each runbook's "Past incidents" section.
* ``scripts/corpus/_incident_truth.jsonl`` — one row per incident with the *true* root-cause flag,
  the affected services and the expected finding. This is the seed for data/golden.jsonl RCA
  trajectory examples and for scoring the agent: unlike a hand-written golden answer, this one is
  correct by construction because the generator chose the fault.

Why synthetic rather than replaying app/incident-generator/data/incidents.jsonl directly: that log
has timestamps and flags but no metric deltas, no trace ids, no responder actions and no customer
-impact assessment — the parts a post-mortem is actually made of. The generator fills those in
consistently with the fault model. `--from-log` is available to anchor dates/scenarios to a real
run once one exists.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from _model import KNOWLEDGE, Fault, Platform, load, rel, write

HERE = Path(__file__).parent
INDEX_OUT = HERE / "_incident_index.json"
TRUTH_OUT = HERE / "_incident_truth.jsonl"

# A couple of incidents are emitted as PDF with a ruled timeline table, the rest as Markdown.
# These two have the richest timelines, so they exercise a real table in the parser bake-off.
PDF_INCIDENTS = {"paymentUnreachable", "kafkaQueueProblems"}

SEV_BY_TIER = {0: "P1", 1: "P2", 2: "P3"}


def _sev(p: Platform, fault: Fault) -> str:
    hits = p.critical_path_hits(fault.service)
    if fault.service in p.services and p.services[fault.service].critical:
        return "P1" if "Unreachable" in fault.flag or "Failure" in fault.flag else "P2"
    return "P2" if hits else "P3"


def _timeline(start: datetime, p: Platform, fault: Fault) -> list[tuple[str, str]]:
    svc = fault.service
    hits = p.critical_path_hits(svc)
    surface = hits[0] if hits else svc
    team = p.teams[p.services[svc].team]
    out: list[tuple[str, str]] = []

    def at(mins: int, text: str) -> None:
        out.append(((start + timedelta(minutes=mins)).strftime("%H:%M"), text))

    at(0, f"Fault injected: flag `{fault.flag}` set to its fault variant. No alert yet.")
    at(2, f"First alert: `{surface}` "
        + ("error ratio" if "error" in fault.signature or "Failure" in fault.flag or "Unreachable" in fault.flag
           else "duration p95")
        + f" crosses its page threshold. {team.name} and the {p.teams[p.services[surface].team].name} "
          f"team are paged.")
    at(4, f"Responder confirms the alert on `{surface}` and begins checking its own dependencies "
          f"rather than `{surface}` itself, since `{surface}` code has not changed.")
    at(7, f"Trace inspection: a `{surface}` request trace shows the "
        + ("error status" if "error" in fault.signature or "Failure" in fault.flag else "time")
        + f" originating in the `{svc}` span, not in `{surface}`.")
    at(9, f"`{svc}` RED panels match the known signature — {fault.signature}")
    at(11, f"flagd checked: `{fault.flag}` is active, set at {start.strftime('%H:%M')}. Cause confirmed.")
    at(12, f"Flag reset to resting variant. `{svc}` recovers on the next flag evaluation.")
    at(16, f"`{surface}` metrics back at baseline for 3 minutes. Incident closed.")
    return out


def _post_mortem(p: Platform, inc_id: str, date: datetime, fault: Fault, sev: str) -> str:
    svc = fault.service
    s = p.services[svc]
    team = p.teams[s.team]
    hits = p.critical_path_hits(svc)
    radius = p.blast_radius(svc)
    surface = hits[0] if hits else svc
    tl = _timeline(date, p, fault)
    duration = 16

    L: list[str] = []
    L.append(f"# {inc_id}: {svc} {_headline(fault)}")
    L.append("")
    L.append(f"**Date:** {date.strftime('%Y-%m-%d')}  ")
    L.append(f"**Severity:** {sev}  ")
    L.append(f"**Duration:** {duration} minutes  ")
    L.append(f"**Services affected:** {svc}"
             + (f", and {', '.join(radius)} downstream" if radius else ""))
    L.append(f"**Responders:** {team.name} ({team.pager})")
    L.append("")

    L.append("## Summary")
    L.append("")
    L.append(
        f"The `{fault.flag}` fault was active for {duration} minutes. {fault.effect} "
        + (
            f"This surfaced first on `{surface}`, which is {_dep_phrase(p, svc, surface)}, "
            f"and the first several minutes of the response were spent on "
            f"`{surface}` before the cause was localised to `{svc}`."
            if surface != svc
            else f"`{svc}` alerted directly."
        )
    )
    L.append("")

    L.append("## Customer impact")
    L.append("")
    L.append(_impact(p, fault))
    L.append("")

    L.append("## Timeline")
    L.append("")
    for ts, text in tl:
        L.append(f"- {ts} — {text}")
    L.append("")

    L.append("## Root cause")
    L.append("")
    L.append(
        f"The `{fault.flag}` feature flag was set to its fault variant. {fault.effect} "
        f"`{svc}` itself was healthy — correct version, no resource pressure, dependencies up. "
        f"The failure was entirely injected at the flag layer."
    )
    L.append("")

    L.append("## Contributing factors")
    L.append("")
    if surface != svc:
        L.append(
            f"- The alert fires on `{surface}` but the cause is {_hops(p, svc, surface)} away. "
            f"Without a dependency-aware first response, the natural move is to investigate the "
            f"service that paged, which is the wrong one here."
        )
    L.append(
        f"- No alert correlates flag changes with incident start. flagd emits "
        f"`flagd_flag_evaluations_total` but nothing pages on a flip, so the {tl[5][0]} flagd check "
        f"was manual and late."
    )
    if "cpu" in fault.flag.lower() or "Gc" in fault.flag or "memory" in fault.effect.lower():
        L.append(
            "- This is a resource/latency fault, not a hard error. It does not trip an "
            "error-rate alert, so detection depends on a latency SLO being set and paged on."
        )
    L.append("")

    L.append("## What went well")
    L.append("")
    L.append(f"- Once a trace was pulled ({tl[3][0]}), the cause was clear within two minutes.")
    L.append(f"- The `{svc}` runbook's documented signature matched exactly, so no time was lost confirming.")
    L.append("")

    L.append("## Action items")
    L.append("")
    L.append(
        f"1. Add a Grafana annotation and a low-urgency alert on `flagd_flag_evaluations_total` "
        f"deltas, so a flag flip is visible on every dashboard. *(owner: {p.teams['platform-eng'].name})*"
    )
    if surface != svc:
        L.append(
            f"2. Add \"check the dependency graph before the service that paged\" as step 1 of the "
            f"`{surface}` runbook. *(owner: {p.teams[p.services[surface].team].name})* — done, see "
            f"`knowledge/runbook/{surface}.md`."
        )
    L.append(
        f"{'3' if surface != svc else '2'}. Confirm the `{svc}` runbook's known-failure-modes "
        f"section lists `{fault.flag}` with this signature. *(owner: {team.name})*"
    )
    L.append("")

    L.append("## Detection signature")
    L.append("")
    L.append("```")
    L.append(fault.signature)
    L.append("```")
    L.append("")
    return "\n".join(L)


def _headline(fault: Fault) -> str:
    f = fault.flag.lower()
    if "unreachable" in f:
        return "unreachable, checkout orders failing"
    if "cache" in f:
        return "cache disabled, upstream fan-out spike"
    if "failure" in f:
        return "returning errors"
    if "cpu" in f or "gc" in f:
        return "CPU/GC saturation, elevated latency"
    if "memory" in fault.effect.lower() or "leak" in f:
        return "memory growth and restarts"
    if "slow" in f or "slowdown" in f:
        return "elevated latency"
    if "readiness" in f:
        return "taken out of rotation by a failing readiness probe"
    if "cache" in f:
        return "cache disabled, upstream fan-out spike"
    return "degraded"


def _dep_phrase(p: Platform, dep: str, caller: str) -> str:
    return f"a direct caller of `{dep}`" if dep in p.services[caller].depends_on else f"downstream of `{dep}`"


def _hops(p: Platform, dep: str, caller: str) -> str:
    n = 1 if dep in p.services[caller].depends_on else 2
    return "one hop" if n == 1 else "two hops"


def _impact(p: Platform, fault: Fault) -> str:
    f = fault.flag.lower()
    svc = fault.service
    if "payment" in svc and ("unreachable" in f or "failure" in f):
        return (
            "Checkout attempts failed at the payment step. Customers who reached the payment page "
            "saw an error and could not complete an order for the duration. Carts were preserved; "
            "no orders were lost or double-charged (payment is a stub with no real PSP)."
        )
    if svc == "cart":
        return (
            "Add-to-cart and cart retrieval failed for the configured fraction of requests. "
            "Affected customers saw an empty or errored cart and a share of checkouts failed early."
        )
    if svc == "ad":
        return "Minimal. The ad slot on the product page rendered empty; browse and checkout were unaffected."
    if svc == "recommendation":
        return (
            "The \"you may also like\" carousel was slow or empty. No checkout impact. Secondary "
            "effect: `product-catalog` load rose sharply, which risked a wider incident if sustained."
        )
    if svc == "email":
        return (
            "Order-confirmation emails were delayed or dropped during the restarts. Orders "
            "themselves completed normally — the email call is best-effort and does not gate checkout."
        )
    if svc == "shipping":
        return "International orders saw multi-second checkout delays. Domestic checkout was unaffected."
    if svc == "product-catalog":
        return (
            "One product's detail page returned an error; the rest of the catalog was fine. "
            "Listing and search were unaffected. Narrow blast radius despite being a shared dependency."
        )
    if svc == "kafka":
        return (
            "No customer-facing impact. `accounting` and `fraud-detection` fell behind on the "
            "orders topic; ledger entries and fraud scores were delayed, not lost, and caught up "
            "after recovery."
        )
    hits = p.critical_path_hits(svc)
    return (
        f"Degradation on {', '.join(hits)} for the duration."
        if hits
        else f"Contained to `{svc}`; no customer-facing degradation observed."
    )


def _as_pdf(stem: str, markdown_body: str) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=13)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)

    out = KNOWLEDGE / "incident" / f"{stem}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=letter, topMargin=0.8 * inch, bottomMargin=0.8 * inch)
    flow: list = []

    lines = markdown_body.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("## Timeline"):
            flow.append(Paragraph("Timeline", h2))
            i += 2
            rows = [["Time", "Event"]]
            cell = ParagraphStyle("cell", parent=body, fontSize=8.5, leading=11)
            while i < len(lines) and lines[i].startswith("- "):
                ts, _, text = lines[i][2:].partition(" — ")
                rows.append([ts, Paragraph(_md(text), cell)])
                i += 1
            tbl = Table(rows, colWidths=[0.9 * inch, 5.4 * inch])
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b3a55")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8a9bb5")),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f8")]),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            flow.append(tbl)
            flow.append(Spacer(1, 8))
            continue
        if ln.startswith("# "):
            flow.append(Paragraph(_md(ln[2:]), h1))
        elif ln.startswith("## "):
            flow.append(Paragraph(_md(ln[3:]), h2))
        elif ln.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            flow.append(Paragraph(f'<font face="Courier" size="8">{_md("<br/>".join(buf))}</font>', body))
        elif ln.startswith("- "):
            flow.append(Paragraph("&bull; " + _md(ln[2:]), body))
        elif ln.strip():
            flow.append(Paragraph(_md(ln), body))
        else:
            flow.append(Spacer(1, 4))
        i += 1

    doc.build(flow)
    return rel(out)


def _md(text: str) -> str:
    import re

    text = text.replace("&", "&amp;").replace("<br/>", "\x00").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\x00", "<br/>")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--count", type=int, default=14)
    args = ap.parse_args(argv)

    p = load()
    rng = random.Random(args.seed)
    all_faults = list(p.faults.values())
    rng.shuffle(all_faults)
    chosen = all_faults[: args.count]

    base = datetime(2026, 6, 1, 9, 0)
    index: dict[str, list[dict]] = {}
    truth_rows: list[dict] = []
    written: list[str] = []

    for n, fault in enumerate(chosen, start=1):
        date = base + timedelta(
            days=rng.randint(0, 75), hours=rng.randint(-2, 6), minutes=rng.choice([0, 7, 13, 22, 41])
        )
        inc_id = f"INC-{2600 + n * 7}"
        sev = _sev(p, fault)
        body = _post_mortem(p, inc_id, date, fault, sev)
        stem = f"{inc_id.lower()}-{fault.service}-{fault.flag.lower()}"

        if fault.flag in PDF_INCIDENTS:
            written.append(_as_pdf(stem, body))
        else:
            written.append(rel(write("incident", stem, body)))

        cause = f"`{fault.flag}` fault flag active"
        index.setdefault(fault.service, []).append(
            {
                "id": inc_id,
                "title": f"{fault.service} {_headline(fault)}",
                "date": date.strftime("%Y-%m-%d"),
                "cause": cause,
            }
        )
        surface = (p.critical_path_hits(fault.service) or [fault.service])[0]
        truth_rows.append(
            {
                "incident_id": inc_id,
                "doc": stem,
                "date": date.strftime("%Y-%m-%d"),
                "alert_fires_on": surface,
                "root_cause_service": fault.service,
                "root_cause_flag": fault.flag,
                "affected_services": [fault.service] + p.blast_radius(fault.service),
                "expected_finding": (
                    f"The {fault.flag} feature flag was set to its fault variant; "
                    f"{fault.service} is healthy and the failure is injected at the flag layer."
                ),
                "signature": fault.signature,
            }
        )

    INDEX_OUT.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    TRUTH_OUT.write_text("".join(json.dumps(r) + "\n" for r in truth_rows))

    print(f"wrote {len(written)} post-mortems:")
    for w in sorted(written):
        print(f"  {w}")
    print(f"\nground truth:\n  {rel(TRUTH_OUT)} ({len(truth_rows)} incidents)\n  {rel(INDEX_OUT)}")
    print("\nre-run gen_runbooks.py now to pick up the 'Past incidents' cross-references.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
