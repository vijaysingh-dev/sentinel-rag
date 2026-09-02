"""Generate one operational runbook per business service.

    uv run python scripts/corpus/gen_runbooks.py

Output: knowledge/runbook/<service>.md, doc_type "runbook", chunked step-by-step by the runbook
strategy so a retrieval for "cart is throwing errors" returns the whole procedure, not step 4 of 7.

The content is derived from scripts/corpus/services.yaml: the dependency list drives the "check
upstream first" section, each fault's `signature` becomes a diagnostic branch, and the owning
team drives the escalation block. A handful are emitted as DOCX instead of Markdown (see
DOCX_RUNBOOKS) so the parser bake-off has non-Markdown runbooks to score.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _model import KNOWLEDGE, Platform, Service, load, rel, write
from _promql import error_ratio, query_for

# Written by gen_incidents.py: {service: [ {id, title, date, cause}, ... ] }. Absent on a first
# run before incidents exist — the runbook just omits the cross-reference section then.
INCIDENT_INDEX = Path(__file__).parent / "_incident_index.json"

# Emitted as Word documents rather than Markdown. Chosen to be procedures a team would plausibly
# keep in a shared drive as .docx, and to give parsing/ non-trivial DOCX with tables and lists.
DOCX_RUNBOOKS = {"checkout", "payment"}


def _thresholds_table(svc: Service) -> str:
    slo = svc.slo
    rows = ["| Signal | Objective | Page at |", "| --- | --- | --- |"]
    if "availability" in slo:
        rows.append(f"| Availability ({28}d) | {slo['availability']} | error budget 50% burned in 1h |")
    if "latency_p95_ms" in slo:
        p95 = slo["latency_p95_ms"]
        rows.append(f"| Duration p95 | < {p95} ms | > {int(p95 * 1.5)} ms for 5m |")
    if "latency_p99_ms" in slo:
        p99 = slo["latency_p99_ms"]
        rows.append(f"| Duration p99 | < {p99} ms | > {int(p99 * 1.5)} ms for 5m |")
    if "freshness_lag_s" in slo:
        rows.append(f"| Consumer lag | < {slo['freshness_lag_s']} s | > {slo['freshness_lag_s'] * 3} s for 10m |")
    if "produce_latency_p95_ms" in slo:
        pp = slo["produce_latency_p95_ms"]
        rows.append(f"| Produce p95 | < {pp} ms | > {pp * 3} ms |")
    return "\n".join(rows)


def _body(p: Platform, svc: Service) -> str:
    team = p.teams[svc.team]
    tech = f"{svc.language}" + (f" / {svc.runtime}" if svc.runtime else "")
    up = list(svc.depends_on)
    crit_hits = p.critical_path_hits(svc.name)

    lines: list[str] = []
    lines.append(f"# {svc.name} runbook")
    lines.append("")
    lines.append(
        f"{svc.role} Implemented in {tech}. Owned by the {team.name} team "
        f"({team.pager}, {team.slack})."
    )
    lines.append("")

    lines.append("## When this fires")
    lines.append("")
    if crit_hits:
        lines.append(
            f"`{svc.name}` is on the customer-facing critical path. A failure here surfaces on "
            f"{', '.join(f'`{s}`' for s in crit_hits)} as elevated errors or latency, often before "
            f"a `{svc.name}` alert fires. If you were paged for one of those and the cause is not "
            f"obvious there, check `{svc.name}` early."
        )
    else:
        lines.append(
            f"`{svc.name}` is tier {svc.tier}. A failure here degrades "
            + (
                f"{', '.join(f'`{s}`' for s in p.blast_radius(svc.name))}"
                if p.blast_radius(svc.name)
                else "no other service directly"
            )
            + ", but does not by itself take the storefront down."
        )
    lines.append("")

    lines.append("## Thresholds")
    lines.append("")
    lines.append(_thresholds_table(svc))
    lines.append("")

    lines.append("## Dashboards and queries")
    lines.append("")
    lines.append(
        f"Grafana: the `{svc.name}` row on the Astroshop service dashboard. The queries behind "
        f"that row, for pasting straight into Prometheus:"
    )
    lines.append("")
    lines.append("```promql")
    lines.append("# error ratio")
    lines.append(error_ratio(svc.name))
    for m in svc.key_metrics:
        label, q = query_for(m, svc.name)
        lines.append(f"# {label}")
        lines.append(q)
    lines.append("```")
    lines.append("")
    lines.append(
        "In Jaeger, filter to "
        f"`service = {svc.name}` and sort by duration for the latency case, or add "
        "`error = true` for the error case."
    )
    lines.append("")

    lines.append("## Diagnose")
    lines.append("")
    step = 1
    if up:
        lines.append(
            f"{step}. **Rule out upstream first.** `{svc.name}` calls "
            f"{', '.join(f'`{d}`' for d in up)}. Check each one's error and latency panels for the "
            f"same window. If an upstream is unhealthy, work that runbook instead — a healthy "
            f"`{svc.name}` failing every call to a sick dependency is not a `{svc.name}` incident."
        )
        step += 1
    lines.append(
        f"{step}. Pull the RED panels for `{svc.name}`: "
        f"{', '.join(f'`{m}`' for m in svc.key_metrics)}. Note whether this is an **error** spike "
        f"(count of failed requests up, latency flat) or a **latency** spike (duration up, error "
        f"count flat). They have different causes and the next step depends on which."
    )
    step += 1
    lines.append(
        f"{step}. Open a trace for a failing `{svc.name}` request in Jaeger. The span that carries "
        f"the error status, or the span that dominates wall-clock time, points at the subsystem."
    )
    step += 1

    if svc.faults:
        lines.append(
            f"{step}. Check whether a fault flag is active. `{svc.name}` has injectable faults; "
            f"a flag flip is by far the most common cause in this environment. Query flagd for the "
            f"flags below and compare the timestamp to when the alert started."
        )
        step += 1

    lines.append("")

    if svc.faults:
        lines.append("## Known failure modes")
        lines.append("")
        for f in svc.faults:
            lines.append(f"### `{f.flag}`")
            lines.append("")
            lines.append(f"**Effect.** {f.effect}")
            lines.append("")
            lines.append(f"**Signature.** {f.signature}")
            lines.append("")
            lines.append(
                f"**Response.** Confirm the flag is set by querying flagd. Set it back to its "
                f"resting variant (`flagd-ui`, or the incident-generator reset). Recovery is "
                f"immediate on the next flag evaluation; no restart. Confirm "
                f"`{svc.key_metrics[0]}` returns to baseline before closing."
            )
            lines.append("")

    lines.append("## Mitigate")
    lines.append("")
    lines.append(
        "1. **If a fault flag is set**, clear it — that is the whole fix and it takes effect on "
        "the next flag evaluation with no restart."
    )
    lines.append(
        f"2. **If a recent deploy correlates**, roll `{svc.name}` back to the previous image tag. "
        f"`{svc.name}` is stateless"
        + (
            f" and its callers ({', '.join(f'`{d}`' for d in p.dependents.get(svc.name, ()))}) retry, "
            "so a rollback is safe mid-traffic."
            if p.dependents.get(svc.name)
            else ", so a rollback is safe mid-traffic."
        )
    )
    if svc.name == "cart":
        lines.append(
            "3. **If the process is wedged** (latency flat-lined, no logs), delete the pod by hand. "
            "`cart` has no container healthcheck, so the orchestrator will not restart it on a "
            "hang — only on a clean process exit."
        )
    elif "memory" in " ".join(f.flag for f in svc.faults).lower() or svc.name in {"email", "recommendation"}:
        lines.append(
            "3. **If memory is the symptom**, a restart buys time but is not a fix — capture a "
            "heap profile first if the incident is not customer-impacting, then restart."
        )
    else:
        lines.append(
            f"3. **If an upstream is the cause**, there is nothing to do on `{svc.name}` itself — "
            f"follow that service's runbook and let this alert clear on its own."
        )
    lines.append("")

    lines.append("## Escalate")
    lines.append("")
    lines.append(
        f"If the cause is not a flag, not a bad deploy, and not an unhealthy upstream, page the "
        f"{team.name} team at **{team.pager}** and post in {team.slack} with the trace id and the "
        f"alert link."
    )
    lines.append("")

    lines.append("## After the incident")
    lines.append("")
    lines.append(
        f"- Confirm `{svc.key_metrics[0]}` and the error ratio have held at baseline for 15 minutes."
    )
    lines.append("- Note the flag, deploy or upstream cause in the incident channel before closing.")
    lines.append(
        "- If this was a new failure mode, add it to the Known failure modes section of this "
        "runbook and to `scripts/corpus/services.yaml`."
    )
    lines.append("")

    incidents = _incidents_for(svc.name)
    if incidents:
        lines.append("## Past incidents")
        lines.append("")
        for inc in incidents:
            lines.append(f"- **{inc['id']}** ({inc['date']}) — {inc['title']}. Cause: {inc['cause']}.")
        lines.append("")

    if svc.notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(svc.notes.strip())
        lines.append("")

    return "\n".join(lines)


def _incidents_for(service: str) -> list[dict]:
    if not INCIDENT_INDEX.exists():
        return []
    try:
        return json.loads(INCIDENT_INDEX.read_text()).get(service, [])
    except (ValueError, OSError):
        return []


def _write_docx(stem: str, markdown_body: str, dest_dir: str) -> str:
    """Render the runbook markdown into a .docx with real headings, tables and numbered lists."""
    from docx import Document
    from docx.shared import Pt


    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    lines = markdown_body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("| "):
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            rows = [
                [c.strip() for c in row.strip().strip("|").split("|")]
                for row in table_lines
                if set(row.strip()) - set("|-: ")
            ]
            if rows:
                t = doc.add_table(rows=len(rows), cols=len(rows[0]))
                t.style = "Light Grid Accent 1"
                for r, cells in enumerate(rows):
                    for c, val in enumerate(cells):
                        t.rows[r].cells[c].text = val
            continue
        elif line and line[0].isdigit() and line[1:3] == ". ":
            doc.add_paragraph(_strip_md(line[3:]), style="List Number")
        elif line.strip():
            doc.add_paragraph(_strip_md(line))
        i += 1

    out = KNOWLEDGE / dest_dir / f"{stem}.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return rel(out)


def _strip_md(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def main() -> int:
    p = load()
    written: list[str] = []
    for name, svc in p.services.items():
        if svc.team == "platform-eng" and not svc.faults and name in {"otel-collector", "flagd"}:
            continue  # infra, covered by an architecture doc, not a per-service runbook
        body = _body(p, svc)
        if name in DOCX_RUNBOOKS:
            written.append(_write_docx(name, body, "runbook"))
        else:
            written.append(rel(write("runbook", name, body)))

    print(f"wrote {len(written)} runbooks:")
    for w in sorted(written):
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    sys.exit(main())
