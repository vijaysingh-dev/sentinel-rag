"""Parent -> child health rollup for the otel-demo platform.

Why read Docker's own healthcheck state instead of polling each service's health endpoint
ourselves: the demo's services speak a mix of protocols (gRPC health checks, plain HTTP, a TCP
probe for flagd-ui) and compose.yaml already runs the correct check for each one. Reimplementing
that per-service would just be a worse copy of what Docker already computes. The rollup logic —
the actual point of this service — sits one layer above that: Docker only ever tells you about one
container, never about the tree.

Two nodes (redis, astronomy-db) are not Docker containers this project manages — they're the
shared, always-on infra containers described in sentinel-rag/CLAUDE.md's infra-layer section, and
a Docker healthcheck lookup on them would only answer "is the container up", not "can
product-catalog actually run a query against it right now". Those two get a real connection
attempt instead (see check_redis/check_postgres) — tree.yaml marks them `check: ping`.

Rollup rule: a node's status is the worst of its own health and every child's rollup status,
computed bottom-up from tree.yaml's leaves. A single unhealthy leaf is therefore visible at every
ancestor up to the root, which is the whole reason this exists — "is checkout okay" should already
answer "no" when the answer is really "payment is down and checkout depends on it".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import docker
import redis.asyncio as redis
import yaml
from fastapi import FastAPI, HTTPException, Request
from psycopg import AsyncConnection
from sse_starlette.sse import EventSourceResponse

TREE_PATH = Path(__file__).parent / "tree.yaml"
POLL_INTERVAL_S = 5
HEARTBEAT_S = 15  # keeps the SSE connection alive through proxies/load balancers that time out idle streams
PING_TIMEOUT_S = 3  # a hung connection attempt must not stall the whole refresh cycle

# Same shared containers cart and product-catalog themselves connect to (see .env.override) —
# reached over the `infra` Docker network by container name, not localhost.
REDIS_URL = os.environ.get("MONITOR_REDIS_URL", "redis://redis:6379/1")
POSTGRES_DSN = os.environ.get(
    "MONITOR_POSTGRES_DSN", "postgresql://astronomy_user:astronomy_password@postgres:5432/astronomy_db"
)

# Worst-first ordering used to fold a node's own health together with its children's rollups.
SEVERITY = {"healthy": 0, "starting": 1, "missing": 2, "unhealthy": 3}


def worse(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


class Tree:
    def __init__(self, path: Path) -> None:
        spec = yaml.safe_load(path.read_text())
        self.roots: list[str] = spec["roots"]
        self.nodes: dict[str, list[str]] = {name: (n.get("depends_on") or []) for name, n in spec["nodes"].items()}
        self.display_names: dict[str, str] = spec.get("display_names", {})
        # A node's Docker lookup name defaults to its own name — `container:` overrides it for the
        # one node (astroshop) whose tree.yaml name isn't what Docker calls the container.
        self._containers: dict[str, str] = {name: n.get("container", name) for name, n in spec["nodes"].items()}
        self._checks: dict[str, str] = {name: n.get("check", "docker") for name, n in spec["nodes"].items()}

    def display_name(self, name: str) -> str:
        return self.display_names.get(name, name)

    def container(self, name: str) -> str:
        return self._containers[name]

    def check_kind(self, name: str) -> str:
        return self._checks[name]


TREE = Tree(TREE_PATH)


def own_health(client: docker.DockerClient, container_name: str) -> str:
    """Health of one container, independent of its children.

    Every tracked container now declares a real compose healthcheck (see compose.yaml) — there is
    no more "no healthcheck, assume healthy" fallback. That fallback used to mean "running" and
    "healthy" were indistinguishable for a wedged-but-still-running process; requiring a real
    check on every node is what makes green mean something.
    """
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        return "missing"

    if container.status != "running":
        return "unhealthy" if container.status in ("exited", "dead", "restarting") else "missing"

    health = container.attrs.get("State", {}).get("Health", {})
    status = health.get("Status")
    if status == "healthy":
        return "healthy"
    if status == "starting":
        return "starting"
    if status == "unhealthy":
        return "unhealthy"
    # No Health block at all means compose has no healthcheck for this container — a tree.yaml /
    # compose.yaml mismatch, not a transient state. Treat it as unhealthy rather than silently
    # reporting green on a container nobody is actually checking.
    return "unhealthy"


async def check_redis() -> str:
    try:
        client = redis.from_url(REDIS_URL, socket_connect_timeout=PING_TIMEOUT_S, socket_timeout=PING_TIMEOUT_S)
        try:
            await client.ping()
            return "healthy"
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 - any connection failure means "can't reach it", not a crash
        return "unhealthy"


async def check_postgres() -> str:
    try:
        conn = await asyncio.wait_for(AsyncConnection.connect(POSTGRES_DSN), timeout=PING_TIMEOUT_S)
        async with conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
        return "healthy"
    except Exception:  # noqa: BLE001 - any connection/query failure means "can't reach it"
        return "unhealthy"


class Monitor:
    def __init__(self) -> None:
        self.client = docker.from_env()
        self.snapshot: dict[str, dict[str, Any]] = {}
        self.computed_at: float = 0.0
        # Docker is still polled every POLL_INTERVAL_S — there's no push API for container health
        # — but subscribers are only woken (and only re-fetch) when the rollup actually changes,
        # not on every poll tick. That's what keeps five browser tabs from meaning five idle
        # SSE frames a second even though nothing on the platform moved.
        self._subscribers: set[asyncio.Queue[None]] = set()

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    def _notify(self) -> None:
        for queue in self._subscribers:
            if queue.empty():
                queue.put_nowait(None)

    async def refresh(self) -> None:
        docker_nodes = [name for name in TREE.nodes if TREE.check_kind(name) == "docker"]
        own: dict[str, str] = {name: own_health(self.client, TREE.container(name)) for name in docker_nodes}

        # The two ping checks run concurrently with each other (they're independent I/O, no reason
        # to serialize) but Docker's own lookups above are synchronous — cheap local socket calls,
        # not worth the complexity of a thread pool just to parallelize them too.
        if "redis" in TREE.nodes:
            own["redis"] = await check_redis()
        if "astronomy-db" in TREE.nodes:
            own["astronomy-db"] = await check_postgres()

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

        new_snapshot = {
            name: {"own": own[name], "status": rollup[name], "depends_on": TREE.nodes[name]} for name in TREE.nodes
        }
        changed = new_snapshot != self.snapshot
        self.snapshot = new_snapshot
        self.computed_at = time.time()
        if changed:
            self._notify()

    async def poll_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001 - keep polling regardless of a transient error
                print(f"system-monitor: refresh failed: {exc}")
            await asyncio.sleep(POLL_INTERVAL_S)


monitor = Monitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await monitor.refresh()
    task = asyncio.create_task(monitor.poll_forever())
    yield
    task.cancel()


app = FastAPI(title="system-monitor", lifespan=lifespan)


def _subtree(name: str) -> dict[str, Any]:
    node = monitor.snapshot[name]
    return {
        "name": name,
        "display_name": TREE.display_name(name),
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
    return {
        "computed_at": monitor.computed_at,
        "nodes": {name: {"display_name": TREE.display_name(name), **data} for name, data in monitor.snapshot.items()},
    }


@app.get("/nodes/{name}")
def node(name: str) -> dict[str, Any]:
    if name not in monitor.snapshot:
        raise HTTPException(404, f"unknown node: {name}")
    return {"name": name, "display_name": TREE.display_name(name), **monitor.snapshot[name]}


@app.get("/stream")
async def stream(request: Request) -> EventSourceResponse:
    """Server-sent events: the same payload as `/tree`, pushed on every rollup change.

    SSE over WebSocket because this is one-directional (server -> browser) and needs nothing
    WebSocket adds — framing for client messages, ping/pong, a fallback for proxies that don't
    speak the upgrade handshake. A plain HTTP response that never ends is all a health rollup
    the client only ever reads needs.
    """

    async def events():
        queue = monitor.subscribe()
        try:
            yield {"event": "tree", "data": json.dumps(tree())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                    yield {"event": "tree", "data": json.dumps(tree())}
                except TimeoutError:
                    yield {"event": "heartbeat", "data": ""}
        finally:
            monitor.unsubscribe(queue)

    return EventSourceResponse(events())
