"""Generate the platform architecture documents.

    uv run python scripts/corpus/gen_architecture.py

Two artifacts, deliberately in two formats the pipeline handles differently:

* ``knowledge/architecture/astroshop-architecture.pptx`` — an architecture deck. Each slide is a
  diagram or a short bullet list; the *reasoning* lives in the speaker notes. This is the case the
  house rule about decks is about: `python-pptx` reads notes, `local_parser` keeps them, and a
  chunk built from a three-word slide plus its notes is useful where the slide alone is not.

* ``knowledge/architecture/dependency-model.pdf`` — a reference PDF with a two-column body and a
  wide dependency table. Multi-column is the layout that silently breaks naive PDF extraction
  (columns interleaved line by line), so this doubles as a reading-order test for the parser
  bake-off. Its hand-labelled truth is emitted by gen_ground_truth.py.

Both are built from scripts/corpus/services.yaml, so the tier assignments, edges and team
ownership stay consistent with every other document.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _model import KNOWLEDGE, Platform, load, rel

sys.path.insert(0, str(Path(__file__).parent))


# --------------------------------------------------------------------------- deck
def _build_deck(p: Platform) -> str:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    title_only = prs.slide_layouts[5]

    def _notes(slide, text: str) -> None:
        slide.notes_slide.notes_text_frame.text = text.strip()

    def _title_slide(title: str, subtitle: str, notes: str):
        s = prs.slides.add_slide(title_only)
        s.shapes.title.text = title
        box = s.shapes.add_textbox(Inches(0.7), Inches(2.0), Inches(12), Inches(1))
        box.text_frame.text = subtitle
        _notes(s, notes)
        return s

    def _bullets(title: str, bullets: list[str], notes: str):
        s = prs.slides.add_slide(blank)
        tb = s.shapes.add_textbox(Inches(0.7), Inches(0.5), Inches(12), Inches(1))
        tb.text_frame.text = title
        tb.text_frame.paragraphs[0].font.size = Pt(32)
        body = s.shapes.add_textbox(Inches(0.9), Inches(1.8), Inches(11.5), Inches(5))
        tf = body.text_frame
        tf.word_wrap = True
        for i, b in enumerate(bullets):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.text = "• " + b
            para.font.size = Pt(20)
        _notes(s, notes)
        return s

    # ---- slide 1
    _title_slide(
        f"{p.name} platform architecture",
        "How the storefront is wired, how it is observed, and how it fails",
        f"""
        {p.name} is a public astronomy e-commerce storefront used as the reference system for the
        Sentinel RCA pipeline. It is a real running microservice deployment — {len(p.services)}
        services across {len({s.language for s in p.services.values()})} languages — with synthetic
        customer traffic and injectable faults. This deck is the shared mental model: read it
        before an on-call shift or before reading any single service's runbook. The claims here
        are backed by scripts/corpus/services.yaml, which is generated from the deployment's own
        compose file and flag config.
        """,
    )

    # ---- slide 2: the request path
    critical = [s.name for s in p.services.values() if s.critical]
    _bullets(
        "The customer request path",
        [
            "Browser -> frontend-proxy (Envoy) -> frontend (Next.js)",
            "frontend fans out to: " + ", ".join(sorted(p.services["frontend"].depends_on)),
            "Checkout order: cart -> product-catalog + currency -> shipping quote -> payment -> ship -> email -> kafka",
            f"Tier-0 (customer-critical): {', '.join(sorted(critical))}",
        ],
        """
        The single most important fact for triage: checkout calls its dependencies in a fixed
        order (get the cart, price each item via product-catalog and currency, quote shipping,
        charge payment, book shipping), synchronously, and the first one that fails or hangs
        fails the order. So an alert on
        checkout almost never means checkout is broken — it means one of cart, currency,
        product-catalog, shipping, payment or email is, and checkout is the first place the
        symptom is visible. The RCA agent's job is to skip the wasted minutes spent looking at
        the service that paged and go straight to the dependency graph. email is the exception:
        it is called after the order commits and is best-effort, so an email failure delays a
        confirmation mail but does not fail the order.
        """,
    )

    # ---- slide 3: dependency diagram (text-rendered, described in notes)
    s = prs.slides.add_slide(blank)
    tb = s.shapes.add_textbox(Inches(0.7), Inches(0.4), Inches(12), Inches(0.9))
    tb.text_frame.text = "Dependency graph"
    tb.text_frame.paragraphs[0].font.size = Pt(32)
    graph = s.shapes.add_textbox(Inches(0.9), Inches(1.5), Inches(11.5), Inches(5.5))
    gf = graph.text_frame
    gf.word_wrap = True
    for i, (name, svc) in enumerate(sorted(p.services.items())):
        deps = ", ".join(svc.depends_on) or "(none)"
        para = gf.paragraphs[0] if i == 0 else gf.add_paragraph()
        para.text = f"{name}  ->  {deps}"
        para.font.size = Pt(13)
    _notes(
        s,
        """
        This is a DAG, not a tree: cart and product-catalog each have two parents (product-catalog
        is under both frontend and recommendation; cart is under both frontend and checkout).
        otel-collector and flagd are deliberately omitted — every service depends on them, so
        including them would make the graph say "everything's parent is the collector" and hide
        the business structure. The health rollup in app/system-monitor uses this same graph and
        makes the same omission. The leaves with no dependencies — currency, payment, quote, ad,
        image-provider — are the services that can only fail in isolation; a failure there never
        has an upstream cause, which shortens their runbooks.
        """,
    )

    # ---- slide 4: telemetry
    _bullets(
        "How it is observed",
        [
            "Every service emits OpenTelemetry traces, metrics and logs over OTLP",
            "otel-collector receives, batches, exports -> Prometheus (metrics) + Jaeger (traces)",
            "RED metrics per service: request rate, error rate, duration p95/p99",
            "Sentinel's RCA agent reads Prometheus and Jaeger; it does not read logs",
        ],
        """
        The observability boundary is a design decision, not an omission. The RCA agent reads
        metrics (to see which service's RED signals moved and when) and traces (to see which span
        in a failing request carries the error or the latency). It does not read logs: log search
        is high-recall, low-precision, and an agent that greps logs tends to anchor on the first
        plausible-looking line. Metrics and traces give a structured, low-cardinality signal that
        an agent can reason over. The one thing not visible in either is a flag flip — flagd emits
        an evaluation counter but nothing pages on a change — which is why "check flagd" is a
        manual step in every runbook and a known contributing factor in most post-mortems.
        """,
    )

    # ---- slide 5: failure model
    fault_lines = [
        f"{f.flag}  ({f.service}): {f.effect}" for f in sorted(p.faults.values(), key=lambda x: x.service)
    ]
    _bullets(
        "How it fails: the injectable faults",
        fault_lines[:8],
        """
        Faults are injected through flagd by flipping a flag in demo.flagd.json, which flagd
        watches and reloads live. app/incident-generator flips these on a schedule and writes an
        incidents.jsonl log — that log is the ground truth the RCA agent is scored against, because
        the generator knows which flag it set. The faults fall into three shapes: hard errors
        (paymentFailure, cartFailure, adFailure — error rate jumps), resource exhaustion (adHighCpu,
        emailMemoryLeak, recommendationCacheFailure — latency or memory climbs, error rate may stay
        flat and not alert), and latency injection (intlShippingSlowdown, imageSlowLoad — duration
        up, everything else green). The second and third shapes are the ones that need a latency
        SLO to be detected at all.
        """,
    )

    # ---- slide 6: what does NOT go through RAG
    _bullets(
        "Scope boundary",
        [
            "In the knowledge corpus: runbooks, post-mortems, ADRs, glossary, this deck",
            "NOT in the corpus: live metrics, live traces, current flag state, deploy history",
            "Live state is read by agent tools at investigation time, never retrieved from the index",
            "Live-state tools are never cached (core/cache.LIVE_TOOLS)",
        ],
        """
        The corpus is the stable knowledge — how a service is supposed to work, what a past
        incident taught, why a decision was made. It is embedded and retrieved. Live state — what
        the error rate is right now, what the last deploy was, which flags are set — is fetched by
        tools during an investigation and must never be cached or indexed, because a cached health
        check is worse than no health check. Getting this boundary wrong in either direction is
        the classic RAG-for-ops mistake: indexing metrics (they are stale the moment they are
        embedded) or trying to answer "why is checkout slow" purely from documents (the answer is
        in a trace from four minutes ago, not in a runbook).
        """,
    )

    out = KNOWLEDGE / "architecture" / "astroshop-architecture.pptx"
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    return rel(out)


# ----------------------------------------------------------------- two-column PDF
def _build_pdf(p: Platform) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        NextPageTemplate,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12, spaceAfter=6)
    h = ParagraphStyle("h", parent=styles["Heading2"], fontSize=11, spaceBefore=8, spaceAfter=4)
    title = ParagraphStyle("t", parent=styles["Title"], fontSize=17)

    out = KNOWLEDGE / "architecture" / "dependency-model.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = BaseDocTemplate(str(out), pagesize=letter, topMargin=0.9 * inch, bottomMargin=0.8 * inch)
    lm, w, gap = 0.75 * inch, doc.width, 0.3 * inch
    col_w = (w - gap) / 2
    left = Frame(lm, doc.bottomMargin, col_w, doc.height, id="L")
    right = Frame(lm + col_w + gap, doc.bottomMargin, col_w, doc.height, id="R")
    full = Frame(lm, doc.bottomMargin, w, doc.height, id="F")

    def _header(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(lm, letter[1] - 0.6 * inch, f"{p.name} — dependency and tier model")
        canvas.drawRightString(letter[0] - lm, letter[1] - 0.6 * inch, "internal reference")
        canvas.line(lm, letter[1] - 0.65 * inch, letter[0] - lm, letter[1] - 0.65 * inch)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawCentredString(letter[0] / 2, 0.55 * inch, f"page {_doc.page}")
        canvas.restoreState()

    doc.addPageTemplates(
        [
            PageTemplate(id="two", frames=[left, right], onPage=_header),
            PageTemplate(id="one", frames=[full], onPage=_header),
        ]
    )

    flow: list = [NextPageTemplate("two")]
    flow.append(Paragraph(f"{p.name} dependency and tier model", title))
    flow.append(Spacer(1, 8))
    flow.append(Paragraph(p.summary, body))

    flow.append(Paragraph("Tiers", h))
    flow.append(
        Paragraph(
            "Tier 0 is the customer-facing critical path: a failure degrades the storefront "
            "directly. Tier 1 is supporting synchronous work — a failure degrades a tier-0 "
            "service but the storefront stays up. Tier 2 is asynchronous back-office processing "
            "off the request path entirely; a failure delays ledger and fraud data, not orders.",
            body,
        )
    )
    for tier in (0, 1, 2):
        names = sorted(s.name for s in p.services.values() if s.tier == tier)
        flow.append(Paragraph(f"<b>Tier {tier}:</b> {', '.join(names)}", body))

    flow.append(Paragraph("Reading the graph", h))
    flow.append(
        Paragraph(
            "The table on the following page lists every service, its tier, its owning team, the "
            "services it calls, and its blast radius — the set of services that transitively "
            "depend on it and therefore degrade when it does. The blast radius is what turns "
            "\"service X is down\" into \"and here is who will be paged for it\". Services with an "
            "empty <i>calls</i> column are leaves: they have no upstream cause, so their runbooks "
            "have no \"rule out upstream\" step.",
            body,
        )
    )
    flow.append(
        Paragraph(
            "otel-collector and flagd are in the deployment but excluded from this model: every "
            "service depends on them, so including them collapses the graph. The health rollup in "
            "app/system-monitor makes the same exclusion for the same reason.",
            body,
        )
    )

    flow.append(Paragraph("Fault ownership", h))
    for team in sorted(p.teams.values(), key=lambda t: t.name):
        faulty = sorted({f.flag for s in team.owns if s in p.services for f in p.services[s].faults})
        if faulty:
            flow.append(Paragraph(f"<b>{team.name}</b> ({team.pager}): {', '.join(faulty)}", body))

    # wide table on its own full-width page
    flow.append(NextPageTemplate("one"))
    flow.append(PageBreak())
    flow.append(Paragraph("Service dependency table", h))
    rows = [["Service", "Tier", "Team", "Calls", "Blast radius"]]
    cell = ParagraphStyle("c", parent=body, fontSize=7.5, leading=9, spaceAfter=0)
    for name, svc in sorted(p.services.items()):
        rows.append(
            [
                Paragraph(name, cell),
                str(svc.tier),
                Paragraph(svc.team, cell),
                Paragraph(", ".join(svc.depends_on) or "—", cell),
                Paragraph(", ".join(p.blast_radius(name)) or "—", cell),
            ]
        )
    tbl = Table(rows, colWidths=[1.1 * inch, 0.4 * inch, 0.9 * inch, 1.9 * inch, 2.4 * inch], repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b3a55")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8a9bb5")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f8")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    flow.append(tbl)

    doc.build(flow)
    return rel(out)


def main() -> int:
    p = load()
    deck = _build_deck(p)
    pdf = _build_pdf(p)
    print("wrote:")
    print(f"  {deck}")
    print(f"  {pdf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
