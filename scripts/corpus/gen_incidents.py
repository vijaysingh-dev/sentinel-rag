"""Generate incident post-mortems from a synthetic-but-deterministic incident history.

    uv run python scripts/corpus/gen_incidents.py [--seed 7] [--count 14]

Each single-fault post-mortem is built from one fault in scripts/corpus/services.yaml: the fault's
`signature` becomes the detection story, the blast radius becomes the customer-impact section, and
the owning team becomes the responders. The timeline is generated so that the *first* alert fires
on a downstream critical-path service and the cause is only localised a few minutes in — the
pattern the RCA agent is built to short-circuit.

Two more incident shapes exist beyond "one flag, one service", because every real RCA benchmark
needs harder cases than "there was exactly one thing wrong":

* **Compound** (COMPOUND_PAIRS) — two flags active in an overlapping window. One pair is causally
  unrelated (kafka + email: coincidence, not cascade); the other is causally adjacent but still two
  distinct faults (recommendation's cache failure fans load onto product-catalog, which *also* has
  its own unrelated single-product failure flag active) — the harder case, because "a dependency is
  degraded" is a real and correct observation that is nonetheless not the whole answer. Both test
  whether the agent keeps investigating after finding *a* culprit instead of stopping at the first.
* **Organic** (ORGANIC_INCIDENTS) — no flagd flag at all. `currency` and `astronomy-db` have no
  fault flag in demo.flagd.json, so their incidents are root-caused in prose only (a bad deploy, a
  connection-pool exhaustion) — testing that the agent's flagd check correctly reports "nothing is
  set" and moves on to deploys/traces instead of stalling because the tool it trusts most came back
  empty.

Three outputs beyond the Markdown documents:

* ``scripts/corpus/_incident_index.json`` — {service: [{id,title,date,cause}]}, consumed by
  gen_runbooks.py for each runbook's "Past incidents" section.
* ``scripts/corpus/_incident_truth.jsonl`` — one row per incident. Every row carries a `causes`
  list (`[{service, flag, effect}]`, `flag: null` for an organic cause) plus an `incident_type`
  discriminator (`single` / `compound` / `organic`); single-fault rows also keep the flat
  `root_cause_service` / `root_cause_flag` fields so gen_golden.py's existing cross-check and any
  other single-cause-only reader keep working unchanged. This is the seed for data/golden.jsonl RCA
  trajectory examples and for scoring the agent: unlike a hand-written golden answer, this one is
  correct by construction because the generator chose the fault(s).

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

# Two flags, active in an overlapping window, with genuinely no causal link between them — the
# alerts happen to land close together and an investigation has to establish they are two
# incidents, not one with a confusing signature.
COMPOUND_UNRELATED = ("kafkaQueueProblems", "emailMemoryLeak")

# Two flags where one *does* affect the other's traffic (recommendation's cache failure fans
# extra load onto product-catalog) but the second fault is a distinct, unrelated single-product
# failure — so "product-catalog is degraded" is a true and tempting stopping point that is still
# the wrong final answer on its own.
COMPOUND_ADJACENT = ("recommendationCacheFailure", "productCatalogFailure")

COMPOUND_PAIRS = [COMPOUND_UNRELATED, COMPOUND_ADJACENT]

# Services with no flagd fault flag at all (services.yaml `faults: []`), each given one
# prose-only, non-flag root cause. No signature/effect fields exist for these in services.yaml
# because there is no flag — the cause lives only in ORGANIC_INCIDENTS.
ORGANIC_INCIDENTS = [
    {
        "service": "currency",
        "headline": "elevated latency after a deploy",
        "cause_summary": (
            "The 14:20 deploy added a synchronous call to an external exchange-rate refresh on "
            "every conversion request, instead of reading the rate cached at startup."
        ),
        "signature": (
            "currency duration_p95 up ~6x immediately after the 14:20 deploy; "
            "error rate flat; no fault flag active."
        ),
        "fix": (
            "Rolled back to the previous image tag. Follow-up: cache the exchange rate and "
            "refresh it on a timer, not per-request."
        ),
    },
    {
        "service": "astronomy-db",
        "headline": "connection pool exhaustion",
        "cause_summary": (
            "product-catalog's connection pool to astronomy-db was sized for steady-state "
            "traffic; a routine traffic ramp from the load generator exceeded it, and requests "
            "queued for a free connection instead of failing fast."
        ),
        "signature": (
            "pg_stat_activity_count pinned at the configured max; product-catalog duration_p95 up; "
            "astronomy-db itself shows no errors, only saturation."
        ),
        "fix": (
            "Raised the pool size and added a queue-wait timeout so callers fail fast instead of "
            "queuing indefinitely. No flag to reset — this was capacity, not injection."
        ),
    },
]


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


# ------------------------------------------------------------------------------------ compound
def _compound_post_mortem(p: Platform, inc_id: str, date: datetime, pair: tuple[str, str], sev: str) -> str:
    """Two faults, one incident. Timelines interleave; the write-up narrates telling them apart."""
    f1, f2 = p.faults[pair[0]], p.faults[pair[1]]
    s1, s2 = p.services[f1.service], p.services[f2.service]
    related = f2.service in p.blast_radius(f1.service) or f1.service in p.blast_radius(f2.service)

    L: list[str] = []
    L.append(f"# {inc_id}: two faults active — {f1.service} and {f2.service}")
    L.append("")
    L.append(f"**Date:** {date.strftime('%Y-%m-%d')}  ")
    L.append(f"**Severity:** {sev}  ")
    L.append("**Duration:** 20 minutes  ")
    downstream = (set(p.blast_radius(f1.service)) | set(p.blast_radius(f2.service))) - {f1.service, f2.service}
    L.append(f"**Services affected:** {f1.service}, {f2.service}"
             + (f", and {', '.join(sorted(downstream))} downstream" if downstream else ""))
    responder_teams = dict.fromkeys([p.teams[s1.team].name, p.teams[s2.team].name])
    L.append(f"**Responders:** {', '.join(responder_teams)}")
    L.append("")

    L.append("## Summary")
    L.append("")
    if related:
        L.append(
            f"Two alerts fired minutes apart: `{f1.service}` (`{f1.flag}`) and `{f2.service}` "
            f"(`{f2.flag}`). `{f1.service}` calls `{f2.service}` in its normal operation, so the "
            f"first hypothesis was a single cascading failure — `{f1.service}`'s fault driving "
            f"`{f2.service}` into distress. That hypothesis does not survive contact with the "
            f"signatures: `{f2.flag}` only affects one specific request shape "
            f"({f2.effect.rstrip('.')}), not the traffic volume `{f1.flag}` would have added. "
            f"**These are two independent faults that happened to overlap**, not one causing the "
            f"other."
        )
    else:
        L.append(
            f"Two unrelated alerts fired within the same window: `{f1.service}` (`{f1.flag}`) and "
            f"`{f2.service}` (`{f2.flag}`). Neither service appears in the other's dependency "
            f"graph in either direction. **The overlap is coincidence, not cascade** — two "
            f"separately-scheduled fault injections landed close together."
        )
    L.append("")

    L.append("## Customer impact")
    L.append("")
    L.append(f"- **{f1.service}:** {_impact(p, f1)}")
    L.append(f"- **{f2.service}:** {_impact(p, f2)}")
    L.append("")

    L.append("## Timeline")
    L.append("")
    t1 = date
    t2 = date + timedelta(minutes=4)
    events = [
        (t1, f"`{f1.flag}` set to its fault variant on `{f1.service}`. No alert yet."),
        (t1 + timedelta(minutes=2), f"First alert: `{f1.service}` signature matches `{f1.flag}` — {f1.signature}"),
        (t2, f"`{f2.flag}` set to its fault variant on `{f2.service}`, independently of the above."),
        (t2 + timedelta(minutes=2), f"Second alert: `{f2.service}` signature matches `{f2.flag}` — {f2.signature}"),
        (
            t2 + timedelta(minutes=5),
            "Responders initially treat this as one incident given the timing overlap"
            + (f"; `{f1.service}` calls `{f2.service}`, so cascade is the natural first guess." if related else "."),
        ),
        (
            t2 + timedelta(minutes=9),
            f"flagd checked: both `{f1.flag}` and `{f2.flag}` are active independently, set 4 minutes apart. "
            "Two incidents, tracked and resolved separately from this point.",
        ),
        (t1 + timedelta(minutes=16), f"`{f1.flag}` reset. `{f1.service}` recovers."),
        (t2 + timedelta(minutes=16), f"`{f2.flag}` reset. `{f2.service}` recovers."),
        (t2 + timedelta(minutes=20), "Both services confirmed back at baseline. Incident closed."),
    ]
    for ts, text in sorted(events, key=lambda e: e[0]):
        L.append(f"- {ts.strftime('%H:%M')} — {text}")
    L.append("")

    L.append("## Root cause")
    L.append("")
    L.append(
        f"Two independent feature flags were active concurrently: `{f1.flag}` on `{f1.service}` "
        f"({f1.effect}) and `{f2.flag}` on `{f2.service}` ({f2.effect}). "
        + (
            "The dependency between the two services made this look like one cascading fault; "
            "it was two."
            if related
            else "There is no dependency relationship between the two services; the timing overlap "
            "was coincidental."
        )
    )
    L.append("")

    L.append("## Contributing factors")
    L.append("")
    L.append(
        "- No alert correlates flag changes with each other, so two flags active in the same "
        "window present as one confusing incident instead of two clear ones."
    )
    if related:
        L.append(
            f"- `{f2.service}` genuinely is a dependency of `{f1.service}`, which made the wrong "
            f"hypothesis (single cascade) a reasonable first guess, not a careless one. The "
            f"signature — a single-product failure rather than a load-shaped one — is what "
            f"separates them."
        )
    L.append("")

    L.append("## What went well")
    L.append("")
    L.append(
        "- Comparing each service's actual failure *signature* against its known failure modes "
        "(rather than assuming shared timing implies a shared cause) correctly split this into "
        "two incidents within minutes of pulling both flag states."
    )
    L.append("")

    L.append("## Action items")
    L.append("")
    L.append(
        f"1. When two alerts overlap, check flagd for *all* active flags, not just the one for "
        f"the service that paged — a second unrelated flag is easy to miss if the search stops "
        f"at the first explanation found. *(owner: {p.teams['platform-eng'].name})*"
    )
    L.append("")

    L.append("## Detection signatures")
    L.append("")
    L.append("```")
    L.append(f"{f1.service} ({f1.flag}): {f1.signature}")
    L.append(f"{f2.service} ({f2.flag}): {f2.signature}")
    L.append("```")
    L.append("")
    return "\n".join(L)


# -------------------------------------------------------------------------------------- organic
def _organic_post_mortem(p: Platform, inc_id: str, date: datetime, spec: dict, sev: str) -> str:
    """No flagd flag. Root cause is a deploy or a resource limit, narrated in prose only."""
    svc = p.services[spec["service"]]
    team = p.teams[svc.team]
    hits = p.critical_path_hits(spec["service"])
    radius = p.blast_radius(spec["service"])
    surface = hits[0] if hits else spec["service"]

    L: list[str] = []
    L.append(f"# {inc_id}: {spec['service']} {spec['headline']}")
    L.append("")
    L.append(f"**Date:** {date.strftime('%Y-%m-%d')}  ")
    L.append(f"**Severity:** {sev}  ")
    L.append("**Duration:** 25 minutes  ")
    L.append(f"**Services affected:** {spec['service']}"
             + (f", and {', '.join(radius)} downstream" if radius else ""))
    L.append(f"**Responders:** {team.name} ({team.pager})")
    L.append("")

    L.append("## Summary")
    L.append("")
    extra = (
        f" The first alert fired on `{surface}`, not `{spec['service']}` directly."
        if surface != spec["service"]
        else ""
    )
    L.append(
        spec["cause_summary"] + " No feature flag was involved — flagd showed nothing active for "
        "the entire window, which cost several minutes early on: the on-call habit here is to "
        "check flagd first, and an empty result was read as 'nothing to see' rather than as "
        "a real answer to keep investigating past." + extra
    )
    L.append("")

    L.append("## Customer impact")
    L.append("")
    L.append(
        f"Degradation on {', '.join([spec['service'], *radius])} for the duration."
        if radius
        else f"Contained to `{spec['service']}`; no wider customer-facing degradation observed."
    )
    L.append("")

    L.append("## Timeline")
    L.append("")

    def at(mins: int, text: str) -> str:
        return f"- {(date + timedelta(minutes=mins)).strftime('%H:%M')} — {text}"

    L.append(at(0, f"First alert: `{surface}` duration_p95 crosses its page threshold."))
    L.append(
        at(
            3,
            "On-call queries flagd: no fault flag active anywhere on the platform. "
            "Investigation continues past the usual first check.",
        )
    )
    L.append(at(8, f"Trace inspection points at `{spec['service']}`: {spec['signature']}"))
    L.append(at(14, f"{team.name} confirms: {spec['cause_summary'][0].lower()}{spec['cause_summary'][1:]}"))
    L.append(at(18, f"Mitigation applied: {spec['fix']}"))
    L.append(at(25, f"`{surface}` metrics back at baseline for 3 minutes. Incident closed."))
    L.append("")

    L.append("## Root cause")
    L.append("")
    L.append(spec["cause_summary"] + " No fault flag was active; this was not an injected failure.")
    L.append("")

    L.append("## Contributing factors")
    L.append("")
    L.append(
        "- This environment's incidents are overwhelmingly flag-driven, which made the flagd "
        "check feel like the whole investigation instead of the first step of it. An empty flagd "
        "result is evidence, not an answer."
    )
    if surface != spec["service"]:
        L.append(f"- The alert surfaced on `{surface}`, one hop from the actual cause at `{spec['service']}`.")
    L.append("")

    L.append("## What went well")
    L.append("")
    L.append(
        "- Once flagd came back empty, the team moved to traces and deploy history rather than "
        "re-checking flagd repeatedly."
    )
    L.append("")

    L.append("## Action items")
    L.append("")
    L.append(f"1. {spec['fix']} *(owner: {team.name})*")
    L.append("")

    L.append("## Detection signature")
    L.append("")
    L.append("```")
    L.append(spec["signature"])
    L.append("```")
    L.append("")
    return "\n".join(L)


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
                "incident_type": "single",
                "alert_fires_on": surface,
                "root_cause_service": fault.service,
                "root_cause_flag": fault.flag,
                "causes": [{"service": fault.service, "flag": fault.flag, "effect": fault.effect}],
                "affected_services": [fault.service] + p.blast_radius(fault.service),
                "expected_finding": (
                    f"The {fault.flag} feature flag was set to its fault variant; "
                    f"{fault.service} is healthy and the failure is injected at the flag layer."
                ),
                "signature": fault.signature,
            }
        )

    # ---------------------------------------------------------------------------- compound
    compound_base = base + timedelta(days=80)
    for n, pair in enumerate(COMPOUND_PAIRS, start=1):
        f1, f2 = p.faults[pair[0]], p.faults[pair[1]]
        date = compound_base + timedelta(days=n * 3, hours=rng.randint(-1, 5))
        inc_id = f"INC-{2700 + n * 7}"
        sev = "P1" if p.services[f1.service].critical or p.services[f2.service].critical else "P2"
        body = _compound_post_mortem(p, inc_id, date, pair, sev)
        stem = f"{inc_id.lower()}-{f1.service}-{f2.service}-compound"
        written.append(rel(write("incident", stem, body)))

        title = f"{f1.service} + {f2.service} — two faults overlapping"
        for svc in (f1.service, f2.service):
            index.setdefault(svc, []).append(
                {"id": inc_id, "title": title, "date": date.strftime("%Y-%m-%d"), "cause": f"compound — see {inc_id}"}
            )

        truth_rows.append(
            {
                "incident_id": inc_id,
                "doc": stem,
                "date": date.strftime("%Y-%m-%d"),
                "incident_type": "compound",
                "alert_fires_on": f1.service,
                "root_cause_service": None,
                "root_cause_flag": None,
                "causes": [
                    {"service": f1.service, "flag": f1.flag, "effect": f1.effect},
                    {"service": f2.service, "flag": f2.flag, "effect": f2.effect},
                ],
                "affected_services": sorted(
                    {f1.service, f2.service, *p.blast_radius(f1.service), *p.blast_radius(f2.service)}
                ),
                "expected_finding": (
                    f"Two independent fault flags were active concurrently: {f1.flag} on {f1.service} "
                    f"and {f2.flag} on {f2.service}. They are separate incidents that overlapped in "
                    f"time, not one cascading from the other."
                ),
                "signature": f"{f1.service}: {f1.signature} | {f2.service}: {f2.signature}",
            }
        )

    # ----------------------------------------------------------------------------- organic
    organic_base = base + timedelta(days=90)
    for n, spec in enumerate(ORGANIC_INCIDENTS, start=1):
        date = organic_base + timedelta(days=n * 4, hours=rng.randint(-1, 5))
        inc_id = f"INC-{2760 + n * 7}"
        svc_name = spec["service"]
        sev = _sev(p, Fault(service=svc_name, flag="", effect="", signature=""))
        body = _organic_post_mortem(p, inc_id, date, spec, sev)
        stem = f"{inc_id.lower()}-{svc_name}-organic"
        written.append(rel(write("incident", stem, body)))

        surface = (p.critical_path_hits(svc_name) or [svc_name])[0]
        index.setdefault(svc_name, []).append(
            {
                "id": inc_id,
                "title": f"{svc_name} {spec['headline']}",
                "date": date.strftime("%Y-%m-%d"),
                "cause": "no fault flag — see post-mortem",
            }
        )

        truth_rows.append(
            {
                "incident_id": inc_id,
                "doc": stem,
                "date": date.strftime("%Y-%m-%d"),
                "incident_type": "organic",
                "alert_fires_on": surface,
                "root_cause_service": svc_name,
                "root_cause_flag": None,
                "causes": [{"service": svc_name, "flag": None, "effect": spec["cause_summary"]}],
                "affected_services": [svc_name] + p.blast_radius(svc_name),
                "expected_finding": spec["cause_summary"] + " No fault flag was active.",
                "signature": spec["signature"],
            }
        )

    INDEX_OUT.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    TRUTH_OUT.write_text("".join(json.dumps(r) + "\n" for r in truth_rows))

    print(
        f"wrote {len(written)} post-mortems "
        f"({len(chosen)} single, {len(COMPOUND_PAIRS)} compound, {len(ORGANIC_INCIDENTS)} organic):"
    )
    for w in sorted(written):
        print(f"  {w}")
    print(f"\nground truth:\n  {rel(TRUTH_OUT)} ({len(truth_rows)} incidents)\n  {rel(INDEX_OUT)}")
    print("\nre-run gen_runbooks.py now to pick up the 'Past incidents' cross-references.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
