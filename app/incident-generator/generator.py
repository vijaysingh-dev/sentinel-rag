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


def log_event(event: str, scenario: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ts": time.time(),
        "event": event,
        "scenario": scenario["name"],
        "flag": scenario["flag"],
        "fault_variant": scenario["fault_variant"],
        "note": scenario.get("note", ""),
    }
    with INCIDENT_LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
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
        self.history: list[dict[str, Any]] = []


state = State()


def snapshot() -> dict[str, Any]:
    return {"active": state.active, "recent": state.history[-10:]}


async def run_scenario(scenario: dict[str, Any]) -> None:
    set_flag(scenario["flag"], scenario["fault_variant"])
    record = log_event("incident_start", scenario)
    state.active = record
    await asyncio.sleep(scenario["duration_s"])
    set_flag(scenario["flag"], scenario["reset_variant"])
    record = log_event("incident_end", scenario)
    state.history.append(record)
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
    if not INCIDENT_LOG_PATH.exists():
        return {"incidents": []}
    lines = INCIDENT_LOG_PATH.read_text().strip().splitlines()
    return {"incidents": [json.loads(line) for line in lines[-limit:]]}
