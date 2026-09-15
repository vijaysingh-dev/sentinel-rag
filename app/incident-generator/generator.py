"""Turns real otel-demo failure modes on and off on a schedule, and writes an incident log.

This is the "the app gets into trouble on its own" half of the mock platform — the load-generator
container (already part of app/otel-demo) supplies "real users calling real services"; this
service supplies "and sometimes it breaks." Without it, sentinel's RCA agent has nothing to
investigate that wasn't scripted by a human two minutes earlier.

Mechanism: flagd watches demo.flagd.json for changes and reloads live (that's how flagd-ui's
toggle switches work too — same file, same volume mount). This service edits the same file on a
timer, so no flagd API or restart is involved, just the file every other flag-editing tool here
already uses.

incidents.jsonl is the ground truth this scenario produces: one line per state change, with the
flag, the fault variant, and a wall-clock timestamp. Session B5 in roadmap.sh correlates this
against what the health-tree rollup and the RCA agent's own findings say happened.

Capped at the last MAX_RUNS scenario-runs (a run being its incident_start/incident_end pair), not
left to grow unbounded: this file is a live status feed for the platform panel, not an audit log
that needs a real retention policy (deliberately deferred — see CLAUDE.md's Sentinel-db-by-default
rule for where durable persistence for this eventually belongs, once it does). `_trim_and_write`
re-reads the whole file, keeps only the newest MAX_RUNS runs, and writes it back — cheap at this
size and simpler than an on-disk ring buffer.

Triggering is manual-only (the chat UI's platform panel calls POST /trigger/{name}) — the random
scheduler that used to fire scenarios on its own is disabled below. Automatic incidents made a
question like "why did checkout just fail" ambiguous about whether it was cause or coincidence
while someone was actively driving a scenario from the UI; a human (or a future scripted caller)
deciding when a fault starts keeps ground truth unambiguous.

No SSE stream here, unlike system-monitor: this service's state only ever changes in direct
response to a request this same panel already made (POST /trigger). There's no background loop
that could push a change the client didn't cause itself, so a snapshot fetched on open and again
after each trigger tells the client everything a push channel would.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException

SCENARIOS_PATH = Path(__file__).parent / "scenarios.yaml"
FLAGD_CONFIG_PATH = Path("/etc/flagd/demo.flagd.json")
INCIDENT_LOG_PATH = Path("/app/data/incidents.jsonl")
MAX_RUNS = 5  # scenario-runs kept, not lines — each run is an incident_start/incident_end pair

_lock = threading.Lock()


def load_scenarios() -> dict[str, dict[str, Any]]:
    spec = yaml.safe_load(SCENARIOS_PATH.read_text())
    return {s["name"]: s for s in spec["scenarios"]}


SCENARIOS = load_scenarios()


def read_flags() -> dict[str, Any]:
    return json.loads(FLAGD_CONFIG_PATH.read_text())


def write_flags(data: dict[str, Any]) -> None:
    # flagd's file-source watcher reacts to the file being replaced, so write-then-rename instead
    # of editing in place — a reader should never see a half-written JSON document.
    tmp = FLAGD_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(FLAGD_CONFIG_PATH)


def read_log() -> list[dict[str, Any]]:
    if not INCIDENT_LOG_PATH.exists():
        return []
    lines = INCIDENT_LOG_PATH.read_text().strip().splitlines()
    return [json.loads(line) for line in lines if line]


def _pair_runs(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Walks oldest-to-newest pairing each incident_start with the incident_end that follows it
    for the same scenario, so a run is never split apart. A dangling incident_start with no end
    yet (the active run) is still returned as a one-record run.
    """
    runs: list[list[dict[str, Any]]] = []
    open_runs: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        name = record["scenario"]
        if record["event"] == "incident_start":
            open_runs[name] = [record]
            runs.append(open_runs[name])
        elif name in open_runs:
            open_runs[name].append(record)
            del open_runs[name]
    return runs


def _trim_and_write(records: list[dict[str, Any]]) -> None:
    """Keeps only the newest MAX_RUNS scenario-runs, oldest run dropped first — trimming drops
    whole runs, not individual lines. A dangling incident_start with no end yet (the active run)
    always counts as the newest run and is never dropped.
    """
    trimmed = _pair_runs(records)[-MAX_RUNS:]
    lines = [json.dumps(r) for run in trimmed for r in run]
    tmp = INCIDENT_LOG_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
    tmp.replace(INCIDENT_LOG_PATH)


def log_event(event: str, scenario: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ts": time.time(),
        "event": event,
        "scenario": scenario["name"],
        "flag": scenario["flag"],
        "fault_variant": scenario["fault_variant"],
        "note": scenario.get("note", ""),
    }
    _trim_and_write([*read_log(), record])
    print(f"incident-generator: {event} {scenario['name']} ({scenario['flag']}={scenario['fault_variant']})")
    return record


def set_flag(flag: str, variant: str) -> None:
    with _lock:
        data = read_flags()
        data["flags"][flag]["defaultVariant"] = variant
        write_flags(data)


def reset_all_flags() -> None:
    """Force every scenario's flag back to its resting variant.

    Runs once at startup. A crash or `docker compose up --build` mid-incident kills this
    process's in-memory `state.active` along with the asyncio sleep that was going to turn the
    flag back off — the fault would otherwise stay stuck on indefinitely with no record of why,
    which is worse than the incident itself. Reset targets come from scenarios.yaml, not from
    whatever the file currently says, because "read the current value and restore it later" is
    exactly the fragile pattern that produced this bug in the first place.
    """
    with _lock:
        data = read_flags()
        changed = []
        for scenario in SCENARIOS.values():
            flag, resting = scenario["flag"], scenario["reset_variant"]
            if data["flags"][flag]["defaultVariant"] != resting:
                data["flags"][flag]["defaultVariant"] = resting
                changed.append(flag)
        if changed:
            write_flags(data)
            print(f"incident-generator: reset stuck flag(s) on startup: {changed}")


class State:
    def __init__(self) -> None:
        self.active: dict[str, Any] | None = None


state = State()


def snapshot() -> dict[str, Any]:
    """`recent` reads from incidents.jsonl, not an in-memory list — the file is what survives a
    restart (`MAX_RUNS`-bounded, see `_trim_and_write`), so this is what makes the platform
    panel's "Recent incidents" section restart-safe instead of resetting to empty every time this
    container restarts.

    Each entry pairs a run's start and end record into one item with both timestamps, rather than
    listing end events alone — so a finished run's exact timeframe is visible instead of just its
    end time. The still-running run is excluded here (it's already surfaced via `active`) and only
    joins `recent` once it ends.
    """
    runs = [run for run in _pair_runs(read_log()) if len(run) == 2]
    recent = [
        {
            "scenario": run[0]["scenario"],
            "flag": run[0]["flag"],
            "fault_variant": run[0]["fault_variant"],
            "note": run[0]["note"],
            "start_ts": run[0]["ts"],
            "end_ts": run[1]["ts"] if len(run) == 2 else None,
        }
        for run in runs
    ]
    return {"active": state.active, "recent": recent[-10:]}


async def run_scenario(scenario: dict[str, Any]) -> None:
    set_flag(scenario["flag"], scenario["fault_variant"])
    record = log_event("incident_start", scenario)
    state.active = record
    await asyncio.sleep(scenario["duration_s"])
    set_flag(scenario["flag"], scenario["reset_variant"])
    log_event("incident_end", scenario)
    state.active = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    reset_all_flags()
    yield


app = FastAPI(title="incident-generator", lifespan=lifespan)


@app.get("/scenarios")
def list_scenarios() -> dict[str, Any]:
    return {"scenarios": list(SCENARIOS.values())}


@app.get("/status")
def status() -> dict[str, Any]:
    return snapshot()


@app.post("/trigger/{name}")
async def trigger(name: str) -> dict[str, Any]:
    """Manual override for session B2 — skip the schedule and fire one scenario now."""
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario: {name}")
    if state.active is not None:
        raise HTTPException(409, f"scenario already running: {state.active['scenario']}")
    asyncio.create_task(run_scenario(SCENARIOS[name]))
    return {"triggered": name}


@app.get("/incidents")
def incidents(limit: int = 50) -> dict[str, Any]:
    return {"incidents": read_log()[-limit:]}
