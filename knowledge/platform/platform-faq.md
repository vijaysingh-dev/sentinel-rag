# Astroshop platform FAQ

Short answers to common questions about the platform itself.

## What services exist?

20 services: frontend, frontend-proxy, cart, checkout, payment, shipping, currency, email, product-catalog, recommendation, ad, quote, image-provider, accounting, fraud-detection, kafka, valkey-cart, astronomy-db, otel-collector, flagd. 18 run under `make demo-up`; valkey-cart, astronomy-db do not. See the service catalog for each one.

## How many services are there?

20 in the platform model (18 business services plus otel-collector and flagd); 18 are running in this deployment. Counting every container in the astroshop project, including Jaeger, Prometheus, Grafana, OpenSearch, the load generator and Sentinel's sidecars, is closer to 25.

## Which services are critical (tier 0)?

frontend, frontend-proxy, cart, checkout, payment, product-catalog.

## What languages are the services written in?

frontend: TypeScript; frontend-proxy: C++; cart: C#; checkout: Go; payment: JavaScript; shipping: Rust; currency: C++; email: Ruby; product-catalog: Go; recommendation: Python; ad: Java; quote: PHP; image-provider: Nginx; accounting: C#; fraud-detection: Kotlin; kafka: Java; valkey-cart: C; astronomy-db: SQL; otel-collector: Go; flagd: Go.

## What is the entry point?

frontend-proxy (Envoy) on http://localhost:8080. It routes to frontend and to the UIs for Jaeger, Grafana, flagd-ui and telemetry-docs.

## What does checkout call?

In order: cart (GetCart), product-catalog and currency for each item, shipping for a quote (shipping calls quote), payment (Charge), shipping again (ShipOrder), cart (EmptyCart), email, then Kafka. Everything up to ShipOrder is synchronous and fails the order.

## Which service is the most shared dependency?

product-catalog: frontend, checkout and recommendation all call it.

## Where does cart store data?

In the shared redis container, logical DB 1. Upstream's valkey-cart is disabled here.

## Where does product-catalog store data?

In the astronomy_db database on the shared postgres container. Upstream's astronomy-db is disabled here.

## Is Kafka running?

Yes. `make demo-up` layers in compose.full.yaml, which runs a single Kafka broker plus its two consumers, accounting and fraud-detection. checkout produces every order to the `orders` topic.

## How is tracing set up?

Every service sends OTLP to otel-collector (4317 gRPC / 4318 HTTP); the collector exports traces to Jaeger, viewable at http://localhost:16686/jaeger/ui/.

## Where are metrics stored?

Prometheus at http://localhost:9090. The collector pushes metrics to it over OTLP.

## Where are logs stored?

OpenSearch, index otel-logs. Sentinel's RCA agent does not read logs (ADR-003).

## How are faults injected?

Through flagd feature flags in demo.flagd.json — 13 fault flags. Flip them by hand in flagd-ui (http://localhost:8080/feature) or run a named scenario from the incident-generator.

## What failure modes exist?

adFailure (ad), adHighCpu (ad), adManualGc (ad), cartFailure (cart), emailMemoryLeak (email), failedReadinessProbe (cart), imageSlowLoad (frontend), intlShippingSlowdown (shipping), kafkaQueueProblems (kafka), paymentFailure (payment), paymentUnreachable (payment), productCatalogFailure (product-catalog), recommendationCacheFailure (recommendation).

## What is the incident-generator?

Sentinel's sidecar on host port 8092 that runs one of 9 named fault scenarios on request by editing demo.flagd.json, then resets it after a fixed duration, and logs each start/end to incidents.jsonl.

## What scenarios can the incident-generator run?

payment_degraded, payment_outage, cart_outage, ad_service_down, ad_high_cpu, recommendation_cache_failure, product_catalog_failure, shipping_slowdown, email_memory_leak.

## What is the system-monitor?

Sentinel's sidecar on host port 8091 that rolls Docker healthchecks and redis/postgres pings up a 17-node parent→child tree rooted at astroshop, so one unhealthy leaf shows at every ancestor.

## Why isn't otel-collector or flagd in the health tree?

Every service depends on them; including them would make them everyone's parent and hide the business structure. load-generator is excluded as a test harness.

## Can a service be failing while system-monitor says healthy?

Yes. It reports container health. Partial error rates and injected latency usually keep the healthcheck passing; use Prometheus error ratios and latency for those.

## How do I start and stop the platform?

`make otel-demo-up` (or `make up` then `make demo-up`) to start, `make demo-down` to stop. `make demo-status` prints the health rollup and recent incidents.

## What happens if postgres goes down?

product-catalog cannot read products, so product pages, recommendation and checkout fail; Sentinel's retrieval and chat history also fail.

## What happens if redis goes down?

cart fails, so add-to-cart and checkout fail. Sentinel loses its cache but keeps working.

## Who owns which service?

storefront: frontend, frontend-proxy, image-provider; checkout: checkout, cart, payment, shipping, currency, email; catalog: product-catalog, recommendation, ad, quote; data: accounting, fraud-detection, kafka; platform-eng: otel-collector, flagd, valkey-cart, astronomy-db.

## What generates traffic?

The load-generator (k6), 5 virtual users by default, against frontend-proxy. Controlled by the loadGeneratorTraffic and loadGeneratorVUs flags.
