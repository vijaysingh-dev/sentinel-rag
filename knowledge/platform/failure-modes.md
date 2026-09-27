# Astroshop failure modes and fault injection

Every way the platform is known to fail: the 13 injectable faults built into the OpenTelemetry Demo, the failure classes they belong to, and the infrastructure failures no flag controls.

## How faults are injected

Faults are feature flags served by **flagd** from `demo.flagd.json`. flagd watches that file and reloads on change, so flipping a flag's `defaultVariant` turns a fault on within seconds with no restart. Three things edit the file: flagd-ui (a human, at http://localhost:8080/feature), the incident-generator (a named scenario, logged), and anyone editing the staged file directly. Only the incident-generator writes an incident log, so a fault turned on in flagd-ui is real but unrecorded.

## Every flag

| Flag | Target service | Variants | What it does | incident-generator scenario |
|---|---|---|---|---|
| adFailure | ad | off, on | Fail ad service | ad_service_down (on, 150s) |
| adHighCpu | ad | off, on | Triggers high cpu load in the ad service | ad_high_cpu (on, 200s) |
| adManualGc | ad | off, on | Triggers full manual garbage collections in the ad service | - |
| cartFailure | cart | 10%, 100%, 25%, 50%, 75%, 90%, off | Fail cart service n% of the time | cart_outage (100%, 150s) |
| emailMemoryLeak | email | 10000x, 1000x, 100x, 10x, 1x, off | Memory leak in the email service. | email_memory_leak (1000x, 240s) |
| failedReadinessProbe | cart | off, on | readiness probe failure for cart service | - |
| imageSlowLoad | frontend | 10sec, 5sec, off | slow loading images in the frontend | - |
| intlShippingSlowdown | shipping | 10sec, 5sec, off | Delays international shipping responses to simulate overseas shipping delay | shipping_slowdown (10sec, 200s) |
| kafkaQueueProblems | kafka | off, on | Overloads Kafka queue while simultaneously introducing a consumer side delay leading to a lag spike | - |
| loadGeneratorTraffic | load-generator | off, on | Enable synthetic traffic from the load generator. Turn off to pause all load-generator scenarios. | - |
| loadGeneratorVUs | load-generator | 10, 25, 5, 50 | Number of concurrent virtual users the load generator's HTTP scenario runs. Changes take effect on the wrapper's next poll, restarting k6. | - |
| paymentFailure | payment | 10%, 100%, 25%, 50%, 75%, 90%, off | Fail payment service charge requests n% | payment_degraded (50%, 180s) |
| paymentUnreachable | payment | off, on | Payment service is unavailable | payment_outage (on, 120s) |
| productCatalogFailure | product-catalog | off, on | Fail product catalog service on a specific product | product_catalog_failure (on, 150s) |
| recommendationCacheFailure | recommendation | off, on | Fail recommendation service cache | recommendation_cache_failure (on, 150s) |

All fault flags default to `off`. loadGeneratorTraffic defaults to `on` and loadGeneratorVUs to `5`; those two control traffic, not faults.

## Failure classes

- **Hard failure (errors)** — adFailure, paymentFailure, cartFailure, productCatalogFailure. Error ratio rises on the target and on its callers; latency usually flat.
- **Unreachable** — paymentUnreachable. Connections refused; callers see connection errors and the target's telemetry may stop entirely.
- **Taken out of rotation** — failedReadinessProbe. Traffic to cart drops to zero with no error spike, because the process is fine but receives nothing.
- **Latency injection** — intlShippingSlowdown, imageSlowLoad. Healthchecks stay green; p95/p99 duration rises on the target and on everything upstream of it.
- **Resource exhaustion** — adHighCpu (CPU), emailMemoryLeak and recommendationCacheFailure (memory), adManualGc (GC pauses). Degrades gradually; may never trip an error alert.
- **Backpressure** — kafkaQueueProblems. Consumer lag climbs; customer path unaffected.

## Each fault in detail

### imageSlowLoad (frontend)

Effect: Product images take seconds to load; page render blocks on them.

Signature: frontend duration_p95 up, downstream services healthy, image-provider latency up.

Blast radius: frontend-proxy.

### cartFailure (cart)

Effect: AddItem and GetCart fail for a configurable percentage of calls.

Signature: cart error_rate up by the configured fraction; checkout error_rate up in step; valkey-cart healthy.

Blast radius: checkout, frontend, frontend-proxy.

incident-generator scenario `cart_outage` sets it to `100%` for 150 seconds. cart keeps passing grpc_health_probe while AddItem/GetCart fail — expect a green health tree and a red checkout error ratio

### failedReadinessProbe (cart)

Effect: Readiness probe fails; orchestrator takes the pod out of rotation while the process keeps running.

Signature: cart request_rate drops to zero with no error spike; "no healthy upstream" from the proxy.

Blast radius: checkout, frontend, frontend-proxy.

### paymentFailure (payment)

Effect: charge() throws for n% of requests.

Signature: payment error_rate = n% (span status ERROR on charge); checkout error_rate up in step; no latency change.

Blast radius: checkout, frontend, frontend-proxy.

incident-generator scenario `payment_degraded` sets it to `50%` for 180 seconds. checkout depends on payment (service_healthy) — expect checkout and frontend to degrade

### paymentUnreachable (payment)

Effect: The service refuses all connections.

Signature: checkout sees connection-refused to payment; payment scrape target down; 100% checkout failure.

Blast radius: checkout, frontend, frontend-proxy.

incident-generator scenario `payment_outage` sets it to `on` for 120 seconds. harder failure than payment_degraded — payment is fully unreachable, not just flaky

### intlShippingSlowdown (shipping)

Effect: International destinations get a multi-second artificial delay.

Signature: shipping duration_p99 up only for international orders; checkout p99 up; error rates flat.

Blast radius: checkout, frontend, frontend-proxy.

incident-generator scenario `shipping_slowdown` sets it to `10sec` for 200 seconds. latency injection, not a hard failure — healthchecks may stay green while checkout gets slow

### emailMemoryLeak (email)

Effect: RSS climbs steadily until the runtime OOMs and restarts.

Signature: email container_memory_usage_total_bytes ramps linearly toward its 100M limit; OOM restarts; checkout unaffected (call is best-effort).

Blast radius: checkout, frontend, frontend-proxy.

incident-generator scenario `email_memory_leak` sets it to `1000x` for 240 seconds. degrades gradually — a good case for noticing a trend before the healthcheck flips

### productCatalogFailure (product-catalog)

Effect: GetProduct fails for one specific product id; ListProducts is unaffected.

Signature: product-catalog error_rate low but non-zero; errors correlate with one product; frontend PDP errors for that product only.

Blast radius: checkout, frontend, recommendation, frontend-proxy.

incident-generator scenario `product_catalog_failure` sets it to `on` for 150 seconds. product-catalog is a shared dependency of both frontend and recommendation — good multi-parent test

### recommendationCacheFailure (recommendation)

Effect: The cache never hits, so every request fans out to product-catalog; memory also grows unbounded.

Signature: product-catalog request_rate up sharply (every recommendation fans out); recommendation process_memory_usage_bytes climbing.

Blast radius: frontend, frontend-proxy.

incident-generator scenario `recommendation_cache_failure` sets it to `on` for 150 seconds. recommendation depends on product-catalog — watch whether the fault stays isolated

### adFailure (ad)

Effect: GetAds throws for every request.

Signature: ad error_rate 100%; frontend degrades gracefully (ad slot empty), no cascade past frontend.

Blast radius: frontend, frontend-proxy.

incident-generator scenario `ad_service_down` sets it to `on` for 150 seconds. ad is a leaf under frontend — isolated failure, should not cascade past frontend

### adHighCpu (ad)

Effect: A background thread pins CPU.

Signature: ad jvm_cpu_recent_utilization_ratio saturated; ad duration_p95 up; error rate flat; may not alert if only latency-gated.

Blast radius: frontend, frontend-proxy.

incident-generator scenario `ad_high_cpu` sets it to `on` for 200 seconds. resource exhaustion rather than a hard failure — may show as slow, not down

### adManualGc (ad)

Effect: Forced full GC pauses on a loop.

Signature: ad jvm_gc_duration_seconds spikes periodically; sawtooth latency; correlates with GC, not load.

Blast radius: frontend, frontend-proxy.

### kafkaQueueProblems (kafka)

Effect: Broker is overloaded and consumers are given an added delay, so lag spikes.

Signature: kafka_consumer_group_lag_ratio climbing for the accounting and fraud-detection groups; checkout produce latency up; storefront unaffected.

Blast radius: accounting, fraud-detection.

## Failures no flag controls

- **postgres down** — product-catalog cannot read products; frontend product pages, recommendation and checkout fail. Sentinel's own retrieval and chat history fail too. system-monitor shows astronomy-db unhealthy via its SELECT 1 check.
- **redis down** — cart fails, so add-to-cart and checkout fail; Sentinel loses its cache but keeps working. system-monitor shows redis unhealthy via PING.
- **otel-collector down** — no new metrics, traces or logs; every rate graph flatlines while customers are unaffected. Easy to misread as a total outage.
- **flagd down** — services fall back to flag defaults (faults off); flagd-ui stops working.
- **load-generator stopped or loadGeneratorTraffic off** — request rates fall to zero everywhere at once with no errors. Not a platform outage.
- **OOM kill from a memory limit** — containers restart only if restarted by hand (restart policy `no`), so an OOM leaves the service down and red in the health rollup.
- **Container crash** — the service goes `exited`; system-monitor marks it unhealthy and every ancestor in the health tree turns unhealthy with it.

## Telling failure classes apart

| Symptom | Most likely class |
|---|---|
| Errors up on one service and its callers, latency flat | Hard failure in the deepest erroring service |
| Request rate to one service drops to zero, no errors | Readiness probe / taken out of rotation |
| Request rate drops to zero everywhere | load-generator stopped or collector down |
| p99 up, errors flat, healthchecks green | Latency injection or CPU saturation |
| Memory climbing steadily, periodic restarts | Memory leak |
| Sawtooth latency correlated with GC time | Forced GC (adManualGc) |
| Errors on one product only | productCatalogFailure |
