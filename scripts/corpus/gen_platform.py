"""Generate the platform reference: what Astroshop *is*, as opposed to how it fails or who to page.

    uv run python scripts/corpus/gen_platform.py

Why this exists: every other generator writes a document *about an operational event or duty* —
a runbook, a post-mortem, an ADR, an escalation path. None of them answers the plain questions a
newcomer asks first: "what services exist?", "how many are there?", "what port is cart on?",
"where does Jaeger run?", "what does the incident-generator actually do?". Those facts were
scattered across compose files, YAML comments and Makefile echo lines, none of which is in
`knowledge/`, so the retriever had nothing to match and the chat correctly said so.

Output is ``knowledge/platform/*.md`` — doc_type ``platform``, chunked ``structural``. Each
document is written so every `##` section stands on its own: the section heading names the
subject ("cart", "Port map") because the structural chunker prefixes the heading path onto each
chunk, and a chunk that says "it listens on 7070" without saying what "it" is cannot be found.

Sources, all read at generation time rather than copied here so they cannot drift:

* ``scripts/corpus/services.yaml`` — services, teams, SLOs, faults, and the `deployment:` /
  `components:` sections that record how `make demo-up` actually runs each piece;
* ``app/otel-demo/src/flagd/demo.flagd.json`` — every feature flag and its variants;
* ``app/incident-generator/scenarios.yaml`` — the named fault scenarios;
* ``app/system-monitor/tree.yaml`` — the health-rollup tree.

The alternative was one long "platform.md". Rejected: separate documents give each topic its own
title, which the structural chunker also prefixes, so "System monitor > Endpoints" and "Incident
generator > Endpoints" stay distinguishable instead of both reading as "Platform > Endpoints".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from _model import REPO_ROOT, Platform, load, rel, write

sys.path.insert(0, str(Path(__file__).parent))

FLAGD_JSON = REPO_ROOT / "app" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
SCENARIOS_YAML = REPO_ROOT / "app" / "incident-generator" / "scenarios.yaml"
TREE_YAML = REPO_ROOT / "app" / "system-monitor" / "tree.yaml"

DOC_TYPE = "platform"

TIER_NAME = {0: "tier 0 (customer-facing critical path)", 1: "tier 1 (supporting)", 2: "tier 2 (async / back office)"}

# Flags that exist in demo.flagd.json but control the traffic harness rather than injecting a fault.
TRAFFIC_FLAGS = {"loadGeneratorTraffic", "loadGeneratorVUs"}


def _callers(p: Platform, name: str) -> list[str]:
    return list(p.dependents.get(name, ()))


def _slo_text(slo: dict) -> str:
    parts = []
    for k, v in slo.items():
        label = k.replace("_", " ").replace("latency p95 ms", "p95 latency").replace("latency p99 ms", "p99 latency")
        parts.append(f"{label} {v}{' ms' if k.endswith('_ms') else ''}{' s' if k.endswith('_s') else ''}")
    return ", ".join(parts)


def _deployed(p: Platform) -> list[str]:
    return [n for n in p.services if p.deployment[n].deployed]


def _not_deployed(p: Platform) -> list[str]:
    return [n for n in p.services if not p.deployment[n].deployed]


# --------------------------------------------------------------------------------- overview
def _overview(p: Platform) -> str:
    deployed = _deployed(p)
    absent = _not_deployed(p)
    business = [n for n in p.services if n not in ("otel-collector", "flagd")]
    langs = sorted({s.language for s in p.services.values()})

    L = [f"# {p.name} platform overview", ""]
    L += [
        f"{p.summary}",
        "",
        f"{p.name} is the OpenTelemetry Demo, run locally with Docker Compose. It is the system "
        "Sentinel investigates: Sentinel reads its metrics (Prometheus), traces (Jaeger), feature "
        "flags (flagd), health rollup (system-monitor) and incident log (incident-generator).",
        "",
        "## How many services are there",
        "",
        f"The platform model defines **{len(p.services)} services**: {len(business)} business "
        f"services and data stores, plus 2 platform services (otel-collector and flagd) that every other service uses. "
        f"Under `make demo-up`, **{len(deployed)} of them run** and {len(absent)} do not "
        f"({', '.join(absent)}) — see \"What is not running\" below.",
        "",
        f"Alongside the services run {len(p.components)} supporting components: the load generator, "
        "the flag UI, the observability backends (Jaeger, Prometheus, Grafana, OpenSearch, OpAMP "
        "server), Sentinel's two sidecars (system-monitor, incident-generator) and the shared "
        "infrastructure (postgres, redis). They are listed in the Platform components document.",
        "",
        f"Services are written in {len(langs)} languages/runtimes: {', '.join(langs)}. That polyglot "
        "mix is deliberate upstream — it shows OpenTelemetry instrumentation in every major ecosystem.",
        "",
        "## What services exist",
        "",
        "| Service | What it does | Tier | Team | Language | Running here |",
        "|---|---|---|---|---|---|",
    ]
    for n, s in p.services.items():
        run = "yes" if p.deployment[n].deployed else "no"
        L.append(f"| {n} | {s.role} | {s.tier} | {s.team} | {s.language} | {run} |")
    L.append("")

    L += ["## Services by tier", ""]
    for tier in (0, 1, 2):
        names = [n for n, s in p.services.items() if s.tier == tier]
        L.append(f"- **{TIER_NAME[tier]}**: {', '.join(names)}.")
    L += [
        "",
        "Tier 0 services are on the path a customer's purchase takes; if any of them fails, "
        "customers cannot browse or buy. Tier 1 services support that path but a failure is "
        "usually a degradation (slow shipping quotes, an empty ad slot, missing recommendations). "
        "Tier 2 services are asynchronous or best-effort: a failure there does not stop an order.",
        "",
        "## Services by owning team",
        "",
    ]
    for t in p.teams.values():
        L.append(f"- **{t.name}** (pager {t.pager}, Slack {t.slack}): {', '.join(t.owns)}.")
    L += [
        "",
        "## What a customer does on the platform",
        "",
        "1. **Browse** — the browser loads the storefront from frontend (through frontend-proxy). "
        "frontend calls product-catalog for products, currency to convert prices, recommendation "
        "for \"you may also like\", ad for a contextual ad, and image-provider for product photos.",
        "2. **Add to cart** — frontend calls cart, which stores the cart in Redis.",
        "3. **Check out** — frontend calls checkout, which runs the synchronous order pipeline: "
        "get the cart, look up each product, convert currency, get a shipping quote (shipping "
        "calls quote), charge the card (payment), book shipping, empty the cart, send the "
        "confirmation email (email), then publish the order to Kafka, where accounting records "
        "it and fraud-detection scores it.",
        "",
        "Customers here are synthetic: the load-generator (k6) runs this journey continuously.",
        "",
        "## What is not running",
        "",
    ]
    for n in absent:
        L.append(f"- **{n}** — {p.deployment[n].note}")
    L += [
        "",
        "Both are replaced rather than missing: cart and product data are real, they live in the "
        "shared redis and postgres containers.",
        "",
        "## Where to go next",
        "",
        "- Service catalog — one section per service: ports, protocol, health check, SLO, metrics, faults.",
        "- Service dependencies and request flows — who calls whom, and what breaks when something does.",
        "- Infrastructure and deployment — Docker Compose layout, networks, ports, make targets.",
        "- Observability stack — OpenTelemetry collector, Prometheus, Jaeger, OpenSearch, Grafana.",
        "- Failure modes and fault injection — every flagd flag and what it breaks.",
        "- Incident generator and System monitor — Sentinel's two sidecar services.",
        "- Platform FAQ — short answers to the common questions.",
    ]
    return "\n".join(L)


# --------------------------------------------------------------------------- service catalog
def _catalog(p: Platform) -> str:
    L = [f"# {p.name} service catalog", ""]
    L += [
        "One section per service. Each lists what the service does, how it is built, where it "
        "listens, how Docker health-checks it, who owns it, what it depends on and what depends "
        "on it, its SLO, the metrics to watch, and the failure modes that can be injected into it.",
        "",
    ]
    for n, s in p.services.items():
        d = p.deployment[n]
        runtime = f"{s.language} ({s.runtime})" if s.runtime and s.runtime != s.language else s.language
        callers = _callers(p, n)
        radius = p.blast_radius(n)
        team = p.team_for(n)
        L += [f"## {n}", ""]
        L.append(f"**{n}** — {s.role}")
        L.append("")
        L.append(f"- Tier: {TIER_NAME[s.tier]}")
        L.append(f"- Owning team: {team.name} (pager {team.pager}, Slack {team.slack})")
        L.append(f"- Language / runtime: {runtime}")
        if d.deployed:
            port = f"port {d.port}, " if d.port else ""
            L.append(f"- Running here: yes — {port}{d.protocol}, memory limit {d.memory_limit}")
            L.append(f"- Docker healthcheck: {d.healthcheck}")
        else:
            L.append(f"- Running here: no — {d.note}")
        L.append(f"- Depends on: {', '.join(s.depends_on) if s.depends_on else 'nothing (leaf service)'}")
        L.append(f"- Called by: {', '.join(callers) if callers else 'no other business service'}")
        if radius:
            L.append(f"- Blast radius if it fails: {', '.join(radius)}")
        L.append(f"- SLO: {_slo_text(s.slo)} over {p.slo_window_days} days")
        L.append(f"- Key metrics: {', '.join(s.key_metrics)}")
        if s.faults:
            L.append(f"- Injectable faults: {', '.join(f.flag for f in s.faults)}")
        else:
            L.append("- Injectable faults: none of its own; failures here come from dependencies or infrastructure")
        if d.deployed and d.note:
            L.append(f"- Deployment note: {d.note}")
        if s.notes:
            L += ["", s.notes.strip()]
        for f in s.faults:
            L += ["", f"**{n} fault `{f.flag}`** — {f.effect} What it looks like: {f.signature}"]
        L.append("")

    L += [
        "## cart health check versus cart errors",
        "",
        "cart declares `grpc_health_probe` on :7070. That probe answers the gRPC health RPC, not "
        "AddItem or GetCart, so under cartFailure the container stays healthy while real calls "
        "fail — cart's error ratio is the signal, not its container health. A hung process does "
        "fail the probe and turns red in system-monitor, but is never restarted automatically: "
        "Docker does not restart on a failing healthcheck and the restart policy is `no`.",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------------------- dependencies
def _dependencies(p: Platform) -> str:
    L = [f"# {p.name} service dependencies and request flows", ""]
    L += [
        "How the services call each other, which calls are synchronous, and what fails "
        "downstream of each service. Edges are business dependencies only: otel-collector and "
        "flagd are used by every service and are left out, because including them would make "
        "every service look like it depends on everything.",
        "",
        "## Call graph (who calls whom)",
        "",
        "| Service | Calls | Called by |",
        "|---|---|---|",
    ]
    for n, s in p.services.items():
        L.append(f"| {n} | {', '.join(s.depends_on) or '-'} | {', '.join(_callers(p, n)) or '-'} |")
    L += [
        "",
        "## Entry point",
        "",
        "All external traffic enters through **frontend-proxy** (Envoy) on http://localhost:8080. "
        "The proxy routes `/` to frontend, `/images/` to image-provider, `/jaeger/` to Jaeger, "
        "`/grafana/` to Grafana, `/feature` to flagd-ui, `/telemetry/` to telemetry-docs, "
        "`/flagservice/` to flagd (for in-browser flag evaluation) and `/otlp-http/` to the "
        "collector (so browser-side spans reach the same trace).",
        "",
        "## Page load flow",
        "",
        "Browser → frontend-proxy → frontend. frontend then calls, per page: product-catalog "
        "(ListProducts / GetProduct), currency (price conversion), recommendation (which itself "
        "calls product-catalog), ad (GetAds), cart (item count), and image-provider serves the "
        "photos. A slow or failing call on this fan-out shows up as frontend latency or a partial "
        "page; ad failing leaves the ad slot empty rather than breaking the page.",
        "",
        "## Checkout flow",
        "",
        "Browser → frontend-proxy → frontend → **checkout.PlaceOrder**, which calls, in order:",
        "",
        "1. cart — GetCart",
        "2. product-catalog — GetProduct for each item, and currency — Convert each item's price",
        "3. shipping — GetQuote (shipping calls quote to price it), then currency converts the quote",
        "4. payment — Charge the card",
        "5. shipping — ShipOrder",
        "6. cart — EmptyCart (its error is ignored; the order is already charged)",
        "7. email — SendOrderConfirmation (best-effort; failure does not fail the order)",
        "8. kafka — publish to the `orders` topic",
        "",
        "Steps 1-5 are synchronous and on the critical path, so an error in cart, "
        "product-catalog, currency, shipping, quote or payment surfaces as a checkout error. "
        "checkout has no fault flag of its own: when an alert fires on checkout, the cause is "
        "almost always one of these downstream calls.",
        "",
        "## Asynchronous flow",
        "",
        "checkout produces each placed order to Kafka topic `orders` (a single KRaft broker). "
        "accounting consumes it and writes the order into astronomy_db; fraud-detection consumes "
        "it and scores the order. Both are consumers, so a failure there shows as consumer lag, "
        "never as a customer-facing error — but a Kafka broker that stops accepting writes slows "
        "or fails checkout's produce call, which *is* on the order path.",
        "",
        "## Data stores",
        "",
        "- cart → Redis (the shared `redis` container, logical DB 1). Upstream calls it valkey-cart.",
        "- product-catalog → PostgreSQL (the `astronomy_db` database in the shared `postgres` "
        "container). Upstream calls it astronomy-db.",
        "- recommendation keeps an in-process cache only.",
        "",
        "## Blast radius per service",
        "",
        "What else degrades when a service fails (transitive callers, nearest first), and which "
        "of those are tier-0.",
        "",
        "| Service | Blast radius | Tier-0 services affected |",
        "|---|---|---|",
    ]
    for n in p.services:
        br = p.blast_radius(n)
        crit = p.critical_path_hits(n)
        L.append(f"| {n} | {', '.join(br) or 'none'} | {', '.join(crit) or 'none'} |")
    L += [
        "",
        "## Shared dependencies",
        "",
        "- **product-catalog** is called by frontend, checkout and recommendation — the most shared "
        "service; a product-catalog failure shows up in three places at once.",
        "- **cart** is called by frontend and checkout.",
        "- **currency** is called by frontend and checkout.",
        "- **postgres** and **redis** are shared infrastructure: Sentinel itself also uses them, so "
        "an outage there affects both Astroshop and Sentinel.",
        "",
        "## Call graph versus health tree",
        "",
        "The system-monitor health tree is *not* the call graph. It nests each service under the "
        "one caller that most conceptually owns it (payment under checkout, ad under frontend), "
        "and puts shared services (cart, product-catalog) at the top level under astroshop so each "
        "appears exactly once. Use the call graph above to reason about causes; use the health "
        "tree to see at a glance which branch is red.",
    ]
    return "\n".join(L)


# ------------------------------------------------------------------------------ infra
def _infra(p: Platform) -> str:
    L = [f"# {p.name} infrastructure and deployment", ""]
    L += [
        "How the platform is deployed on the local machine: Docker Compose layering, networks, "
        "shared databases, host ports, and the make targets that start and stop it.",
        "",
        "## Docker Compose layout",
        "",
        "`make demo-up` starts one Compose project named **astroshop** from four files layered in order:",
        "",
        "1. `app/otel-demo/compose.yaml` — the business services, flagd, flagd-ui, load-generator, "
        "telemetry-docs and the OpenTelemetry collector.",
        "2. `app/otel-demo/compose.full.yaml` — Kafka, accounting and fraud-detection, and checkout's "
        "Kafka producer settings.",
        "3. `app/otel-demo/compose.observability.yaml` — Jaeger, Prometheus, Grafana, OpenSearch and "
        "the OpAMP server, plus the collector config that exports to them. Must come after the "
        "full layer: its collector command lists otelcol-config-full.yml ahead of its own.",
        "4. `app/otel-demo/compose.extras.yaml` — Sentinel's changes: disables astronomy-db and "
        "valkey-cart, points product-catalog, cart and accounting at the shared infra containers, "
        "publishes Jaeger on a fixed port, overrides bind mounts to a runtime staging directory, "
        "and adds system-monitor and incident-generator.",
        "",
        "Env files: `app/otel-demo/.env` (upstream ports and images, IMAGE_VERSION 3.0.0) then "
        "`app/otel-demo/.env.override` (POSTGRES_HOST=postgres, VALKEY_ADDR=redis:6379,defaultDatabase=1, "
        "LLM settings for upstream's optional agent stack).",
        "",
        "Not layered in: `compose.profiling.yaml`, `compose.agent.yaml` and `compose.tests.yaml`.",
        "",
        "## The shared infra layer",
        "",
        "postgres and redis are **not** part of any Compose project. They are standalone, always-on "
        "containers (`docker run --restart unless-stopped`) on a Docker network called `infra`, "
        "started once and left running — the same way Portainer is. Idle, the two cost about 57MB "
        "together, too little to be worth starting and stopping per project. Everything that "
        "costs something idle (the ~25 Astroshop containers, Sentinel's Langfuse) is started and "
        "stopped manually.",
        "",
        "- **postgres** (pgvector/pgvector:pg16, host port 5432): databases `astronomy_db` "
        "(owner astronomy_user; product-catalog's product data) and `sentinel` (owner sentinelapp; "
        "Sentinel's vectors, chat sessions, agent checkpoints, Langfuse). Bootstrap user postgres/postgres.",
        "- **redis** (redis:7-alpine, host port 6379, maxmemory 512mb, allkeys-lru, no volume): "
        "DB 0 is Sentinel's cache (keys prefixed `sentinel:`), DB 1 is Astroshop's cart storage. "
        "Restarting it loses carts, nothing durable.",
        "",
        "product-catalog, cart, otel-collector (for its PostgreSQL receiver) and system-monitor "
        "join both the astroshop default network and `infra`, and reach the two by container name. "
        "Because they are outside the Compose project, `depends_on` cannot wait on them — they are "
        "simply expected to be up already. `make up` checks that and fails fast if not.",
        "",
        "## Why astronomy-db and valkey-cart are disabled",
        "",
        "Upstream ships its own Postgres (astronomy-db, 80M limit) and Valkey (valkey-cart, 20M). "
        "Running a second Postgres and a second Redis next to the shared ones was not worth it, so "
        "compose.extras.yaml puts both behind a profile (`disabled-by-sentinel-rag`) nothing "
        "activates. Compose has no directive to delete a service from an earlier file; an "
        "unrequested profile is the idiomatic way to say \"don't start this\".",
        "",
        "## Runtime staging directory",
        "",
        "The repository sits on a filesystem where chmod is a no-op (a fuseblk-style mount), so "
        "containers that drop to a non-root user cannot read files bind-mounted straight from the "
        "repo. Configs are therefore staged under `~/.local/share/sentinel-rag/runtime/` and mounted "
        "from there: flagd's demo.flagd.json, Jaeger, Grafana and Prometheus configs, product-catalog's "
        "otel-config.yml. This also keeps the incident-generator's constant edits to demo.flagd.json "
        "out of `git status`. Incident logs live in `~/.local/share/sentinel-rag/app/incident-generator-data/`.",
        "",
        "## Host ports",
        "",
        "| What | Address |",
        "|---|---|",
        "| Storefront (frontend-proxy) | http://localhost:8080 |",
        "| Envoy admin | http://localhost:10000 |",
        "| Jaeger UI | http://localhost:16686/jaeger/ui/ (also via the proxy at http://localhost:8080/jaeger/ui/) |",
        "| Grafana | http://localhost:8080/grafana/ |",
        "| Flag UI (flagd-ui) | http://localhost:8080/feature |",
        "| Telemetry docs | http://localhost:8080/telemetry/ |",
        "| Prometheus | http://localhost:9090 |",
        "| system-monitor | http://localhost:8091 |",
        "| incident-generator | http://localhost:8092 |",
        "| Sentinel chat UI | http://localhost:3000 |",
        "| Sentinel chat API | http://localhost:8093 |",
        "| Sentinel LangGraph dev server (debug only) | http://localhost:2024 |",
        "| postgres | localhost:5432 |",
        "| redis | localhost:6379 |",
        "",
        "Every other container port (cart 7070, checkout 5050, kafka 9092, and so on) is "
        "published to a random host port chosen by Docker, so the table above is the full list of "
        "stable addresses. Inside the astroshop network, services reach each other by name and "
        "container port, e.g. `cart:7070`.",
        "",
        "## Container ports and memory limits",
        "",
        "| Service | Container port | Protocol | Memory limit | Running here |",
        "|---|---|---|---|---|",
    ]
    for n in p.services:
        d = p.deployment[n]
        L.append(f"| {n} | {d.port or '-'} | {d.protocol} | {d.memory_limit} | {'yes' if d.deployed else 'no'} |")
    L += [
        "",
        "Memory limits are tight on purpose (checkout, currency, shipping and product-catalog at "
        "20M). A memory-leak fault reaches the limit and gets OOM-killed quickly, which is what "
        "makes those faults observable in minutes rather than hours.",
        "",
        "## Starting and stopping",
        "",
        "| Command | What it does |",
        "|---|---|",
        "| `make up` | Checks postgres/redis are reachable on `infra`, starts Langfuse |",
        "| `make demo-up` | Stages demo.flagd.json if missing, starts the astroshop project, waits for Prometheus |",
        "| `make otel-demo-up` | `make up`, applies the astronomy_db seed if missing, then `make demo-up` |",
        "| `make demo-status` | Prints the health rollup and the last five incident-log events |",
        "| `make demo-down` | Stops the astroshop project (postgres/redis stay up) |",
        "| `make down` | Stops Langfuse |",
        "",
        "After `make demo-up`, set `FLAGD_CONFIG_PATH` in Sentinel's `.env` to the staged "
        "demo.flagd.json so Sentinel's flag tools can read current flag state.",
        "",
        "## Restart policy",
        "",
        "Astroshop containers use Compose's default (`restart: no`): a crashed container stays "
        "down until someone restarts it, which is exactly what makes a crash visible to the health "
        "rollup. postgres, redis and Portainer use `unless-stopped`.",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------------------- observability
def _observability(p: Platform) -> str:
    L = [f"# {p.name} observability stack", ""]
    L += [
        "How telemetry is produced, collected and stored, and how Sentinel reads it.",
        "",
        "## Signal flow",
        "",
        "Every service is instrumented with an OpenTelemetry SDK and sends traces, metrics and "
        "logs over **OTLP** (OpenTelemetry Protocol) to the **otel-collector** — gRPC on 4317, "
        "HTTP on 4318. Browser spans come in through the proxy at `/otlp-http/`. The collector "
        "then exports:",
        "",
        "- traces → **Jaeger** (OTLP gRPC to jaeger:4317), and into the span-metrics connector;",
        "- metrics → **Prometheus** (OTLP HTTP push to prometheus:9090/api/v1/otlp);",
        "- logs → **OpenSearch** (index `otel-logs`);",
        "- every signal also to the collector's `debug` exporter.",
        "",
        "Grafana reads all three backends for dashboards.",
        "",
        "## The OpenTelemetry collector",
        "",
        "Besides OTLP, the collector pulls metrics itself with these receivers:",
        "",
        "- `docker_stats` — per-container CPU, memory, network and block I/O from the Docker socket;",
        "- `host_metrics` — the host's CPU, memory, disk, filesystem, network, load and processes;",
        "- `http_check/frontend-proxy` — synthetic HTTP probe of the storefront;",
        "- `nginx` — image-provider's stub status every 10s;",
        "- `postgresql` — astronomy_db statistics via the monitoring_user role;",
        "- `redis` — reads INFO from the shared `redis` container every 10s (redirected there from "
        "the disabled valkey-cart by otelcol-config-extras.yml);",
        "- `kafkametrics` — broker, topic and consumer-group metrics (including consumer lag) every 10s;",
        "- `prometheus/ad` — scrapes ad's JVM metrics on :9465 every 10s.",
        "",
        "Processors: `resource_detection` (env, docker, system), `memory_limiter` (80% limit, 25% "
        "spike), span-name normalisation, and a redaction step that deletes card CVVs, masks card "
        "numbers to their last four digits and replaces user email with a SHA-256 hash before any "
        "trace leaves the collector.",
        "",
        "The `span_metrics` connector turns every span into RED metrics "
        "(traces_span_metrics_calls_total, traces_span_metrics_duration_milliseconds), which gives "
        "one uniform rate/error/duration source across services regardless of language.",
        "",
        "The collector sits on two Docker networks (the astroshop network and `infra`), so its OTLP "
        "receivers listen on 0.0.0.0 rather than on the address `otel-collector` resolves to. Bound "
        "to that name, they listened on the `infra` address only, and every service not itself on "
        "`infra` — all but cart, product-catalog and accounting — silently lost its traces and OTLP "
        "metrics. If Jaeger lists only a handful of services, check the receivers' bind address first.",
        "",
        "The collector itself has no Docker healthcheck, and is excluded from the health rollup: "
        "if it stops, telemetry stops arriving but customers are unaffected.",
        "",
        "## Prometheus",
        "",
        "Prometheus v3 on host port 9090. Metrics are pushed to it over OTLP rather than scraped "
        "from each service; its global scrape interval is 60s to match the SDKs' export interval. "
        "Resource attributes such as service.name, service.namespace, service.version and "
        "container.name are promoted to labels. Out-of-order samples up to 30 minutes are accepted.",
        "",
        "Metric families present in this deployment (names follow OpenTelemetry semantic conventions):",
        "",
        "| Family | Examples | Source |",
        "|---|---|---|",
        "| RPC server | rpc_server_call_duration_seconds (ad, on the Java agent: "
        "rpc_server_duration_milliseconds) | gRPC services |",
        "| HTTP server | http_server_request_duration_seconds, http_server_active_requests | HTTP services |",
        "| Span metrics | traces_span_metrics_calls_total, traces_span_metrics_duration_milliseconds "
        "| collector connector |",
        "| Containers | container_cpu_utilization_ratio, container_memory_usage_total_bytes | docker_stats |",
        "| Host | system_cpu_utilization_ratio, system_memory_utilization_ratio | host_metrics |",
        "| PostgreSQL | postgresql_backends, postgresql_commits_total, postgresql_deadlocks_total "
        "| postgresql receiver |",
        "| Database client | db_client_operation_duration_seconds, db_sql_connection_open | product-catalog |",
        "| Cart | demo_cart_add_item_latency_seconds, demo_cart_get_cart_latency_seconds | cart |",
        "| .NET / Go runtimes | dotnet_gc_collections_total, go_goroutine_count | cart, Go services |",
        "| Process | process_memory_usage_bytes, process_cpu_time_seconds_total | SDK process metrics |",
        "| nginx | nginx_requests_total | image-provider |",
        "| Redis | redis_memory_used_bytes, redis_commands_processed_total, redis_keyspace_hits_total | "
        "redis receiver (shared redis) |",
        "| Kafka broker | kafka_request_time_99p_milliseconds, kafka_message_count_total, "
        "kafka_request_failed_total | kafka |",
        "| Kafka consumer lag | kafka_consumer_group_lag_ratio{group=\"accounting\"} (messages behind) | "
        "kafkametrics receiver |",
        "| Node.js runtime | nodejs_eventloop_delay_p99_seconds, v8js_memory_heap_used_bytes | frontend, payment |",
        "| Python runtime | cpython_gc_collections_total, process_memory_usage_bytes | recommendation |",
        "| Business counters | demo_payment_transactions_total, demo_shipping_items_shipped_total, "
        "quotes_total | per service |",
        "| HTTP check | httpcheck_status, httpcheck_duration_milliseconds | frontend-proxy probe |",
        "| Feature flags | feature_flag_evaluation_requests_total | flagd clients |",
        "| Collector | otelcol_exporter_sent_spans_total, otelcol_exporter_send_failed_* | otel-collector |",
        "",
        "## Querying a service's error ratio and latency",
        "",
        "The per-language request metrics differ in name and unit, and currency and email emit "
        "none, so the uniform RED source is span metrics. For a service SVC:",
        "",
        "- error ratio: `sum(rate(traces_span_metrics_calls_total{service_name=\"SVC\", "
        "span_kind=\"SPAN_KIND_SERVER\", status_code=\"STATUS_CODE_ERROR\"}[5m])) / "
        "sum(rate(traces_span_metrics_calls_total{service_name=\"SVC\", span_kind=\"SPAN_KIND_SERVER\"}[5m]))`",
        "- p95 duration (ms): `histogram_quantile(0.95, sum by (le) (rate("
        "traces_span_metrics_duration_milliseconds_bucket{service_name=\"SVC\", "
        "span_kind=\"SPAN_KIND_SERVER\"}[5m])))`",
        "",
        "accounting and fraud-detection have no server spans; use `span_kind=\"SPAN_KIND_CONSUMER\"`. "
        "The Kafka broker, redis, postgres and the collector emit no spans at all.",
        "",
        "## Which label selects a service",
        "",
        "| Metric family | Select a service with |",
        "|---|---|",
        "| SDK metrics and span metrics | `service_name=\"cart\"` |",
        "| container_* (docker_stats) | `container_name=\"cart\"` |",
        "| kafka_consumer_group_* | `group=\"accounting\"` or `group=\"fraud-detection\"` |",
        "| redis_*, postgresql_*, nginx_*, httpcheck_* | no service label — one shared target each |",
        "",
        "A selector on the wrong label returns an empty result, not an error; an empty graph "
        "means \"wrong label\" far more often than \"zero\".",
        "",
        "## Jaeger",
        "",
        "Jaeger v2 (1200M memory limit) stores traces and serves the UI and query API on host port "
        "16686 under base path /jaeger/ui: the UI is http://localhost:16686/jaeger/ui/ and the API "
        "http://localhost:16686/jaeger/ui/api/. The base path applies to the API too, so the bare "
        "http://localhost:16686/api/ answers with HTML, not JSON. The proxy route "
        "http://localhost:8080/jaeger/ui/ reaches the same UI. A trace is the tree of spans for one "
        "request; the span with an error status or the one dominating wall-clock time points at "
        "the service to investigate.",
        "",
        "## OpenSearch and logs",
        "",
        "Logs from every service go to OpenSearch (index `otel-logs`, 1G memory limit) and can be "
        "explored in Grafana. Sentinel's RCA agent deliberately does not read logs — it works from "
        "metrics and traces only (ADR-003).",
        "",
        "## Grafana",
        "",
        "Grafana at http://localhost:8080/grafana/ with provisioned datasources for Prometheus, "
        "Jaeger and OpenSearch and upstream's demo dashboards.",
        "",
        "## How Sentinel reads the platform",
        "",
        "| Source | Sentinel setting | Used for |",
        "|---|---|---|",
        "| Prometheus | prometheus_url (http://localhost:9090) | PromQL queries through a read-only API, "
        "metric-name allowlist, bounded time range |",
        "| Jaeger | jaeger_url (http://localhost:16686/jaeger/ui) | Trace lookups for a service and time window |",
        "| flagd | flagd_config_path (staged demo.flagd.json) | Which fault flags are currently on |",
        "| incident-generator | incident_log_url (http://localhost:8092) | Recent incidents and scenario status |",
        "| system-monitor | system_monitor_url (http://localhost:8091) | Live health rollup |",
        "",
        "Live-state tools are never cached: a cached \"healthy\" is worse than no answer during an incident.",
    ]
    return "\n".join(L)


# ----------------------------------------------------------------------------- failure modes
def _failure_modes(p: Platform, flags: dict, scenarios: list[dict]) -> str:
    scen_by_flag = {s["flag"]: s for s in scenarios}
    fault_flags = [f for f in flags if f not in TRAFFIC_FLAGS]

    L = [f"# {p.name} failure modes and fault injection", ""]
    L += [
        f"Every way the platform is known to fail: the {len(fault_flags)} injectable faults built "
        "into the OpenTelemetry Demo, the failure classes they belong to, and the infrastructure "
        "failures no flag controls.",
        "",
        "## How faults are injected",
        "",
        "Faults are feature flags served by **flagd** from `demo.flagd.json`. flagd watches that "
        "file and reloads on change, so flipping a flag's `defaultVariant` turns a fault on within "
        "seconds with no restart. Three things edit the file: flagd-ui (a human, at "
        "http://localhost:8080/feature), the incident-generator (a named scenario, logged), and "
        "anyone editing the staged file directly. Only the incident-generator writes an incident "
        "log, so a fault turned on in flagd-ui is real but unrecorded.",
        "",
        "## Every flag",
        "",
        "| Flag | Target service | Variants | What it does | incident-generator scenario |",
        "|---|---|---|---|---|",
    ]
    for name, spec in flags.items():
        target = p.faults[name].service if name in p.faults else ("load-generator" if name in TRAFFIC_FLAGS else "-")
        variants = ", ".join(spec["variants"])
        scen = scen_by_flag.get(name)
        scen_txt = f"{scen['name']} ({scen['fault_variant']}, {scen['duration_s']}s)" if scen else "-"
        L.append(f"| {name} | {target} | {variants} | {spec.get('description', '')} | {scen_txt} |")
    L += [
        "",
        "All fault flags default to `off`. loadGeneratorTraffic defaults to `on` and "
        "loadGeneratorVUs to `5`; those two control traffic, not faults.",
        "",
        "## Failure classes",
        "",
        "- **Hard failure (errors)** — adFailure, paymentFailure, cartFailure, productCatalogFailure. "
        "Error ratio rises on the target and on its callers; latency usually flat.",
        "- **Unreachable** — paymentUnreachable. Connections refused; callers see connection errors "
        "and the target's telemetry may stop entirely.",
        "- **Taken out of rotation** — failedReadinessProbe. Traffic to cart drops to zero with no "
        "error spike, because the process is fine but receives nothing.",
        "- **Latency injection** — intlShippingSlowdown, imageSlowLoad. Healthchecks stay green; "
        "p95/p99 duration rises on the target and on everything upstream of it.",
        "- **Resource exhaustion** — adHighCpu (CPU), emailMemoryLeak and recommendationCacheFailure "
        "(memory), adManualGc (GC pauses). Degrades gradually; may never trip an error alert.",
        "- **Backpressure** — kafkaQueueProblems. Consumer lag climbs; customer path unaffected.",
        "",
    ]
    L += ["## Each fault in detail", ""]
    for f in p.faults.values():
        scen = scen_by_flag.get(f.flag)
        L += [f"### {f.flag} ({f.service})", ""]
        L.append(f"Effect: {f.effect}")
        L.append("")
        L.append(f"Signature: {f.signature}")
        br = p.blast_radius(f.service)
        L.append("")
        L.append(f"Blast radius: {', '.join(br) if br else 'none beyond ' + f.service}.")
        if scen:
            L.append("")
            L.append(
                f"incident-generator scenario `{scen['name']}` sets it to `{scen['fault_variant']}` "
                f"for {scen['duration_s']} seconds. {scen.get('note', '')}"
            )
        if not p.deployment[f.service].deployed:
            L.append("")
            L.append(f"Not observable here: {f.service} does not run under `make demo-up`.")
        L.append("")
    L += [
        "## Failures no flag controls",
        "",
        "- **postgres down** — product-catalog cannot read products; frontend product pages, "
        "recommendation and checkout fail. Sentinel's own retrieval and chat history fail too. "
        "system-monitor shows astronomy-db unhealthy via its SELECT 1 check.",
        "- **redis down** — cart fails, so add-to-cart and checkout fail; Sentinel loses its cache "
        "but keeps working. system-monitor shows redis unhealthy via PING.",
        "- **otel-collector down** — no new metrics, traces or logs; every rate graph flatlines "
        "while customers are unaffected. Easy to misread as a total outage.",
        "- **flagd down** — services fall back to flag defaults (faults off); flagd-ui stops working.",
        "- **load-generator stopped or loadGeneratorTraffic off** — request rates fall to zero "
        "everywhere at once with no errors. Not a platform outage.",
        "- **OOM kill from a memory limit** — containers restart only if restarted by hand "
        "(restart policy `no`), so an OOM leaves the service down and red in the health rollup.",
        "- **Container crash** — the service goes `exited`; system-monitor marks it unhealthy and "
        "every ancestor in the health tree turns unhealthy with it.",
        "",
        "## Telling failure classes apart",
        "",
        "| Symptom | Most likely class |",
        "|---|---|",
        "| Errors up on one service and its callers, latency flat | Hard failure in the deepest erroring service |",
        "| Request rate to one service drops to zero, no errors | Readiness probe / taken out of rotation |",
        "| Request rate drops to zero everywhere | load-generator stopped or collector down |",
        "| p99 up, errors flat, healthchecks green | Latency injection or CPU saturation |",
        "| Memory climbing steadily, periodic restarts | Memory leak |",
        "| Sawtooth latency correlated with GC time | Forced GC (adManualGc) |",
        "| Errors on one product only | productCatalogFailure |",
    ]
    return "\n".join(L)


# ------------------------------------------------------------------------ incident-generator
def _incident_generator(scenarios: list[dict]) -> str:
    L = ["# Incident generator", ""]
    L += [
        "The incident-generator is a small FastAPI service Sentinel adds to the platform. It turns "
        "real OpenTelemetry Demo failure modes on and off as named scenarios, and writes an "
        "incident log that is the ground truth for what was injected and when. The load-generator "
        "supplies \"real users calling real services\"; the incident-generator supplies \"and "
        "sometimes it breaks\".",
        "",
        "## Where it runs",
        "",
        "Container `incident-generator` in the astroshop Compose project (added by "
        "compose.extras.yaml), built from `app/incident-generator/`, Python 3.12 + uvicorn on "
        "container port 8000, published on **host port 8092**. It mounts the staged flagd "
        "directory (`~/.local/share/sentinel-rag/runtime/flagd` → /etc/flagd) and its data "
        "directory (`~/.local/share/sentinel-rag/app/incident-generator-data` → /app/data). It "
        "starts after flagd.",
        "",
        "## How it injects a fault",
        "",
        "It edits the same `demo.flagd.json` that flagd watches: sets the scenario's flag "
        "`defaultVariant` to the fault variant, waits `duration_s` seconds, then sets it back to the "
        "reset variant. Writes go to a temp file and are renamed into place, so flagd never reads "
        "half-written JSON. No flagd API and no restart are involved.",
        "",
        "## Scenarios",
        "",
        "| Scenario | Flag | Fault variant | Duration | What to expect |",
        "|---|---|---|---|---|",
    ]
    for s in scenarios:
        L.append(f"| {s['name']} | {s['flag']} | {s['fault_variant']} | {s['duration_s']}s | {s.get('note', '')} |")
    L += [
        "",
        f"{len(scenarios)} scenarios in total, defined in `app/incident-generator/scenarios.yaml`. "
        "Every scenario resets its flag to `off`. Flags with no scenario (adManualGc, "
        "failedReadinessProbe, imageSlowLoad, kafkaQueueProblems) can still be flipped by hand in "
        "flagd-ui.",
        "",
        "## Triggering",
        "",
        "Triggering is **manual only**. The chat UI's platform panel calls `POST /trigger/{name}`. "
        "An earlier random scheduler was disabled: incidents firing on their own made \"why did "
        "checkout just fail?\" ambiguous while someone was driving a scenario from the UI.",
        "",
        "Only one scenario runs at a time. Triggering while one is active returns HTTP 409 "
        "(\"scenario already running\"); an unknown name returns 404.",
        "",
        "## API",
        "",
        "| Method and path | Returns |",
        "|---|---|",
        "| GET /scenarios | Every scenario definition |",
        "| GET /status | `active` (the running scenario, or null) and `recent` (up to 10 finished runs "
        "with start and end timestamps) |",
        "| POST /trigger/{name} | Starts the scenario in the background: `{\"triggered\": name}` |",
        "| GET /incidents?limit=50 | Raw incident-log events, newest last |",
        "",
        "## The incident log",
        "",
        "`incidents.jsonl` has one JSON line per state change: `ts` (Unix time), `event` "
        "(`incident_start` or `incident_end`), `scenario`, `flag`, `fault_variant`, `note`. A *run* "
        "is an incident_start plus its incident_end. Only the newest **5 runs** are kept (whole "
        "runs are dropped, never single lines; an active run is never dropped) — it is a live "
        "status feed, not an audit log. It survives container restarts because it lives on the "
        "host.",
        "",
        "## Crash safety",
        "",
        "On startup it resets every scenario's flag to its reset variant. A crash or rebuild "
        "mid-incident would otherwise leave a fault switched on forever, since the timer that "
        "would have switched it off died with the process. Reset targets come from scenarios.yaml, "
        "not from the file's current state.",
        "",
        "## How Sentinel uses it",
        "",
        "Sentinel's chat routes \"is an incident running?\"-style questions straight to "
        "`/incidents` (no LLM involved), and the RCA agent compares its findings against the log. "
        "Setting: `incident_log_url`, default http://localhost:8092.",
        "",
        "`make capture-incident S=<scenario>` uses it to build agent evaluation cases from real "
        "incidents: it triggers the scenario, records every RCA tool's live output mid-incident, "
        "runs the live agent, and stores the case with the flag the generator flipped as ground truth.",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------------------- system-monitor
def _system_monitor(tree: dict) -> str:
    nodes = tree["nodes"]

    def render(name: str, depth: int) -> list[str]:
        n = nodes[name]
        check = "ping" if n.get("check") == "ping" else "docker healthcheck"
        container = f", container {n['container']}" if n.get("container") else ""
        out = [f"{'  ' * depth}- {name} ({check}{container})"]
        for child in n.get("depends_on") or []:
            out += render(child, depth + 1)
        return out

    L = ["# System monitor", ""]
    L += [
        "The system-monitor is a small FastAPI service Sentinel adds to the platform. It answers "
        "\"is the platform healthy right now, and if not, which branch?\" by rolling each "
        "service's own health up a parent→child tree. Docker only ever reports on one container; "
        "the rollup is the part Docker does not do.",
        "",
        "## Where it runs",
        "",
        "Container `system-monitor` in the astroshop Compose project (added by "
        "compose.extras.yaml), built from `app/system-monitor/`, published on **host port 8091**. "
        "It mounts the Docker socket read-only and joins the `infra` network so it can reach "
        "postgres and redis by name.",
        "",
        "## How health is determined",
        "",
        "- **Docker nodes** — reads the container's own Docker healthcheck state. Each service's "
        "compose healthcheck already runs the right probe (gRPC health, HTTP, TCP), so "
        "system-monitor does not reimplement them. A container that is not running is "
        "`unhealthy` (exited, dead, restarting) or `missing` (not found); a running container with "
        "no healthcheck is treated as `unhealthy`, so green always means something checked it.",
        "- **Ping nodes** — redis and astronomy-db are outside the Compose project, so a Docker "
        "lookup would only say \"the container is up\". Instead it connects: Redis `PING` "
        "(redis://redis:6379/1) and Postgres `SELECT 1` against astronomy_db, 3-second timeout each.",
        "",
        "## Statuses and the rollup rule",
        "",
        "Statuses, best to worst: `healthy`, `starting`, `missing`, `unhealthy`. A node's rollup "
        "status is the **worst** of its own status and every child's rollup, computed bottom-up. "
        "So one unhealthy leaf turns every ancestor unhealthy up to the root — \"is checkout okay?\" "
        "already answers no when payment is down. Each node reports both `own` (itself) and "
        "`status` (rollup); `own` healthy with `status` unhealthy means the problem is below it.",
        "",
        "Docker is polled every 5 seconds. Stream subscribers are only notified when the rollup "
        "actually changes.",
        "",
        "## The health tree",
        "",
        f"Root: {', '.join(tree['roots'])} (display name Astroshop; its container is frontend-proxy).",
        "",
    ]
    for root in tree["roots"]:
        L += render(root, 0)
    L += [
        "",
        f"{len(nodes)} nodes in total.",
        "",
        "## Why the tree is not the call graph",
        "",
        "Nesting on compose `depends_on` put nearly everything under frontend and put cart under "
        "three parents. Instead each node appears exactly once, under the caller that most "
        "conceptually owns it: payment, shipping, currency and email under checkout; quote under "
        "shipping; ad under frontend. cart and product-catalog are called by several services but "
        "owned by none, so they sit directly under astroshop with their data store beneath them.",
        "",
        "## What it excludes",
        "",
        "otel-collector, flagd and load-generator are deliberately not in the tree. Every service "
        "depends on the first two, so including them would make them everyone's parent and hide "
        "the business structure. load-generator is a test harness: its death means \"we stopped "
        "measuring\", not \"the platform is down\". Jaeger, Prometheus, Grafana and OpenSearch are "
        "also not tracked.",
        "",
        "## API",
        "",
        "| Method and path | Returns |",
        "|---|---|",
        "| GET /health | Root-level rollup only, e.g. `{\"roots\": {\"astroshop\": \"healthy\"}}` |",
        "| GET /tree | The full nested tree with own and rollup status per node |",
        "| GET /nodes | Flat map of every node: display_name, own, status, depends_on |",
        "| GET /nodes/{name} | One node; 404 for an unknown name |",
        "| GET /stream | Server-sent events: the /tree payload on every change, heartbeat every 15s |",
        "",
        "SSE rather than WebSocket because the data only ever flows server → browser.",
        "",
        "## How Sentinel uses it",
        "",
        "Sentinel's chat routes health questions (\"is the platform up?\", \"which services are "
        "unhealthy?\") straight to `/nodes` with no LLM involved, and the platform panel in the "
        "web UI subscribes to `/stream`. Setting: `system_monitor_url`, default http://localhost:8091.",
        "",
        "## Limits",
        "",
        "It reports container health, not correctness. A service returning errors for 50% of "
        "requests (paymentFailure at 50%) or running 10 seconds slow (intlShippingSlowdown) usually "
        "stays healthy here, because its healthcheck still passes. Error ratios and latency come "
        "from Prometheus; the health tree catches crashes, unreachability and data-store outages.",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------------------- components
def _components(p: Platform) -> str:
    L = [f"# {p.name} platform components", ""]
    L += [
        "Everything that runs alongside the business services but is not one: the traffic "
        "generator, tooling, observability backends, Sentinel's sidecars and the shared "
        "infrastructure. None has an SLO or a customer.",
        "",
    ]
    categories = {
        "traffic": "Traffic",
        "tooling": "Tooling",
        "observability": "Observability backends",
        "sentinel-sidecar": "Sentinel sidecars",
        "shared-infra": "Shared infrastructure",
        "sentinel": "Sentinel's own services",
    }
    for cat, heading in categories.items():
        members = [c for c in p.components.values() if c.category == cat]
        if not members:
            continue
        L += [f"## {heading}", ""]
        for c in members:
            L += [f"### {c.name}", "", c.what, "", f"Runs in: {c.runs_in}.", ""]
            L += [f"Why it matters: {c.why_it_matters}", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------------- FAQ
def _faq(p: Platform, flags: dict, scenarios: list[dict], tree: dict) -> str:
    deployed = _deployed(p)
    absent = _not_deployed(p)
    fault_flags = [f for f in flags if f not in TRAFFIC_FLAGS]
    qa: list[tuple[str, str]] = [
        ("What services exist?",
         f"{len(p.services)} services: {', '.join(p.services)}. {len(deployed)} run under "
         f"`make demo-up`; {', '.join(absent)} do not. See the service catalog for each one."),
        ("How many services are there?",
         f"{len(p.services)} in the platform model ({len(p.services) - 2} business services plus "
         f"otel-collector and flagd); {len(deployed)} are running in this deployment. Counting every "
         f"container in the astroshop project, including Jaeger, Prometheus, Grafana, OpenSearch, "
         f"the load generator and Sentinel's sidecars, is closer to 25."),
        ("Which services are critical (tier 0)?",
         ", ".join(n for n, s in p.services.items() if s.tier == 0) + "."),
        ("What languages are the services written in?",
         "; ".join(f"{n}: {s.language}" for n, s in p.services.items()) + "."),
        ("What is the entry point?",
         "frontend-proxy (Envoy) on http://localhost:8080. It routes to frontend and to the UIs for "
         "Jaeger, Grafana, flagd-ui and telemetry-docs."),
        ("What does checkout call?",
         "In order: cart (GetCart), product-catalog and currency for each item, shipping for a "
         "quote (shipping calls quote), payment (Charge), shipping again (ShipOrder), cart "
         "(EmptyCart), email, then Kafka. Everything up to ShipOrder is synchronous and fails the order."),
        ("Which service is the most shared dependency?",
         "product-catalog: frontend, checkout and recommendation all call it."),
        ("Where does cart store data?",
         "In the shared redis container, logical DB 1. Upstream's valkey-cart is disabled here."),
        ("Where does product-catalog store data?",
         "In the astronomy_db database on the shared postgres container. Upstream's astronomy-db is disabled here."),
        ("Is Kafka running?",
         "Yes. `make demo-up` layers in compose.full.yaml, which runs a single Kafka broker plus its "
         "two consumers, accounting and fraud-detection. checkout produces every order to the "
         "`orders` topic."),
        ("How is tracing set up?",
         "Every service sends OTLP to otel-collector (4317 gRPC / 4318 HTTP); the collector "
         "exports traces to Jaeger, viewable at http://localhost:16686/jaeger/ui/."),
        ("Where are metrics stored?",
         "Prometheus at http://localhost:9090. The collector pushes metrics to it over OTLP."),
        ("Where are logs stored?",
         "OpenSearch, index otel-logs. Sentinel's RCA agent does not read logs (ADR-003)."),
        ("How are faults injected?",
         f"Through flagd feature flags in demo.flagd.json — {len(fault_flags)} fault flags. Flip "
         "them by hand in flagd-ui (http://localhost:8080/feature) or run a named scenario from the "
         "incident-generator."),
        ("What failure modes exist?",
         ", ".join(f"{f} ({p.faults[f].service})" for f in fault_flags if f in p.faults) + "."),
        ("What is the incident-generator?",
         f"Sentinel's sidecar on host port 8092 that runs one of {len(scenarios)} named fault "
         "scenarios on request by editing demo.flagd.json, then resets it after a fixed duration, "
         "and logs each start/end to incidents.jsonl."),
        ("What scenarios can the incident-generator run?",
         ", ".join(s["name"] for s in scenarios) + "."),
        ("What is the system-monitor?",
         f"Sentinel's sidecar on host port 8091 that rolls Docker healthchecks and redis/postgres "
         f"pings up a {len(tree['nodes'])}-node parent→child tree rooted at astroshop, so one "
         "unhealthy leaf shows at every ancestor."),
        ("Why isn't otel-collector or flagd in the health tree?",
         "Every service depends on them; including them would make them everyone's parent and "
         "hide the business structure. load-generator is excluded as a test harness."),
        ("Can a service be failing while system-monitor says healthy?",
         "Yes. It reports container health. Partial error rates and injected latency usually keep "
         "the healthcheck passing; use Prometheus error ratios and latency for those."),
        ("How do I start and stop the platform?",
         "`make otel-demo-up` (or `make up` then `make demo-up`) to start, `make demo-down` to stop. "
         "`make demo-status` prints the health rollup and recent incidents."),
        ("What happens if postgres goes down?",
         "product-catalog cannot read products, so product pages, recommendation and checkout fail; "
         "Sentinel's retrieval and chat history also fail."),
        ("What happens if redis goes down?",
         "cart fails, so add-to-cart and checkout fail. Sentinel loses its cache but keeps working."),
        ("Who owns which service?",
         "; ".join(f"{t.name}: {', '.join(t.owns)}" for t in p.teams.values()) + "."),
        ("What generates traffic?",
         "The load-generator (k6), 5 virtual users by default, against frontend-proxy. Controlled by "
         "the loadGeneratorTraffic and loadGeneratorVUs flags."),
    ]
    L = [f"# {p.name} platform FAQ", "", "Short answers to common questions about the platform itself.", ""]
    for q, a in qa:
        L += [f"## {q}", "", a, ""]
    return "\n".join(L)


def main() -> int:
    p = load()
    flags = json.loads(FLAGD_JSON.read_text())["flags"]
    scenarios = yaml.safe_load(SCENARIOS_YAML.read_text())["scenarios"]
    tree = yaml.safe_load(TREE_YAML.read_text())

    docs = {
        "platform-overview": _overview(p),
        "service-catalog": _catalog(p),
        "service-dependencies": _dependencies(p),
        "infrastructure-and-deployment": _infra(p),
        "observability-stack": _observability(p),
        "failure-modes": _failure_modes(p, flags, scenarios),
        "incident-generator": _incident_generator(scenarios),
        "system-monitor": _system_monitor(tree),
        "platform-components": _components(p),
        "platform-faq": _faq(p, flags, scenarios, tree),
    }
    print("wrote:")
    for stem, body in docs.items():
        print(f"  {rel(write(DOC_TYPE, stem, body))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
