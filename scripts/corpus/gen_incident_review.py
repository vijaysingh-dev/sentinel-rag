"""Generate the quarterly incident-review — a trend doc aggregating the incidents gen_incidents.py wrote.

    uv run python scripts/corpus/gen_incident_review.py

Every other document in `knowledge/incident/` is one incident. This is the one document that is
*about* the set of them: which team paged most, which fault type recurs, which services show up
in more than one incident. It exists to give the corpus a document whose content is derived
aggregate knowledge rather than a single event — retrieval over "which service caused the most
incidents this quarter" has no single source chunk to match against unless one document actually
says it, the same reasoning gen_ground_truth.py's provenance note makes about generated-vs-real
ground truth, applied to a different stage.

Reads `scripts/corpus/_incident_truth.jsonl`, so it must run after gen_incidents.py (same ordering
constraint gen_runbooks.py has, for the same reason: it consumes a file that script writes).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from _model import Platform, load, rel, write

sys.path.insert(0, str(Path(__file__).parent))

TRUTH = Path(__file__).parent / "_incident_truth.jsonl"


def _load_truth() -> list[dict]:
    if not TRUTH.exists():
        return []
    return [json.loads(line) for line in TRUTH.read_text().splitlines() if line.strip()]


def _service_counts(rows: list[dict]) -> Counter:
    """Counts a service once per incident it caused, including each cause of a compound row —
    a service that is one of two causes in a compound incident still gets credited with causing
    an incident, it just shares that incident with another service."""
    c: Counter = Counter()
    for row in rows:
        for cause in row["causes"]:
            c[cause["service"]] += 1
    return c


def _team_counts(p: Platform, rows: list[dict]) -> Counter:
    c: Counter = Counter()
    for row in rows:
        for cause in row["causes"]:
            c[p.teams[p.services[cause["service"]].team].name] += 1
    return c


def _type_counts(rows: list[dict]) -> Counter:
    return Counter(row.get("incident_type", "single") for row in rows)


def _body(p: Platform, rows: list[dict]) -> str:
    if not rows:
        return "# Quarterly incident review\n\nNo incidents recorded for this period.\n"

    dates = sorted(row["date"] for row in rows)
    svc_counts = _service_counts(rows)
    team_counts = _team_counts(p, rows)
    type_counts = _type_counts(rows)
    repeat_services = [s for s, n in svc_counts.items() if n > 1]

    lines = [
        "# Quarterly incident review",
        "",
        f"{len(rows)} incidents recorded, {dates[0]} through {dates[-1]}. Generated from "
        "`scripts/corpus/_incident_truth.jsonl` — every incident this document aggregates has its "
        "own full post-mortem in `knowledge/incident/`; this page is the roll-up, not a "
        "replacement for reading the individual write-ups.",
        "",
        "## By incident type",
        "",
        "| Type | Count | What it means |",
        "| --- | --- | --- |",
        f"| single | {type_counts.get('single', 0)} | one fault flag, one root cause |",
        f"| compound | {type_counts.get('compound', 0)} | two faults active in the same window, independently |",
        f"| organic | {type_counts.get('organic', 0)} | no fault flag — a deploy or a resource limit |",
        "",
        "## By responding team",
        "",
        "| Team | Incidents caused |",
        "| --- | --- |",
    ]
    for team, n in team_counts.most_common():
        lines.append(f"| {team} | {n} |")
    lines += [
        "",
        "## By service",
        "",
        "| Service | Incidents caused |",
        "| --- | --- |",
    ]
    for svc, n in svc_counts.most_common():
        lines.append(f"| `{svc}` | {n} |")
    lines.append("")

    lines.append("## Repeat offenders")
    lines.append("")
    if repeat_services:
        lines.append(
            "A service showing up more than once in one review period is a signal no single "
            "post-mortem's action items can surface on its own — check its runbook's Known "
            "failure modes section for whether the recurring cause is the same flag each time or "
            "a different one."
        )
        lines.append("")
        for svc in sorted(repeat_services, key=lambda s: -svc_counts[s]):
            incs = [row["incident_id"] for row in rows if any(c["service"] == svc for c in row["causes"])]
            lines.append(f"- `{svc}` — {svc_counts[svc]} incidents ({', '.join(incs)})")
    else:
        lines.append("No service caused more than one incident this period.")
    lines.append("")

    lines.append("## Notable")
    lines.append("")
    compound_rows = [r for r in rows if r.get("incident_type") == "compound"]
    organic_rows = [r for r in rows if r.get("incident_type") == "organic"]
    if compound_rows:
        lines.append(
            f"- {len(compound_rows)} compound incident(s) this period — two independent faults "
            f"overlapping in time. These take longer to close than a single-fault incident of the "
            f"same severity because the investigation has to first establish that it is looking "
            f"at two things, not one; see each compound post-mortem's \"Contributing factors\" "
            f"section for the specific reasoning that split them."
        )
    if organic_rows:
        lines.append(
            f"- {len(organic_rows)} incident(s) this period had no fault flag at all — a deploy or "
            f"a resource limit, not an injection. flagd coming back empty is not by itself grounds "
            f"to stop investigating; both organic post-mortems record time lost to exactly that "
            f"assumption."
        )
    if not compound_rows and not organic_rows:
        lines.append("Nothing outside the ordinary single-fault pattern this period.")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    p = load()
    rows = _load_truth()
    if not rows:
        print("scripts/corpus/_incident_truth.jsonl missing or empty — run gen_incidents.py first", file=sys.stderr)
        return 1
    path = write("reference", "quarterly-incident-review", _body(p, rows))
    print(f"wrote {rel(path)} ({len(rows)} incidents aggregated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
