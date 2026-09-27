# System monitor

The system-monitor is a small FastAPI service Sentinel adds to the platform. It answers "is the platform healthy right now, and if not, which branch?" by rolling each service's own health up a parent→child tree. Docker only ever reports on one container; the rollup is the part Docker does not do.

## Where it runs

Container `system-monitor` in the astroshop Compose project (added by compose.extras.yaml), built from `app/system-monitor/`, published on **host port 8091**. It mounts the Docker socket read-only and joins the `infra` network so it can reach postgres and redis by name.

## How health is determined

- **Docker nodes** — reads the container's own Docker healthcheck state. Each service's compose healthcheck already runs the right probe (gRPC health, HTTP, TCP), so system-monitor does not reimplement them. A container that is not running is `unhealthy` (exited, dead, restarting) or `missing` (not found); a running container with no healthcheck is treated as `unhealthy`, so green always means something checked it.
- **Ping nodes** — redis and astronomy-db are outside the Compose project, so a Docker lookup would only say "the container is up". Instead it connects: Redis `PING` (redis://redis:6379/1) and Postgres `SELECT 1` against astronomy_db, 3-second timeout each.

## Statuses and the rollup rule

Statuses, best to worst: `healthy`, `starting`, `missing`, `unhealthy`. A node's rollup status is the **worst** of its own status and every child's rollup, computed bottom-up. So one unhealthy leaf turns every ancestor unhealthy up to the root — "is checkout okay?" already answers no when payment is down. Each node reports both `own` (itself) and `status` (rollup); `own` healthy with `status` unhealthy means the problem is below it.

Docker is polled every 5 seconds. Stream subscribers are only notified when the rollup actually changes.

## The health tree

Root: astroshop (display name Astroshop; its container is frontend-proxy).

- astroshop (docker healthcheck, container frontend-proxy)
  - frontend (docker healthcheck)
    - ad (docker healthcheck)
  - checkout (docker healthcheck)
    - currency (docker healthcheck)
    - email (docker healthcheck)
    - payment (docker healthcheck)
    - shipping (docker healthcheck)
      - quote (docker healthcheck)
  - recommendation (docker healthcheck)
  - cart (docker healthcheck)
    - redis (ping)
  - product-catalog (docker healthcheck)
    - astronomy-db (ping)
  - image-provider (docker healthcheck)
  - flagd-ui (docker healthcheck)
  - telemetry-docs (docker healthcheck)

17 nodes in total.

## Why the tree is not the call graph

Nesting on compose `depends_on` put nearly everything under frontend and put cart under three parents. Instead each node appears exactly once, under the caller that most conceptually owns it: payment, shipping, currency and email under checkout; quote under shipping; ad under frontend. cart and product-catalog are called by several services but owned by none, so they sit directly under astroshop with their data store beneath them.

## What it excludes

otel-collector, flagd and load-generator are deliberately not in the tree. Every service depends on the first two, so including them would make them everyone's parent and hide the business structure. load-generator is a test harness: its death means "we stopped measuring", not "the platform is down". Jaeger, Prometheus, Grafana and OpenSearch are also not tracked.

## API

| Method and path | Returns |
|---|---|
| GET /health | Root-level rollup only, e.g. `{"roots": {"astroshop": "healthy"}}` |
| GET /tree | The full nested tree with own and rollup status per node |
| GET /nodes | Flat map of every node: display_name, own, status, depends_on |
| GET /nodes/{name} | One node; 404 for an unknown name |
| GET /stream | Server-sent events: the /tree payload on every change, heartbeat every 15s |

SSE rather than WebSocket because the data only ever flows server → browser.

## How Sentinel uses it

Sentinel's chat routes health questions ("is the platform up?", "which services are unhealthy?") straight to `/nodes` with no LLM involved, and the platform panel in the web UI subscribes to `/stream`. Setting: `system_monitor_url`, default http://localhost:8091.

## Limits

It reports container health, not correctness. A service returning errors for 50% of requests (paymentFailure at 50%) or running 10 seconds slow (intlShippingSlowdown) usually stays healthy here, because its healthcheck still passes. Error ratios and latency come from Prometheus; the health tree catches crashes, unreachability and data-store outages.
