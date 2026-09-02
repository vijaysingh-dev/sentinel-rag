"""Generate deliberately messy documents that stress the parser's failure modes.

    uv run python scripts/corpus/gen_messy.py

Every failure mode listed in docs/STAGE-OVERVIEW.md section 1 gets one document that triggers it,
so the parser bake-off has something to score other than clean input:

* ``knowledge/reference/capacity-review.pdf`` — a running header AND footer on every page, the
  thing that dilutes every chunk with the same boilerplate. Tests `_encoding_score` / header
  detection.
* ``knowledge/reference/quarterly-slo-report.pdf`` — a wide table that splits across a page
  break, so the second half is a set of orphaned rows with no header. Tests table reconstruction.
* ``knowledge/architecture/deployment-topology.pdf`` — a two-column layout where the columns are
  close together, the case where naive extraction interleaves them line by line and destroys
  reading order. Tests Kendall's-tau reading-order scoring.

These are real content — a capacity review, an SLO report, a topology note — not lorem ipsum, so
a chunk that does survive extraction is still a useful retrieval target. The point is that the
*bad* extraction is measurably bad, which is what justifies routing them to a cloud parser.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _model import KNOWLEDGE, Platform, load, rel

sys.path.insert(0, str(Path(__file__).parent))


def _running_header_pdf(p: Platform) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=8)
    h = ParagraphStyle("h", parent=styles["Heading2"], fontSize=12, spaceBefore=10)

    out = KNOWLEDGE / "reference" / "capacity-review.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    def _deco(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.HexColor("#444444"))
        # the offending running header — same text on every page
        canvas.drawString(inch, letter[1] - 0.5 * inch,
                          "ASTROSHOP CAPACITY REVIEW — Q3 2026 — CONFIDENTIAL — DO NOT DISTRIBUTE")
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(inch, 0.5 * inch,
                          "Astroshop Platform Engineering  |  capacity-review-q3-2026  |  "
                          f"page {doc.page} of 4  |  generated 2026-07-15")
        canvas.line(inch, letter[1] - 0.6 * inch, letter[0] - inch, letter[1] - 0.6 * inch)
        canvas.line(inch, 0.65 * inch, letter[0] - inch, 0.65 * inch)
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out), pagesize=letter, topMargin=1.1 * inch, bottomMargin=1.0 * inch)
    flow = [Paragraph("Astroshop capacity review — Q3 2026", styles["Title"]), Spacer(1, 10)]

    sections = [
        ("Scope", "This review covers the tier-0 request path only: "
         + ", ".join(sorted(s.name for s in p.services.values() if s.critical))
         + ". Tier-1 and tier-2 services are reviewed on a separate cadence."),
        ("Method", "Peak concurrent virtual users over the quarter were replayed against a "
         "staging deployment at 1x, 2x and 4x. Headroom is defined as the multiple of current "
         "peak at which p99 latency first exceeds the service SLO."),
        ("Frontend", "The frontend fans out to every product-path service, so its own capacity is "
         "bounded by its slowest dependency under load, not by its own CPU. At 2x it stayed within "
         "SLO; at 4x, p99 was dominated by product-catalog and recommendation queueing."),
        ("Checkout", "Checkout is synchronous over six dependencies, so its latency is the sum of "
         "theirs plus its own overhead. Headroom is 2.5x. Payment and shipping are the two that "
         "degrade first under load; email does not matter because the call is best-effort."),
        ("Product-catalog", "Shared by frontend and recommendation. At 4x frontend load plus the "
         "recommendation-cache-failure fan-out pattern, product-catalog is the first tier-0 "
         "service to breach SLO. This is the single highest-risk capacity finding."),
        ("Recommendation", "The in-process cache is load-bearing for product-catalog's capacity, "
         "not just recommendation's latency. A recommendation deploy that resets the cache "
         "briefly doubles product-catalog read load — schedule deploys off-peak."),
        ("Kafka and consumers", "accounting and fraud-detection lag is a function of order rate. "
         "At 4x order rate, steady-state lag stayed under the 60s freshness SLO but recovery from "
         "a 5-minute broker stall took 12 minutes. Consider a second partition."),
        ("Recommendations", "1. Add a product-catalog read replica before Q4 peak. "
         "2. Move recommendation deploys to a maintenance window. "
         "3. Add a second orders-topic partition. "
         "4. Re-run this review at 6x to find the next bottleneck."),
    ]
    for title, text in sections:
        flow.append(Paragraph(title, h))
        flow.append(Paragraph(text, body))
        flow.append(Spacer(1, 4))

    doc.build(flow, onFirstPage=_deco, onLaterPages=_deco)
    return rel(out)


def _split_table_pdf(p: Platform) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=9.5, leading=13)

    out = KNOWLEDGE / "reference" / "quarterly-slo-report.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=letter, topMargin=0.9 * inch, bottomMargin=0.9 * inch)

    flow = [Paragraph("Astroshop quarterly SLO report — Q3 2026", styles["Title"]), Spacer(1, 8)]
    flow.append(
        Paragraph(
            "Attainment against SLO for every service, with error-budget burn. The table runs "
            "onto a second page; note that the continued rows carry no repeated header.",
            body,
        )
    )
    flow.append(Spacer(1, 10))

    rows = [["Service", "Tier", "SLO target", "Q3 attainment", "Budget burned", "Worst incident"]]
    # enough rows that it splits, and repeatRows deliberately NOT set
    burn = {0: "62%", 1: "35%", 2: "18%"}
    for name, svc in sorted(p.services.items()):
        target = svc.slo.get("availability", "—")
        att = {0: "99.87%", 1: "99.94%", 2: "99.7%"}[svc.tier]
        worst = next((f.flag for f in svc.faults), "none")
        rows.append([name, str(svc.tier), str(target), att, burn[svc.tier], worst])

    tbl = Table(rows, colWidths=[1.3 * inch, 0.5 * inch, 1.0 * inch, 1.2 * inch, 1.1 * inch, 1.7 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b3a55")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8a9bb5")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f8")]),
            ]
        )
    )
    flow.append(tbl)
    flow.append(Spacer(1, 10))
    flow.append(
        Paragraph(
            "Interpretation: tier-0 burned 62% of its 28-day budget this quarter, driven mostly "
            "by the payment and product-catalog incidents. Tier-2 attainment looks low but has no "
            "customer impact — it is ledger and fraud-scoring freshness, which recovered fully "
            "each time.",
            body,
        )
    )
    doc.build(flow)
    return rel(out)


def _two_column_pdf(p: Platform) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer

    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=9, leading=11.5, spaceAfter=6)
    h = ParagraphStyle("h", parent=styles["Heading3"], fontSize=10.5, spaceBefore=6, spaceAfter=3)

    out = KNOWLEDGE / "architecture" / "deployment-topology.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(out), pagesize=letter, topMargin=0.9 * inch, bottomMargin=0.8 * inch)

    # deliberately narrow gutter — columns nearly touch, so line-by-line extraction interleaves them
    lm = 0.7 * inch
    gutter = 0.18 * inch
    col_w = (doc.width - gutter) / 2
    frames = [
        Frame(lm, doc.bottomMargin, col_w, doc.height, id="c1", rightPadding=4),
        Frame(lm + col_w + gutter, doc.bottomMargin, col_w, doc.height, id="c2", leftPadding=4),
    ]
    doc.addPageTemplates([PageTemplate(id="two", frames=frames)])

    flow = [Paragraph("Astroshop deployment topology", styles["Title"]), Spacer(1, 8)]
    flow.append(
        Paragraph(
            "How the services are laid out across nodes and networks. This is a two-column "
            "document with a tight gutter; if your extraction interleaves the columns, the "
            "reading order score will show it.",
            body,
        )
    )
    for name, svc in sorted(p.services.items()):
        flow.append(Paragraph(f"{name} ({svc.language})", h))
        flow.append(
            Paragraph(
                f"{svc.role} Tier {svc.tier}, owned by {svc.team}. "
                + (
                    f"Calls {', '.join(svc.depends_on)}. "
                    if svc.depends_on
                    else "No outbound service calls. "
                )
                + (
                    "Co-located with its datastore. "
                    if svc.name in {"cart", "product-catalog"}
                    else ""
                )
                + "Scrape target on the metrics network; exports spans to the collector over OTLP.",
                body,
            )
        )
    doc.build(flow)
    return rel(out)


def main() -> int:
    p = load()
    a = _running_header_pdf(p)
    b = _split_table_pdf(p)
    c = _two_column_pdf(p)
    print("wrote (deliberately messy — expect low local parse scores):")
    print(f"  {a}   running header/footer on every page")
    print(f"  {b}   table split across a page break, no repeated header")
    print(f"  {c}   two-column layout, tight gutter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
