# Astroshop platform components

Everything that runs alongside the business services but is not one: the traffic generator, tooling, observability backends, Sentinel's sidecars and the shared infrastructure. None has an SLO or a customer.

## Traffic

### load-generator

k6-based synthetic shopper traffic against frontend-proxy (K6_TARGET_URL=http://frontend-proxy:8080). Browses, adds to cart and checks out continuously. On/off via the loadGeneratorTraffic flag, concurrency via loadGeneratorVUs (5, 10, 25 or 50 virtual users; default 5). Memory limit 512M.

Runs in: astroshop compose project (compose.yaml).

Why it matters: Without it no request rates, error ratios or latencies exist to investigate. If it stops, every rate graph drops to zero at once — that means "we stopped measuring", not "the platform is down".

## Tooling

### flagd-ui

Web UI for flipping flagd feature flags by hand, served through the proxy at http://localhost:8080/feature. Edits the same demo.flagd.json that flagd watches.

Runs in: astroshop compose project (compose.yaml).

Why it matters: The manual equivalent of the incident-generator. A flag flipped here is a real fault that the incident log will not record.

### telemetry-docs

Static documentation of the demo's telemetry schema, served through the proxy at /telemetry/.

Runs in: astroshop compose project (compose.yaml).

Why it matters: Leaf of the health tree under astroshop; its failure affects no customer path.

## Observability backends

### jaeger

Distributed-tracing backend (Jaeger v2). Receives traces from the collector over OTLP gRPC and serves the trace UI. The UI's own port 16686 is published to a random host port, so the stable address is through the proxy, http://localhost:8080/jaeger/ui/. Memory limit 1200M.

Runs in: astroshop compose project (compose.observability.yaml).

Why it matters: Where "which span in this request errored or was slow" is answered. Sentinel's trace tools read it via settings.jaeger_url.

### prometheus

Metrics backend, host port 9090. Metrics arrive pushed by the collector over OTLP (/api/v1/otlp) rather than being scraped from each service; resource attributes such as service.name are promoted to labels. Memory limit 200M.

Runs in: astroshop compose project (compose.observability.yaml).

Why it matters: Where RED metrics live. Sentinel's PromQL tool reads it read-only via settings.prometheus_url.

### grafana

Dashboards over Prometheus, Jaeger and OpenSearch, through the proxy at http://localhost:8080/grafana/. Memory limit 175M.

Runs in: astroshop compose project (compose.observability.yaml).

Why it matters: The human-facing view of the same data Sentinel queries directly.

### opensearch

Log store. The collector exports every service's OTLP logs to the otel-logs index. Memory limit 1G.

Runs in: astroshop compose project (compose.observability.yaml).

Why it matters: Logs are stored but Sentinel's RCA agent deliberately does not read them (ADR-003) — metrics and traces only.

### opamp-server

OpAMP (Open Agent Management Protocol) server the collector reports its effective config and health to.

Runs in: astroshop compose project (compose.observability.yaml).

Why it matters: Remote management of the collector; not on any request path.

## Sentinel sidecars

### system-monitor

Computes a parent-to-child health rollup of the platform from Docker healthchecks plus real Redis/Postgres pings. See the system-monitor document.

Runs in: astroshop compose project (compose.extras.yaml), host port 8091.

Why it matters: Source of "is the platform healthy right now" answers in the chat UI and the platform panel.

### incident-generator

Triggers named fault scenarios by editing flagd's flag file and writes an incident log. See the incident-generator document.

Runs in: astroshop compose project (compose.extras.yaml), host port 8092.

Why it matters: Source of ground truth for "what fault was injected, and when".

## Shared infrastructure

### postgres

pgvector/pgvector:pg16. Holds otel-demo's `astronomy_db` (owner astronomy_user, read by product-catalog) and Sentinel's own `sentinel` database (owner sentinelapp: vectors, chat sessions, checkpoints). Not managed by any compose project.

Runs in: standalone always-on container (docker run, restart unless-stopped) on the `infra` network, host port 5432.

Why it matters: Plays the astronomy-db role. If it is down, product-catalog cannot serve products, and Sentinel cannot retrieve.

### redis

redis:7-alpine, maxmemory 512mb, allkeys-lru, no volume. Logical DB 0 is Sentinel's cache (keys prefixed sentinel:); logical DB 1 is otel-demo's cart storage. A third consumer should claim DB 2.

Runs in: standalone always-on container (docker run, restart unless-stopped) on the `infra` network, host port 6379.

Why it matters: Plays the valkey-cart role. If it is down, cart fails and checkout fails with it.

### portainer

Docker GUI for seeing what is running and what is idle. Talks to Docker through the socket.

Runs in: standalone always-on container, not on the infra network.

Why it matters: Operator convenience only; nothing depends on it.

## Sentinel's own services

### langfuse

LLM tracing for Sentinel itself (prompts, tool calls, token cost), stored in the shared postgres.

Runs in: sentinel-rag compose project (`make up`), joins the infra network.

Why it matters: Observes Sentinel, not Astroshop.
