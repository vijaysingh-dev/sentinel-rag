# Astroshop infrastructure and deployment

How the platform is deployed on the local machine: Docker Compose layering, networks, shared databases, host ports, and the make targets that start and stop it.

## Docker Compose layout

`make demo-up` starts one Compose project named **astroshop** from four files layered in order:

1. `app/otel-demo/compose.yaml` — the business services, flagd, flagd-ui, load-generator, telemetry-docs and the OpenTelemetry collector.
2. `app/otel-demo/compose.full.yaml` — Kafka, accounting and fraud-detection, and checkout's Kafka producer settings.
3. `app/otel-demo/compose.observability.yaml` — Jaeger, Prometheus, Grafana, OpenSearch and the OpAMP server, plus the collector config that exports to them. Must come after the full layer: its collector command lists otelcol-config-full.yml ahead of its own.
4. `app/otel-demo/compose.extras.yaml` — Sentinel's changes: disables astronomy-db and valkey-cart, points product-catalog, cart and accounting at the shared infra containers, publishes Jaeger on a fixed port, overrides bind mounts to a runtime staging directory, and adds system-monitor and incident-generator.

Env files: `app/otel-demo/.env` (upstream ports and images, IMAGE_VERSION 3.0.0) then `app/otel-demo/.env.override` (POSTGRES_HOST=postgres, VALKEY_ADDR=redis:6379,defaultDatabase=1, LLM settings for upstream's optional agent stack).

Not layered in: `compose.profiling.yaml`, `compose.agent.yaml` and `compose.tests.yaml`.

## The shared infra layer

postgres and redis are **not** part of any Compose project. They are standalone, always-on containers (`docker run --restart unless-stopped`) on a Docker network called `infra`, started once and left running — the same way Portainer is. Idle, the two cost about 57MB together, too little to be worth starting and stopping per project. Everything that costs something idle (the ~25 Astroshop containers, Sentinel's Langfuse) is started and stopped manually.

- **postgres** (pgvector/pgvector:pg16, host port 5432): databases `astronomy_db` (owner astronomy_user; product-catalog's product data) and `sentinel` (owner sentinelapp; Sentinel's vectors, chat sessions, agent checkpoints, Langfuse). Bootstrap user postgres/postgres.
- **redis** (redis:7-alpine, host port 6379, maxmemory 512mb, allkeys-lru, no volume): DB 0 is Sentinel's cache (keys prefixed `sentinel:`), DB 1 is Astroshop's cart storage. Restarting it loses carts, nothing durable.

product-catalog, cart, otel-collector (for its PostgreSQL receiver) and system-monitor join both the astroshop default network and `infra`, and reach the two by container name. Because they are outside the Compose project, `depends_on` cannot wait on them — they are simply expected to be up already. `make up` checks that and fails fast if not.

## Why astronomy-db and valkey-cart are disabled

Upstream ships its own Postgres (astronomy-db, 80M limit) and Valkey (valkey-cart, 20M). Running a second Postgres and a second Redis next to the shared ones was not worth it, so compose.extras.yaml puts both behind a profile (`disabled-by-sentinel-rag`) nothing activates. Compose has no directive to delete a service from an earlier file; an unrequested profile is the idiomatic way to say "don't start this".

## Runtime staging directory

The repository sits on a filesystem where chmod is a no-op (a fuseblk-style mount), so containers that drop to a non-root user cannot read files bind-mounted straight from the repo. Configs are therefore staged under `~/.local/share/sentinel-rag/runtime/` and mounted from there: flagd's demo.flagd.json, Jaeger, Grafana and Prometheus configs, product-catalog's otel-config.yml. This also keeps the incident-generator's constant edits to demo.flagd.json out of `git status`. Incident logs live in `~/.local/share/sentinel-rag/app/incident-generator-data/`.

## Host ports

| What | Address |
|---|---|
| Storefront (frontend-proxy) | http://localhost:8080 |
| Envoy admin | http://localhost:10000 |
| Jaeger UI | http://localhost:16686/jaeger/ui/ (also via the proxy at http://localhost:8080/jaeger/ui/) |
| Grafana | http://localhost:8080/grafana/ |
| Flag UI (flagd-ui) | http://localhost:8080/feature |
| Telemetry docs | http://localhost:8080/telemetry/ |
| Prometheus | http://localhost:9090 |
| system-monitor | http://localhost:8091 |
| incident-generator | http://localhost:8092 |
| Sentinel chat UI | http://localhost:3000 |
| Sentinel chat API | http://localhost:8093 |
| Sentinel LangGraph dev server (debug only) | http://localhost:2024 |
| postgres | localhost:5432 |
| redis | localhost:6379 |

Every other container port (cart 7070, checkout 5050, kafka 9092, and so on) is published to a random host port chosen by Docker, so the table above is the full list of stable addresses. Inside the astroshop network, services reach each other by name and container port, e.g. `cart:7070`.

## Container ports and memory limits

| Service | Container port | Protocol | Memory limit | Running here |
|---|---|---|---|---|
| frontend | 8080 | HTTP | 250M | yes |
| frontend-proxy | 8080 | HTTP | 90M | yes |
| cart | 7070 | gRPC | 160M | yes |
| checkout | 5050 | gRPC | 20M | yes |
| payment | 50051 | gRPC | 140M | yes |
| shipping | 50050 | HTTP | 20M | yes |
| currency | 7001 | gRPC | 20M | yes |
| email | 6060 | HTTP | 100M | yes |
| product-catalog | 3550 | gRPC | 20M | yes |
| recommendation | 9001 | gRPC | 500M | yes |
| ad | 9555 | gRPC | 300M | yes |
| quote | 8090 | HTTP | 40M | yes |
| image-provider | 8081 | HTTP | 120M | yes |
| accounting | - | Kafka consumer | 160M | yes |
| fraud-detection | - | Kafka consumer | 300M | yes |
| kafka | 9092 | Kafka | 620M | yes |
| valkey-cart | 6379 | Redis | 20M | no |
| astronomy-db | 5432 | PostgreSQL | 80M | no |
| otel-collector | 4317 | OTLP gRPC (4317) and OTLP HTTP (4318) | 400M | yes |
| flagd | 8013 | gRPC flag evaluation (8013) and OFREP HTTP (8016) | 75M | yes |

Memory limits are tight on purpose (checkout, currency, shipping and product-catalog at 20M). A memory-leak fault reaches the limit and gets OOM-killed quickly, which is what makes those faults observable in minutes rather than hours.

## Starting and stopping

| Command | What it does |
|---|---|
| `make up` | Checks postgres/redis are reachable on `infra`, starts Langfuse |
| `make demo-up` | Stages demo.flagd.json if missing, starts the astroshop project, waits for Prometheus |
| `make otel-demo-up` | `make up`, applies the astronomy_db seed if missing, then `make demo-up` |
| `make demo-status` | Prints the health rollup and the last five incident-log events |
| `make demo-down` | Stops the astroshop project (postgres/redis stay up) |
| `make down` | Stops Langfuse |

After `make demo-up`, set `FLAGD_CONFIG_PATH` in Sentinel's `.env` to the staged demo.flagd.json so Sentinel's flag tools can read current flag state.

## Restart policy

Astroshop containers use Compose's default (`restart: no`): a crashed container stays down until someone restarts it, which is exactly what makes a crash visible to the health rollup. postgres, redis and Portainer use `unless-stopped`.
