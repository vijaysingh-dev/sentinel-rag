"""The shared domain model every corpus generator reads.

Loads scripts/corpus/services.yaml once and exposes it as plain dataclasses, plus a few derived
views the generators keep needing: the reverse dependency graph (blast radius), the fault index
(flag -> which service, what signature), and the team roster.

Why dataclasses over passing the raw dict around: a typo in `svc.team` is an AttributeError here
and now, whereas `svc["taem"]` in a PDF generator is a silently blank cell discovered days later
when someone reads the document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

MODEL_PATH = Path(__file__).parent / "services.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = REPO_ROOT / "knowledge"


@dataclass(frozen=True)
class Fault:
    flag: str
    effect: str
    signature: str
    service: str = ""


@dataclass(frozen=True)
class Service:
    name: str
    tier: int
    team: str
    language: str
    role: str
    depends_on: tuple[str, ...]
    slo: dict[str, Any]
    key_metrics: tuple[str, ...]
    faults: tuple[Fault, ...]
    runtime: str = ""
    notes: str = ""

    @property
    def critical(self) -> bool:
        return self.tier == 0


@dataclass(frozen=True)
class Team:
    name: str
    pager: str
    slack: str
    owns: tuple[str, ...]


@dataclass
class Platform:
    name: str
    summary: str
    slo_window_days: int
    golden_signals: tuple[str, ...]
    services: dict[str, Service]
    teams: dict[str, Team]
    dependents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    faults: dict[str, Fault] = field(default_factory=dict)

    def service(self, name: str) -> Service:
        return self.services[name]

    def team_for(self, service_name: str) -> Team:
        return self.teams[self.services[service_name].team]

    def blast_radius(self, service_name: str) -> list[str]:
        """Every service that transitively depends on `service_name`, nearest first."""
        seen: dict[str, int] = {}
        frontier = [(d, 1) for d in self.dependents.get(service_name, ())]
        while frontier:
            node, depth = frontier.pop(0)
            if node in seen:
                continue
            seen[node] = depth
            frontier.extend((d, depth + 1) for d in self.dependents.get(node, ()))
        return sorted(seen, key=lambda n: (seen[n], n))

    def critical_path_hits(self, service_name: str) -> list[str]:
        """Blast-radius members that are themselves tier-0."""
        return [s for s in self.blast_radius(service_name) if self.services[s].critical]


@lru_cache
def load() -> Platform:
    raw = yaml.safe_load(MODEL_PATH.read_text())
    p = raw["platform"]

    services: dict[str, Service] = {}
    for name, sv in raw["services"].items():
        faults = tuple(
            Fault(service=name, flag=f["flag"], effect=f["effect"], signature=f["signature"])
            for f in sv.get("faults", [])
        )
        services[name] = Service(
            name=name,
            tier=sv["tier"],
            team=sv["team"],
            language=sv["language"],
            runtime=sv.get("runtime", ""),
            role=sv["role"],
            depends_on=tuple(sv.get("depends_on", [])),
            slo=sv.get("slo", {}),
            key_metrics=tuple(sv.get("key_metrics", [])),
            faults=faults,
            notes=sv.get("notes", ""),
        )

    teams = {
        name: Team(name=name, pager=t["pager"], slack=t["slack"], owns=tuple(t["owns"]))
        for name, t in raw["teams"].items()
    }

    dependents: dict[str, list[str]] = {name: [] for name in services}
    for name, svc in services.items():
        for dep in svc.depends_on:
            dependents.setdefault(dep, []).append(name)

    faults = {f.flag: f for svc in services.values() for f in svc.faults}

    return Platform(
        name=p["name"],
        summary=p["summary"].strip(),
        slo_window_days=p["slo_window_days"],
        golden_signals=tuple(p["golden_signals"]),
        services=services,
        teams=teams,
        dependents={k: tuple(sorted(v)) for k, v in dependents.items()},
        faults=faults,
    )


def write(doc_type: str, stem: str, content: str, *, ext: str = "md") -> Path:
    """Write a text document into knowledge/<doc_type>/<stem>.<ext> and return the path."""
    out = KNOWLEDGE / doc_type / f"{stem}.{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content if content.endswith("\n") else content + "\n")
    return out


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
