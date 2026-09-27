# Astroshop platform overview

A public astronomy e-commerce storefront. Customers browse a product catalog, add items to a cart, and check out through a synchronous order pipeline. Traffic is synthetic, driven by the load generator; faults are injected through flagd. Telemetry is OpenTelemetry throughout, scraped by Prometheus and traced through Jaeger.

Astroshop is the OpenTelemetry Demo, run locally with Docker Compose. It is the system Sentinel investigates: Sentinel reads its metrics (Prometheus), traces (Jaeger), feature flags (flagd), health rollup (system-monitor) and incident log (incident-generator).

## How many services are there

The platform model defines **20 services**: 18 business services and data stores, plus 2 platform services (otel-collector and flagd) that every other service uses. Under `make demo-up`, **18 of them run** and 2 do not (valkey-cart, astronomy-db) — see "What is not running" below.

Alongside the services run 14 supporting components: the load generator, the flag UI, the observability backends (Jaeger, Prometheus, Grafana, OpenSearch, OpAMP server), Sentinel's two sidecars (system-monitor, incident-generator) and the shared infrastructure (postgres, redis). They are listed in the Platform components document.

Services are written in 14 languages/runtimes: C, C#, C++, Go, Java, JavaScript, Kotlin, Nginx, PHP, Python, Ruby, Rust, SQL, TypeScript. That polyglot mix is deliberate upstream — it shows OpenTelemetry instrumentation in every major ecosystem.

## What services exist

| Service | What it does | Tier | Team | Language | Running here |
|---|---|---|---|---|---|
| frontend | Serves the web storefront and the JSON API the browser calls. Fans out to every product path. | 0 | storefront | TypeScript | yes |
| frontend-proxy | Edge proxy. All external traffic and the flagd-ui and telemetry-docs routes pass through it. | 0 | storefront | C++ | yes |
| cart | Holds the customer's cart. Backed by valkey-cart (Redis-compatible). Health-checked with grpc_health_probe; a failing check marks it unhealthy but never restarts it. | 0 | checkout | C# | yes |
| checkout | The order pipeline. Gets the cart, prices each item (product-catalog + currency), quotes shipping, charges payment, books shipping, empties the cart, emails a confirmation, then publishes the order to Kafka. | 0 | checkout | Go | yes |
| payment | Charges the card. Pure compute stub, no real PSP. The last synchronous call before the order is confirmed. | 0 | checkout | JavaScript | yes |
| shipping | Quotes a shipping cost and books the shipment. Called by checkout. | 1 | checkout | Rust | yes |
| currency | Converts prices between currencies. Called by frontend and checkout. Pure compute, no dependencies. | 1 | checkout | C++ | yes |
| email | Sends the order-confirmation email. Called by checkout after the order is committed, best-effort. | 2 | checkout | Ruby | yes |
| product-catalog | The product database. Shared dependency of frontend and recommendation. Backed by astronomy-db. | 0 | catalog | Go | yes |
| recommendation | Returns "you may also like" products. Calls product-catalog. Has an in-process cache. | 1 | catalog | Python | yes |
| ad | Serves a contextual ad on the product page. Leaf service under frontend. | 1 | catalog | Java | yes |
| quote | Computes a shipping quote (weight/size -> price). Called by shipping. | 1 | catalog | PHP | yes |
| image-provider | Static image host for product photos. Served through frontend-proxy. | 1 | storefront | Nginx | yes |
| accounting | Consumes the orders topic from Kafka and writes ledger entries. Fully asynchronous. | 2 | data | C# | yes |
| fraud-detection | Consumes the orders topic and scores each order for fraud. Asynchronous, does not gate checkout. | 2 | data | Kotlin | yes |
| kafka | The order event bus. checkout produces to it; accounting and fraud-detection consume. | 1 | data | Java | yes |
| valkey-cart | In-memory store behind cart. Redis wire protocol. | 1 | platform-eng | C | no |
| astronomy-db | Relational store behind product-catalog. | 1 | platform-eng | SQL | no |
| otel-collector | Receives OTLP from every service, batches, and exports to Prometheus and Jaeger. Excluded from the health rollup by design. | 1 | platform-eng | Go | yes |
| flagd | Feature-flag daemon. Reads demo.flagd.json and serves flag evaluations. The fault-injection control plane. | 1 | platform-eng | Go | yes |

## Services by tier

- **tier 0 (customer-facing critical path)**: frontend, frontend-proxy, cart, checkout, payment, product-catalog.
- **tier 1 (supporting)**: shipping, currency, recommendation, ad, quote, image-provider, kafka, valkey-cart, astronomy-db, otel-collector, flagd.
- **tier 2 (async / back office)**: email, accounting, fraud-detection.

Tier 0 services are on the path a customer's purchase takes; if any of them fails, customers cannot browse or buy. Tier 1 services support that path but a failure is usually a degradation (slow shipping quotes, an empty ad slot, missing recommendations). Tier 2 services are asynchronous or best-effort: a failure there does not stop an order.

## Services by owning team

- **storefront** (pager PD-STOREFRONT, Slack #astroshop-storefront): frontend, frontend-proxy, image-provider.
- **checkout** (pager PD-CHECKOUT, Slack #astroshop-checkout): checkout, cart, payment, shipping, currency, email.
- **catalog** (pager PD-CATALOG, Slack #astroshop-catalog): product-catalog, recommendation, ad, quote.
- **data** (pager PD-DATA, Slack #astroshop-data): accounting, fraud-detection, kafka.
- **platform-eng** (pager PD-PLATFORM, Slack #astroshop-platform): otel-collector, flagd, valkey-cart, astronomy-db.

## What a customer does on the platform

1. **Browse** — the browser loads the storefront from frontend (through frontend-proxy). frontend calls product-catalog for products, currency to convert prices, recommendation for "you may also like", ad for a contextual ad, and image-provider for product photos.
2. **Add to cart** — frontend calls cart, which stores the cart in Redis.
3. **Check out** — frontend calls checkout, which runs the synchronous order pipeline: get the cart, look up each product, convert currency, get a shipping quote (shipping calls quote), charge the card (payment), book shipping, empty the cart, send the confirmation email (email), then publish the order to Kafka, where accounting records it and fraud-detection scores it.

Customers here are synthetic: the load-generator (k6) runs this journey continuously.

## What is not running

- **valkey-cart** — Disabled by compose.extras.yaml (profile disabled-by-sentinel-rag). Its role is played by the shared, always-on `redis` container, logical DB 1.
- **astronomy-db** — Disabled by compose.extras.yaml (profile disabled-by-sentinel-rag). Its role is played by the `astronomy_db` database inside the shared, always-on `postgres` container.

Both are replaced rather than missing: cart and product data are real, they live in the shared redis and postgres containers.

## Where to go next

- Service catalog — one section per service: ports, protocol, health check, SLO, metrics, faults.
- Service dependencies and request flows — who calls whom, and what breaks when something does.
- Infrastructure and deployment — Docker Compose layout, networks, ports, make targets.
- Observability stack — OpenTelemetry collector, Prometheus, Jaeger, OpenSearch, Grafana.
- Failure modes and fault injection — every flagd flag and what it breaks.
- Incident generator and System monitor — Sentinel's two sidecar services.
- Platform FAQ — short answers to the common questions.
