# Astroshop service dependencies and request flows

How the services call each other, which calls are synchronous, and what fails downstream of each service. Edges are business dependencies only: otel-collector and flagd are used by every service and are left out, because including them would make every service look like it depends on everything.

## Call graph (who calls whom)

| Service | Calls | Called by |
|---|---|---|
| frontend | ad, cart, checkout, currency, product-catalog, quote, recommendation, shipping, image-provider | frontend-proxy |
| frontend-proxy | frontend | - |
| cart | valkey-cart | checkout, frontend |
| checkout | cart, currency, email, payment, product-catalog, shipping | frontend |
| payment | - | checkout |
| shipping | quote | checkout, frontend |
| currency | - | checkout, frontend |
| email | - | checkout |
| product-catalog | astronomy-db | checkout, frontend, recommendation |
| recommendation | product-catalog | frontend |
| ad | - | frontend |
| quote | - | frontend, shipping |
| image-provider | - | frontend |
| accounting | kafka | - |
| fraud-detection | kafka | - |
| kafka | - | accounting, fraud-detection |
| valkey-cart | - | cart |
| astronomy-db | - | product-catalog |
| otel-collector | - | - |
| flagd | - | - |

## Entry point

All external traffic enters through **frontend-proxy** (Envoy) on http://localhost:8080. The proxy routes `/` to frontend, `/images/` to image-provider, `/jaeger/` to Jaeger, `/grafana/` to Grafana, `/feature` to flagd-ui, `/telemetry/` to telemetry-docs, `/flagservice/` to flagd (for in-browser flag evaluation) and `/otlp-http/` to the collector (so browser-side spans reach the same trace).

## Page load flow

Browser → frontend-proxy → frontend. frontend then calls, per page: product-catalog (ListProducts / GetProduct), currency (price conversion), recommendation (which itself calls product-catalog), ad (GetAds), cart (item count), and image-provider serves the photos. A slow or failing call on this fan-out shows up as frontend latency or a partial page; ad failing leaves the ad slot empty rather than breaking the page.

## Checkout flow

Browser → frontend-proxy → frontend → **checkout.PlaceOrder**, which calls, in order:

1. cart — GetCart
2. product-catalog — GetProduct for each item, and currency — Convert each item's price
3. shipping — GetQuote (shipping calls quote to price it), then currency converts the quote
4. payment — Charge the card
5. shipping — ShipOrder
6. cart — EmptyCart (its error is ignored; the order is already charged)
7. email — SendOrderConfirmation (best-effort; failure does not fail the order)
8. kafka — publish to the `orders` topic

Steps 1-5 are synchronous and on the critical path, so an error in cart, product-catalog, currency, shipping, quote or payment surfaces as a checkout error. checkout has no fault flag of its own: when an alert fires on checkout, the cause is almost always one of these downstream calls.

## Asynchronous flow

checkout produces each placed order to Kafka topic `orders` (a single KRaft broker). accounting consumes it and writes the order into astronomy_db; fraud-detection consumes it and scores the order. Both are consumers, so a failure there shows as consumer lag, never as a customer-facing error — but a Kafka broker that stops accepting writes slows or fails checkout's produce call, which *is* on the order path.

## Data stores

- cart → Redis (the shared `redis` container, logical DB 1). Upstream calls it valkey-cart.
- product-catalog → PostgreSQL (the `astronomy_db` database in the shared `postgres` container). Upstream calls it astronomy-db.
- recommendation keeps an in-process cache only.

## Blast radius per service

What else degrades when a service fails (transitive callers, nearest first), and which of those are tier-0.

| Service | Blast radius | Tier-0 services affected |
|---|---|---|
| frontend | frontend-proxy | frontend-proxy |
| frontend-proxy | none | none |
| cart | checkout, frontend, frontend-proxy | checkout, frontend, frontend-proxy |
| checkout | frontend, frontend-proxy | frontend, frontend-proxy |
| payment | checkout, frontend, frontend-proxy | checkout, frontend, frontend-proxy |
| shipping | checkout, frontend, frontend-proxy | checkout, frontend, frontend-proxy |
| currency | checkout, frontend, frontend-proxy | checkout, frontend, frontend-proxy |
| email | checkout, frontend, frontend-proxy | checkout, frontend, frontend-proxy |
| product-catalog | checkout, frontend, recommendation, frontend-proxy | checkout, frontend, frontend-proxy |
| recommendation | frontend, frontend-proxy | frontend, frontend-proxy |
| ad | frontend, frontend-proxy | frontend, frontend-proxy |
| quote | frontend, shipping, checkout, frontend-proxy | frontend, checkout, frontend-proxy |
| image-provider | frontend, frontend-proxy | frontend, frontend-proxy |
| accounting | none | none |
| fraud-detection | none | none |
| kafka | accounting, fraud-detection | none |
| valkey-cart | cart, checkout, frontend, frontend-proxy | cart, checkout, frontend, frontend-proxy |
| astronomy-db | product-catalog, checkout, frontend, recommendation, frontend-proxy | product-catalog, checkout, frontend, frontend-proxy |
| otel-collector | none | none |
| flagd | none | none |

## Shared dependencies

- **product-catalog** is called by frontend, checkout and recommendation — the most shared service; a product-catalog failure shows up in three places at once.
- **cart** is called by frontend and checkout.
- **currency** is called by frontend and checkout.
- **postgres** and **redis** are shared infrastructure: Sentinel itself also uses them, so an outage there affects both Astroshop and Sentinel.

## Call graph versus health tree

The system-monitor health tree is *not* the call graph. It nests each service under the one caller that most conceptually owns it (payment under checkout, ad under frontend), and puts shared services (cart, product-catalog) at the top level under astroshop so each appears exactly once. Use the call graph above to reason about causes; use the health tree to see at a glance which branch is red.
