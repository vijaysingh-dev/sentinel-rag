# Astroshop service catalog

One section per service. Each lists what the service does, how it is built, where it listens, how Docker health-checks it, who owns it, what it depends on and what depends on it, its SLO, the metrics to watch, and the failure modes that can be injected into it.

## frontend

**frontend** — Serves the web storefront and the JSON API the browser calls. Fans out to every product path.

- Tier: tier 0 (customer-facing critical path)
- Owning team: storefront (pager PD-STOREFRONT, Slack #astroshop-storefront)
- Language / runtime: TypeScript (Next.js)
- Running here: yes — port 8080, HTTP, memory limit 250M
- Docker healthcheck: TCP connect to :8080 (node one-liner)
- Depends on: ad, cart, checkout, currency, product-catalog, quote, recommendation, shipping, image-provider
- Called by: frontend-proxy
- Blast radius if it fails: frontend-proxy
- SLO: availability 99.9%, p95 latency 800 ms, p99 latency 1500 ms over 28 days
- Key metrics: http_server_request_duration_seconds, nodejs_eventloop_delay_p99_seconds, v8js_memory_heap_used_bytes
- Injectable faults: imageSlowLoad

**frontend fault `imageSlowLoad`** — Product images take seconds to load; page render blocks on them. What it looks like: frontend duration_p95 up, downstream services healthy, image-provider latency up.

## frontend-proxy

**frontend-proxy** — Edge proxy. All external traffic and the flagd-ui and telemetry-docs routes pass through it.

- Tier: tier 0 (customer-facing critical path)
- Owning team: storefront (pager PD-STOREFRONT, Slack #astroshop-storefront)
- Language / runtime: C++ (Envoy)
- Running here: yes — port 8080, HTTP, memory limit 90M
- Docker healthcheck: TCP connect to :8080 (bash /dev/tcp)
- Depends on: frontend
- Called by: no other business service
- SLO: availability 99.95%, p95 latency 900 ms over 28 days
- Key metrics: httpcheck_status, httpcheck_duration_milliseconds
- Injectable faults: none of its own; failures here come from dependencies or infrastructure
- Deployment note: The only business service published on a fixed host port: http://localhost:8080. Envoy admin on :10000.

## cart

**cart** — Holds the customer's cart. Backed by valkey-cart (Redis-compatible). Health-checked with grpc_health_probe; a failing check marks it unhealthy but never restarts it.

- Tier: tier 0 (customer-facing critical path)
- Owning team: checkout (pager PD-CHECKOUT, Slack #astroshop-checkout)
- Language / runtime: C# (.NET)
- Running here: yes — port 7070, gRPC, memory limit 160M
- Docker healthcheck: grpc_health_probe on :7070
- Depends on: valkey-cart
- Called by: checkout, frontend
- Blast radius if it fails: checkout, frontend, frontend-proxy
- SLO: availability 99.9%, p95 latency 150 ms over 28 days
- Key metrics: http_server_request_duration_seconds, demo_cart_add_item_latency_seconds, dotnet_exceptions_total
- Injectable faults: cartFailure, failedReadinessProbe
- Deployment note: Talks to the shared `redis` container on logical DB 1 (VALKEY_ADDR=redis:6379,defaultDatabase=1), not to valkey-cart.

**cart fault `cartFailure`** — AddItem and GetCart fail for a configurable percentage of calls. What it looks like: cart error_rate up by the configured fraction; checkout error_rate up in step; valkey-cart healthy.

**cart fault `failedReadinessProbe`** — Readiness probe fails; orchestrator takes the pod out of rotation while the process keeps running. What it looks like: cart request_rate drops to zero with no error spike; "no healthy upstream" from the proxy.

## checkout

**checkout** — The order pipeline. Gets the cart, prices each item (product-catalog + currency), quotes shipping, charges payment, books shipping, empties the cart, emails a confirmation, then publishes the order to Kafka.

- Tier: tier 0 (customer-facing critical path)
- Owning team: checkout (pager PD-CHECKOUT, Slack #astroshop-checkout)
- Language / runtime: Go
- Running here: yes — port 5050, gRPC, memory limit 20M
- Docker healthcheck: grpc_health_probe on :5050
- Depends on: cart, currency, email, payment, product-catalog, shipping
- Called by: frontend
- Blast radius if it fails: frontend, frontend-proxy
- SLO: availability 99.5%, p95 latency 1200 ms, p99 latency 2500 ms over 28 days
- Key metrics: rpc_server_call_duration_seconds, rpc_client_call_duration_seconds, go_goroutine_count
- Injectable faults: none of its own; failures here come from dependencies or infrastructure
- Deployment note: Produces every placed order to the Kafka `orders` topic (KAFKA_ADDR from compose.full.yaml); starts only once kafka is healthy.

Checkout has no fault flag of its own. Every checkout incident is a downstream failure surfacing here — which makes it the classic "the alert fires on checkout but the cause is three hops away" case the RCA agent exists for.

## payment

**payment** — Charges the card. Pure compute stub, no real PSP. The last synchronous call before the order is confirmed.

- Tier: tier 0 (customer-facing critical path)
- Owning team: checkout (pager PD-CHECKOUT, Slack #astroshop-checkout)
- Language / runtime: JavaScript (Node.js)
- Running here: yes — port 50051, gRPC, memory limit 140M
- Docker healthcheck: TCP connect to :50051 (node one-liner)
- Depends on: nothing (leaf service)
- Called by: checkout
- Blast radius if it fails: checkout, frontend, frontend-proxy
- SLO: availability 99.9%, p95 latency 200 ms over 28 days
- Key metrics: demo_payment_transactions_total, nodejs_eventloop_delay_p99_seconds
- Injectable faults: paymentFailure, paymentUnreachable

**payment fault `paymentFailure`** — charge() throws for n% of requests. What it looks like: payment error_rate = n% (span status ERROR on charge); checkout error_rate up in step; no latency change.

**payment fault `paymentUnreachable`** — The service refuses all connections. What it looks like: checkout sees connection-refused to payment; payment scrape target down; 100% checkout failure.

## shipping

**shipping** — Quotes a shipping cost and books the shipment. Called by checkout.

- Tier: tier 1 (supporting)
- Owning team: checkout (pager PD-CHECKOUT, Slack #astroshop-checkout)
- Language / runtime: Rust
- Running here: yes — port 50050, HTTP, memory limit 20M
- Docker healthcheck: /app/healthcheck binary
- Depends on: quote
- Called by: checkout, frontend
- Blast radius if it fails: checkout, frontend, frontend-proxy
- SLO: availability 99.5%, p95 latency 500 ms over 28 days
- Key metrics: http_server_request_duration_seconds, demo_shipping_items_shipped_total
- Injectable faults: intlShippingSlowdown

**shipping fault `intlShippingSlowdown`** — International destinations get a multi-second artificial delay. What it looks like: shipping duration_p99 up only for international orders; checkout p99 up; error rates flat.

## currency

**currency** — Converts prices between currencies. Called by frontend and checkout. Pure compute, no dependencies.

- Tier: tier 1 (supporting)
- Owning team: checkout (pager PD-CHECKOUT, Slack #astroshop-checkout)
- Language / runtime: C++
- Running here: yes — port 7001, gRPC, memory limit 20M
- Docker healthcheck: nc -z localhost 7001
- Depends on: nothing (leaf service)
- Called by: checkout, frontend
- Blast radius if it fails: checkout, frontend, frontend-proxy
- SLO: availability 99.95%, p95 latency 50 ms over 28 days
- Key metrics: demo_exchange_conversions_counter_total, container_cpu_utilization_ratio
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## email

**email** — Sends the order-confirmation email. Called by checkout after the order is committed, best-effort.

- Tier: tier 2 (async / back office)
- Owning team: checkout (pager PD-CHECKOUT, Slack #astroshop-checkout)
- Language / runtime: Ruby
- Running here: yes — port 6060, HTTP, memory limit 100M
- Docker healthcheck: TCP connect to :6060 (ruby one-liner)
- Depends on: nothing (leaf service)
- Called by: checkout
- Blast radius if it fails: checkout, frontend, frontend-proxy
- SLO: availability 99%, p95 latency 1000 ms over 28 days
- Key metrics: demo_notification_confirmations_total, container_memory_usage_total_bytes
- Injectable faults: emailMemoryLeak

**email fault `emailMemoryLeak`** — RSS climbs steadily until the runtime OOMs and restarts. What it looks like: email container_memory_usage_total_bytes ramps linearly toward its 100M limit; OOM restarts; checkout unaffected (call is best-effort).

## product-catalog

**product-catalog** — The product database. Shared dependency of frontend and recommendation. Backed by astronomy-db.

- Tier: tier 0 (customer-facing critical path)
- Owning team: catalog (pager PD-CATALOG, Slack #astroshop-catalog)
- Language / runtime: Go
- Running here: yes — port 3550, gRPC, memory limit 20M
- Docker healthcheck: grpc_health_probe on :3550
- Depends on: astronomy-db
- Called by: checkout, frontend, recommendation
- Blast radius if it fails: checkout, frontend, recommendation, frontend-proxy
- SLO: availability 99.9%, p95 latency 100 ms over 28 days
- Key metrics: rpc_server_call_duration_seconds, db_client_operation_duration_seconds, db_sql_connection_open
- Injectable faults: productCatalogFailure
- Deployment note: Reads the `astronomy_db` database on the shared `postgres` container (POSTGRES_HOST=postgres), not astronomy-db.

**product-catalog fault `productCatalogFailure`** — GetProduct fails for one specific product id; ListProducts is unaffected. What it looks like: product-catalog error_rate low but non-zero; errors correlate with one product; frontend PDP errors for that product only.

## recommendation

**recommendation** — Returns "you may also like" products. Calls product-catalog. Has an in-process cache.

- Tier: tier 1 (supporting)
- Owning team: catalog (pager PD-CATALOG, Slack #astroshop-catalog)
- Language / runtime: Python
- Running here: yes — port 9001, gRPC, memory limit 500M
- Docker healthcheck: nc -z localhost 9001
- Depends on: product-catalog
- Called by: frontend
- Blast radius if it fails: frontend, frontend-proxy
- SLO: availability 99%, p95 latency 300 ms over 28 days
- Key metrics: demo_recommendation_requests_recommendations_total, process_memory_usage_bytes, process_cpu_utilization_ratio
- Injectable faults: recommendationCacheFailure
- Deployment note: Memory limit is deliberately high so the recommendationCacheFailure leak has room to show before an OOM kill.

**recommendation fault `recommendationCacheFailure`** — The cache never hits, so every request fans out to product-catalog; memory also grows unbounded. What it looks like: product-catalog request_rate up sharply (every recommendation fans out); recommendation process_memory_usage_bytes climbing.

## ad

**ad** — Serves a contextual ad on the product page. Leaf service under frontend.

- Tier: tier 1 (supporting)
- Owning team: catalog (pager PD-CATALOG, Slack #astroshop-catalog)
- Language / runtime: Java
- Running here: yes — port 9555, gRPC, memory limit 300M
- Docker healthcheck: TCP connect to :9555 (bash /dev/tcp)
- Depends on: nothing (leaf service)
- Called by: frontend
- Blast radius if it fails: frontend, frontend-proxy
- SLO: availability 99%, p95 latency 250 ms over 28 days
- Key metrics: rpc_server_duration_milliseconds, jvm_gc_duration_seconds, jvm_cpu_recent_utilization_ratio
- Injectable faults: adFailure, adHighCpu, adManualGc
- Deployment note: Also exposes Prometheus-format JVM metrics on :9465, scraped by the collector every 10s.

**ad fault `adFailure`** — GetAds throws for every request. What it looks like: ad error_rate 100%; frontend degrades gracefully (ad slot empty), no cascade past frontend.

**ad fault `adHighCpu`** — A background thread pins CPU. What it looks like: ad jvm_cpu_recent_utilization_ratio saturated; ad duration_p95 up; error rate flat; may not alert if only latency-gated.

**ad fault `adManualGc`** — Forced full GC pauses on a loop. What it looks like: ad jvm_gc_duration_seconds spikes periodically; sawtooth latency; correlates with GC, not load.

## quote

**quote** — Computes a shipping quote (weight/size -> price). Called by shipping.

- Tier: tier 1 (supporting)
- Owning team: catalog (pager PD-CATALOG, Slack #astroshop-catalog)
- Language / runtime: PHP
- Running here: yes — port 8090, HTTP, memory limit 40M
- Docker healthcheck: TCP connect to :8090 (php fsockopen)
- Depends on: nothing (leaf service)
- Called by: frontend, shipping
- Blast radius if it fails: frontend, shipping, checkout, frontend-proxy
- SLO: availability 99.5%, p95 latency 200 ms over 28 days
- Key metrics: quotes_total, container_cpu_utilization_ratio
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## image-provider

**image-provider** — Static image host for product photos. Served through frontend-proxy.

- Tier: tier 1 (supporting)
- Owning team: storefront (pager PD-STOREFRONT, Slack #astroshop-storefront)
- Language / runtime: Nginx
- Running here: yes — port 8081, HTTP, memory limit 120M
- Docker healthcheck: wget spider on :8081
- Depends on: nothing (leaf service)
- Called by: frontend
- Blast radius if it fails: frontend, frontend-proxy
- SLO: availability 99.9%, p95 latency 100 ms over 28 days
- Key metrics: nginx_requests_total, nginx_connections_current
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## accounting

**accounting** — Consumes the orders topic from Kafka and writes ledger entries. Fully asynchronous.

- Tier: tier 2 (async / back office)
- Owning team: data (pager PD-DATA, Slack #astroshop-data)
- Language / runtime: C# (.NET)
- Running here: yes — Kafka consumer, memory limit 160M
- Docker healthcheck: none
- Depends on: kafka
- Called by: no other business service
- SLO: availability 99%, freshness lag s 60 s over 28 days
- Key metrics: kafka_consumer_group_lag_ratio, db_client_operation_duration_seconds
- Injectable faults: none of its own; failures here come from dependencies or infrastructure
- Deployment note: From compose.full.yaml. No listening port; consumes `orders` and writes each order into astronomy_db on the shared postgres (joins the `infra` network for it).

## fraud-detection

**fraud-detection** — Consumes the orders topic and scores each order for fraud. Asynchronous, does not gate checkout.

- Tier: tier 2 (async / back office)
- Owning team: data (pager PD-DATA, Slack #astroshop-data)
- Language / runtime: Kotlin (JVM)
- Running here: yes — Kafka consumer, memory limit 300M
- Docker healthcheck: none
- Depends on: kafka
- Called by: no other business service
- SLO: availability 99%, freshness lag s 120 s over 28 days
- Key metrics: kafka_consumer_group_lag_ratio, jvm_memory_used_bytes
- Injectable faults: none of its own; failures here come from dependencies or infrastructure
- Deployment note: From compose.full.yaml. No listening port; consumes `orders` and scores each one.

## kafka

**kafka** — The order event bus. checkout produces to it; accounting and fraud-detection consume.

- Tier: tier 1 (supporting)
- Owning team: data (pager PD-DATA, Slack #astroshop-data)
- Language / runtime: Java
- Running here: yes — port 9092, Kafka, memory limit 620M
- Docker healthcheck: nc -z kafka 9092
- Depends on: nothing (leaf service)
- Called by: accounting, fraud-detection
- Blast radius if it fails: accounting, fraud-detection
- SLO: availability 99.9%, produce p95 latency 100 ms over 28 days
- Key metrics: kafka_request_time_99p_milliseconds, kafka_message_count_total, kafka_request_failed_total
- Injectable faults: kafkaQueueProblems
- Deployment note: From compose.full.yaml. Single KRaft broker (no ZooKeeper), heap fixed at 400MB, topic `orders`.

**kafka fault `kafkaQueueProblems`** — Broker is overloaded and consumers are given an added delay, so lag spikes. What it looks like: kafka_consumer_group_lag_ratio climbing for the accounting and fraud-detection groups; checkout produce latency up; storefront unaffected.

## valkey-cart

**valkey-cart** — In-memory store behind cart. Redis wire protocol.

- Tier: tier 1 (supporting)
- Owning team: platform-eng (pager PD-PLATFORM, Slack #astroshop-platform)
- Language / runtime: C (Valkey)
- Running here: no — Disabled by compose.extras.yaml (profile disabled-by-sentinel-rag). Its role is played by the shared, always-on `redis` container, logical DB 1.
- Depends on: nothing (leaf service)
- Called by: cart
- Blast radius if it fails: cart, checkout, frontend, frontend-proxy
- SLO: availability 99.95%, p95 latency 5 ms over 28 days
- Key metrics: redis_memory_used_bytes, redis_commands_processed_total
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## astronomy-db

**astronomy-db** — Relational store behind product-catalog.

- Tier: tier 1 (supporting)
- Owning team: platform-eng (pager PD-PLATFORM, Slack #astroshop-platform)
- Language / runtime: SQL (PostgreSQL)
- Running here: no — Disabled by compose.extras.yaml (profile disabled-by-sentinel-rag). Its role is played by the `astronomy_db` database inside the shared, always-on `postgres` container.
- Depends on: nothing (leaf service)
- Called by: product-catalog
- Blast radius if it fails: product-catalog, checkout, frontend, recommendation, frontend-proxy
- SLO: availability 99.9%, p95 latency 20 ms over 28 days
- Key metrics: postgresql_backends, postgresql_commits_total
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## otel-collector

**otel-collector** — Receives OTLP from every service, batches, and exports to Prometheus and Jaeger. Excluded from the health rollup by design.

- Tier: tier 1 (supporting)
- Owning team: platform-eng (pager PD-PLATFORM, Slack #astroshop-platform)
- Language / runtime: Go
- Running here: yes — port 4317, OTLP gRPC (4317) and OTLP HTTP (4318), memory limit 400M
- Docker healthcheck: none
- Depends on: nothing (leaf service)
- Called by: no other business service
- SLO: availability 99.95% over 28 days
- Key metrics: otelcol_receiver_accepted_spans_total, otelcol_exporter_sent_spans_total
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## flagd

**flagd** — Feature-flag daemon. Reads demo.flagd.json and serves flag evaluations. The fault-injection control plane.

- Tier: tier 1 (supporting)
- Owning team: platform-eng (pager PD-PLATFORM, Slack #astroshop-platform)
- Language / runtime: Go
- Running here: yes — port 8013, gRPC flag evaluation (8013) and OFREP HTTP (8016), memory limit 75M
- Docker healthcheck: none
- Depends on: nothing (leaf service)
- Called by: no other business service
- SLO: availability 99.95% over 28 days
- Key metrics: feature_flag_evaluation_requests_total
- Injectable faults: none of its own; failures here come from dependencies or infrastructure

## cart health check versus cart errors

cart declares `grpc_health_probe` on :7070. That probe answers the gRPC health RPC, not AddItem or GetCart, so under cartFailure the container stays healthy while real calls fail — cart's error ratio is the signal, not its container health. A hung process does fail the probe and turns red in system-monitor, but is never restarted automatically: Docker does not restart on a failing healthcheck and the restart policy is `no`.
