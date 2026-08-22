"""Parent -> child health rollup for the otel-demo platform.

Why read Docker's own healthcheck state instead of polling each service's health endpoint
ourselves: the demo's services speak a mix of protocols (gRPC health checks, plain HTTP, a TCP
probe for flagd-ui) and compose.yaml already runs the correct check for each one. Reimplementing
that per-service would just be a worse copy of what Docker already computes. The rollup logic —
the actual point of this service — sits one layer above that: Docker only ever tells you about one
container, never about the tree.

Rollup rule: a node's status is the worst of its own health and every child's rollup status,
computed bottom-up from tree.yaml's leaves. A single unhealthy leaf is therefore visible at every
ancestor up to the root, which is the whole reason this exists — "is checkout okay" should already
answer "no" when the answer is really "payment is down and checkout depends on it".
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import docker
import yaml
from fastapi import FastAPI, HTTPException

TREE_PATH = Path(__file__).parent / "tree.yaml"
POLL_INTERVAL_S = 5

# Worst-first ordering used to fold a node's own health together with its children's rollups.
SEVERITY = {"healthy": 0, "starting": 1, "missing": 2, "unhealthy": 3}


def worse(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


class Tree:
    def __init__(self, path: Path) -> None:
        spec = yaml.safe_load(path.read_text())
        self.roots: list[str] = spec["roots"]
        self.nodes: dict[str, list[str]] = {name: (n.get("depends_on") or []) for name, n in spec["nodes"].items()}
        self.no_healthcheck: set[str] = set(spec.get("no_healthcheck", []))


TREE = Tree(TREE_PATH)


def own_health(client: docker.DockerClient, name: str) -> str:
    """Health of one container, independent of its children."""
    try:
        container = client.containers.get(name)
    except docker.errors.NotFound:
        return "missing"

    if container.status != "running":
        return "unhealthy" if container.status in ("exited", "dead", "restarting") else "missing"

    if name in TREE.no_healthcheck:
        # No healthcheck defined in compose.yaml for this service — "running" is the only signal
        # Docker gives us. A wedged-but-running process reads as healthy here; see tree.yaml.
        return "healthy"

    health = container.attrs.get("State", {}).get("Health", {})
    status = health.get("Status")
    if status == "healthy":
        return "healthy"
    if status == "starting":
        return "starting"
    if status == "unhealthy":
        return "unhealthy"
    # Healthcheck expected but not yet reported (e.g. container just created).
    return "starting"


class Monitor:
    def __init__(self) -> None:
        self.client = docker.from_env()
        self.snapshot: dict[str, dict[str, Any]] = {}
        self.computed_at: float = 0.0

    def refresh(self) -> None:
        own = {name: own_health(self.client, name) for name in TREE.nodes}
        rollup: dict[str, str] = {}

        def resolve(name: str) -> str:
            if name in rollup:
                return rollup[name]
            status = own[name]
            for child in TREE.nodes[name]:
                status = worse(status, resolve(child))
            rollup[name] = status
            return status

        for name in TREE.nodes:
            resolve(name)

        self.snapshot = {
            name: {"own": own[name], "status": rollup[name], "depends_on": TREE.nodes[name]} for name in TREE.nodes
        }
        self.computed_at = time.time()

    async def poll_forever(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - keep polling regardless of a transient docker error
                print(f"system-monitor: refresh failed: {exc}")
            await asyncio.sleep(POLL_INTERVAL_S)


monitor = Monitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.refresh()
    task = asyncio.create_task(monitor.poll_forever())
    yield
    task.cancel()


app = FastAPI(title="system-monitor", lifespan=lifespan)


def _subtree(name: str) -> dict[str, Any]:
    node = monitor.snapshot[name]
    return {
        "name": name,
        "own": node["own"],
        "status": node["status"],
        "children": [_subtree(child) for child in node["depends_on"]],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """Root-level rollup — what most callers actually want ("is the platform up")."""
    return {
        "computed_at": monitor.computed_at,
        "roots": {root: monitor.snapshot[root]["status"] for root in TREE.roots},
    }


@app.get("/tree")
def tree() -> dict[str, Any]:
    """Full nested rollup, root down to leaves, for the tree view / debugging."""
    return {"computed_at": monitor.computed_at, "trees": [_subtree(root) for root in TREE.roots]}


@app.get("/nodes")
def nodes() -> dict[str, Any]:
    """Flat view of every tracked node — easiest shape for another tool to poll."""
    return {"computed_at": monitor.computed_at, "nodes": monitor.snapshot}


@app.get("/nodes/{name}")
def node(name: str) -> dict[str, Any]:
    if name not in monitor.snapshot:
        raise HTTPException(404, f"unknown node: {name}")
    return {"name": name, **monitor.snapshot[name]}
