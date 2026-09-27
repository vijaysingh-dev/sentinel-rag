"""Generate the reference documents: metrics catalog, on-call guide, and a scanned escalation card.

    uv run python scripts/corpus/gen_reference.py

Three outputs:

* ``knowledge/reference/metrics-catalog.xlsx`` — every key metric in services.yaml, one row per
  (service, metric), with its type, the PromQL to graph it, and its alert threshold. Wide table,
  many rows — the case the local XLSX parser handles perfectly and a layout model has nothing to
  add.

* ``knowledge/oncall/escalation-guide.docx`` — the cross-team escalation policy: who owns what,
  the paging order for a customer-facing incident, and the comms rules. DOCX with a table and
  nested lists.

* ``knowledge/oncall/escalation-card-scanned.pdf`` — the one-page "who to call" card, rendered to
  an image and wrapped in a PDF with no text layer. This forces the parser's scanned-PDF branch
  (`_pdf_has_text_layer` returns false -> route to cloud OCR). On a local-only install it will
  score badly, which is the point: it demonstrates the escalation path the hybrid router exists
  for. Its hand-transcribed truth is in gen_ground_truth.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _model import KNOWLEDGE, Platform, load, rel
from _promql import error_ratio, query_for

sys.path.insert(0, str(Path(__file__).parent))


def _metric_type(metric: str) -> str:
    if "duration" in metric and metric.endswith(("_seconds", "_milliseconds")):
        return "histogram"
    if metric.endswith("_total"):
        return "counter"
    if metric.endswith(("_bytes", "_ratio", "_count", "_lag")):
        return "gauge"
    return "gauge"


def _threshold(p: Platform, svc_name: str, metric: str) -> str:
    svc = p.services[svc_name]
    slo = svc.slo
    if "duration" in metric:
        if "latency_p95_ms" in slo:
            return f"p95 > {int(slo['latency_p95_ms'] * 1.5)} ms for 5m"
        return "p95 > 1.5x baseline for 5m"
    if "fail" in metric or "error" in metric:
        return "rate > 0 sustained; ratio > 1% for 5m"
    if "lag" in metric:
        # kafka_consumer_group_lag_ratio counts messages, while the freshness SLO is in seconds;
        # "still rising" is the unit-free signal, and it is what a stalled consumer looks like.
        base = slo.get("freshness_lag_s", 60)
        return f"rising for 10m (messages behind; freshness SLO is {base}s)"
    if "memory" in metric or "bytes" in metric:
        return "monotonic climb over 30m (leak signature)"
    if "cpu" in metric:
        return "> 85% for 10m"
    return "no direct alert; context for the RED panels"


def _metrics_xlsx(p: Platform) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Metrics"
    ws.append(["Service", "Tier", "Team", "Metric", "Type", "PromQL", "Alert threshold"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2B3A55")

    for name, svc in sorted(p.services.items()):
        # the generic error-ratio row first, for every service that emits spans
        ratio = error_ratio(name)
        if ratio:
            ws.append([name, svc.tier, svc.team, "error ratio (RED)", "derived",
                       ratio.replace("\n", " "), "> 1% for 5m"])
        for m in svc.key_metrics:
            _label, q = query_for(m, name)
            ws.append([name, svc.tier, svc.team, m, _metric_type(m), q, _threshold(p, name, m)])

    for col, wdt in zip("ABCDEFG", [16, 5, 12, 34, 11, 62, 34], strict=True):
        ws.column_dimensions[col].width = wdt
    for row in ws.iter_rows(min_row=2):
        row[5].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"

    out = KNOWLEDGE / "reference" / "metrics-catalog.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return rel(out)


def _escalation_text(p: Platform) -> list[tuple[str, list[str]]]:
    """Returns [(heading, [paragraphs/list-items])] used by both the DOCX and the scanned card."""
    checkout_chain = ["checkout", "cart", "product-catalog", "shipping", "payment"]
    return [
        (
            "Ownership",
            [f"{t.name}: {', '.join(t.owns)} — page {t.pager}, discuss in {t.slack}"
             for t in sorted(p.teams.values(), key=lambda x: x.name)],
        ),
        (
            "Paging order for a customer-facing incident",
            [
                "1. Page the team that owns the service the alert fired on.",
                "2. In parallel, page the team that owns each direct dependency of that service — "
                "the cause is usually one hop down, not in the service that paged.",
                "3. If checkout is failing, page the Checkout team plus the owners of "
                + ", ".join(checkout_chain[1:]) + " immediately; do not wait to localise first.",
                "4. Escalate to platform-eng only if the cause is the collector, flagd, or a "
                "shared datastore (valkey-cart, astronomy-db).",
            ],
        ),
        (
            "First checks before paging a second team",
            [
                "Is a fault flag set? Query flagd. A flag flip is the most common cause here and "
                "needs no second team.",
                "Did a deploy correlate? Check the deploy log for the alerting service and its "
                "dependencies in the last 30 minutes.",
                "Is an upstream already red on the health board? If so, work that incident, not "
                "this symptom.",
            ],
        ),
        (
            "Communications",
            [
                "P1: incident channel within 5 minutes, status page within 15, updates every 30.",
                "P2: incident channel, updates every 60 minutes.",
                "Async-only faults (kafka lag, accounting, fraud-detection): no status page — "
                "there is no customer-facing impact, only delayed back-office data.",
            ],
        ),
    ]


def _escalation_docx(p: Platform) -> str:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading("Astroshop on-call escalation guide", level=1)
    doc.add_paragraph(
        "How incidents are routed between the five Astroshop teams. Read alongside the per-service "
        "runbooks: this document says who to involve, the runbook says what to check."
    )

    for heading, items in _escalation_text(p):
        doc.add_heading(heading, level=2)
        if heading == "Ownership":
            t = doc.add_table(rows=1, cols=3)
            t.style = "Light Grid Accent 1"
            t.rows[0].cells[0].text = "Team"
            t.rows[0].cells[1].text = "Owns"
            t.rows[0].cells[2].text = "Contact"
            for team in sorted(p.teams.values(), key=lambda x: x.name):
                row = t.add_row().cells
                row[0].text = team.name
                row[1].text = ", ".join(team.owns)
                row[2].text = f"{team.pager} / {team.slack}"
        else:
            for it in items:
                style = "List Number" if it[0].isdigit() else "List Bullet"
                doc.add_paragraph(it[3:] if it[0].isdigit() else it, style=style)

    out = KNOWLEDGE / "oncall" / "escalation-guide.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return rel(out)


def _scanned_card(p: Platform) -> str:
    """Render a one-page escalation card to a PNG, then wrap the image in a text-layer-free PDF."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    W, H = 1700, 2200  # ~200 DPI letter
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    def _font(size: int):
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ):
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    pass
        return ImageFont.load_default()

    big, mid, small = _font(58), _font(38), _font(30)
    y = 90
    d.text((90, y), "ASTROSHOP — ESCALATION CARD", font=big, fill="black")
    y += 90
    d.text((90, y), "Pin by the on-call desk. Full policy: escalation-guide.docx", font=small, fill="black")
    y += 80
    d.line((90, y, W - 90, y), fill="black", width=3)
    y += 50

    d.text((90, y), "WHO OWNS WHAT", font=mid, fill="black")
    y += 60
    for team in sorted(p.teams.values(), key=lambda x: x.name):
        d.text((110, y), f"{team.pager:16s} {team.name}: {', '.join(team.owns[:4])}", font=small, fill="black")
        y += 46
        if len(team.owns) > 4:
            d.text((110 + 260, y), ", ".join(team.owns[4:]), font=small, fill="black")
            y += 46
    y += 30

    d.text((90, y), "CHECKOUT IS FAILING", font=mid, fill="black")
    y += 60
    for line in [
        "Page Checkout (PD-CHECKOUT) + owners of cart, product-catalog,",
        "shipping, payment NOW. Do not wait to localise.",
        "First: is a fault flag set? Query flagd before paging a 2nd team.",
    ]:
        d.text((110, y), line, font=small, fill="black")
        y += 46
    y += 30

    d.text((90, y), "COMMS", font=mid, fill="black")
    y += 60
    for line in [
        "P1: incident channel <5min, status page <15min, updates /30min",
        "P2: incident channel, updates /60min",
        "Async faults (kafka lag): no status page, no customer impact",
    ]:
        d.text((110, y), line, font=small, fill="black")
        y += 46

    # a faint rotation + speckle so it reads as a scan, not a screenshot
    img = img.rotate(-0.4, expand=False, fillcolor="white")

    png = KNOWLEDGE / "oncall" / "_escalation-card.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    img.save(png, dpi=(200, 200))

    out = KNOWLEDGE / "oncall" / "escalation-card-scanned.pdf"
    c = canvas.Canvas(str(out), pagesize=letter)
    c.drawImage(ImageReader(str(png)), 0, 0, width=letter[0], height=letter[1])
    c.save()
    png.unlink()
    return rel(out)


def main() -> int:
    p = load()
    xlsx = _metrics_xlsx(p)
    docx = _escalation_docx(p)
    pdf = _scanned_card(p)
    print("wrote:")
    print(f"  {xlsx}")
    print(f"  {docx}")
    print(f"  {pdf}   (no text layer — routes to cloud OCR)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
