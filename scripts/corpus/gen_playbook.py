"""Generate the dependency-failure playbook and the deploy/release history.

    uv run python scripts/corpus/gen_playbook.py

Two ops documents a real platform team keeps that `services.yaml` already has every fact needed
for, but that no existing generator produces:

* ``knowledge/reference/dependency-failure-playbook.md`` — "if X is down, what else breaks and how
  badly" for every service with at least one dependent, derived from `Platform.blast_radius` and
  `Platform.critical_path_hits`. This is retrieval for the multi_hop golden-set category
  (`if product-catalog goes down which customer-facing services break`) — today those questions
  are answered only by the runbooks' own "When this fires" section for the *one* service being
  asked about; this document answers the same question from the other direction, one page per
  upstream, so "what does X being down do to everything else" does not require reading every
  downstream service's runbook individually.

* ``knowledge/reference/deploy-history.md`` — every deploy a post-mortem names as a root cause,
  i.e. just the two organic incidents' triggering deploys from `_incident_truth.jsonl`
  (gen_incidents.py), not a full release log (see the docstring on `_deploy_history_body` for why
  a fuller synthetic log was considered and dropped). This exists because
  `data/fixtures/*-organic.json`'s `recent_deployments` tool result cites a deploy that, until
  this generator ran, had no corresponding document — an agent correctly reading the fixture and
  then searching the knowledge base for the deploy it names would find nothing. This is the
  retrieval-side half of the organic incidents; the fixture is the tool-side half.

Run after gen_incidents.py (like gen_runbooks.py) so the organic deploy entries exist to embed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _model import Platform, load, rel, write

sys.path.insert(0, str(Path(__file__).parent))

TRUTH = Path(__file__).parent / "_incident_truth.jsonl"


def _load_organic_deploys() -> list[dict]:
    """Pulls the two organic incidents' triggering deploys out of the truth file, so the deploy
    history and the incident post-mortems describe the same event instead of two invented ones."""
    if not TRUTH.exists():
        return []
    out = []
    for line in TRUTH.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("incident_type") == "organic":
            cause = row["causes"][0]
            out.append(
                {
                    "service": cause["service"],
                    "date": row["date"],
                    "note": cause["effect"],
                    "incident_ref": row["incident_id"],
                }
            )
    return out


def _playbook_entry(p: Platform, name: str) -> str:
    svc = p.services[name]
    radius = p.blast_radius(name)
    hits = p.critical_path_hits(name)
    team = p.teams[svc.team]

    lines = [f"## If `{name}` is down", "", f"{svc.role} Owned by {team.name} ({team.pager})."]
    if not radius:
        lines += [
            "",
            f"**Blast radius: none.** Nothing in `services.yaml` calls `{name}`. A `{name}` "
            f"outage is contained to `{name}` itself and does not by construction cascade — "
            f"confirm this against the live dependency graph if the service has grown new callers "
            f"since this playbook was generated.",
        ]
        lines.append("")
        return "\n".join(lines)

    direct = sorted(p.dependents.get(name, ()))
    lines += ["", f"**Direct callers:** {', '.join(f'`{d}`' for d in direct)}."]
    lines += [
        "",
        f"**Full blast radius ({len(radius)} service{'s' if len(radius) != 1 else ''}):** "
        + ", ".join(f"`{s}`" for s in radius) + ".",
    ]
    if hits:
        lines += [
            "",
            f"**Customer-facing impact: yes**, via {', '.join(f'`{s}`' for s in hits)}. Expect the "
            f"first page to land on one of those, not on `{name}` — see that service's own runbook "
            f"for its specific symptom, and `{name}`'s runbook for the actual fix.",
        ]
    else:
        lines += [
            "",
            "**Customer-facing impact: none directly.** Every service in the blast radius is "
            "tier 1/2; the storefront critical path does not run through here.",
        ]
    lines.append("")
    return "\n".join(lines)


def _playbook_body(p: Platform) -> str:
    lines = [
        "# Dependency-failure playbook",
        "",
        f"{p.name}'s full \"if this is down, what else breaks\" reference, one entry per service "
        "that has at least one caller. Generated from the dependency graph in "
        "`scripts/corpus/services.yaml` — the source of truth is the graph, not this document; "
        "regenerate rather than hand-edit if a dependency changes.",
        "",
        "Read this top-down when the alert has already fired somewhere and you need to know "
        "**where else to look**; read an individual runbook when you already know which service "
        "is the suspect and need **its own diagnose/mitigate steps**. This document answers "
        "\"what does X being down do to everything else\"; the runbooks answer \"what do I do "
        "about X\".",
        "",
    ]
    for name in sorted(p.services):
        if p.dependents.get(name) or p.blast_radius(name):
            lines.append(_playbook_entry(p, name))
    return "\n".join(lines)


def _deploy_history_body(p: Platform) -> str:
    """Only the deploys that actually matter: the ones an organic incident's root cause names.
    A full routine-release log was considered and dropped — it added forty rows of "routine
    release" noise for zero retrieval value, since nothing else in the corpus ever asks about a
    deploy that didn't cause an incident. If that changes (a golden-set question wants "when was
    X last deployed" for a service with no incident), add rows for that case specifically rather
    than reintroducing a full synthetic log."""
    organic = _load_organic_deploys()

    lines = [
        "# Deploy history",
        "",
        f"Every deploy in {p.name}'s history that a post-mortem names as a root cause. Not a full "
        "release log — this corpus has no use for a deploy that didn't cause an incident, so only "
        "the two below exist. Each row's note and timestamp come from "
        "`scripts/corpus/_incident_truth.jsonl` (gen_incidents.py) so this document and the "
        "post-mortem that cites it describe the same event.",
        "",
        "| Date | Service | Team | Note | Incident |",
        "| --- | --- | --- | --- | --- |",
    ]
    for dep in sorted(organic, key=lambda d: d["date"]):
        team = p.teams[p.services[dep["service"]].team]
        lines.append(f"| {dep['date']} | `{dep['service']}` | {team.name} | {dep['note']} | {dep['incident_ref']} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = load()
    written: list[str] = []
    written.append(rel(write("reference", "dependency-failure-playbook", _playbook_body(p))))
    written.append(rel(write("reference", "deploy-history", _deploy_history_body(p))))
    print(f"wrote {len(written)} documents:")
    for w in written:
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
