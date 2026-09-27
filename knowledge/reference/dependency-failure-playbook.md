# Dependency-failure playbook

Astroshop's full "if this is down, what else breaks" reference, one entry per service that has at least one caller. Generated from the dependency graph in `scripts/corpus/services.yaml` — the source of truth is the graph, not this document; regenerate rather than hand-edit if a dependency changes.

Read this top-down when the alert has already fired somewhere and you need to know **where else to look**; read an individual runbook when you already know which service is the suspect and need **its own diagnose/mitigate steps**. This document answers "what does X being down do to everything else"; the runbooks answer "what do I do about X".

## If `ad` is down

Serves a contextual ad on the product page. Leaf service under frontend. Owned by catalog (PD-CATALOG).

**Direct callers:** `frontend`.

**Full blast radius (2 services):** `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `ad` — see that service's own runbook for its specific symptom, and `ad`'s runbook for the actual fix.

## If `astronomy-db` is down

Relational store behind product-catalog. Owned by platform-eng (PD-PLATFORM).

**Direct callers:** `product-catalog`.

**Full blast radius (5 services):** `product-catalog`, `checkout`, `frontend`, `recommendation`, `frontend-proxy`.

**Customer-facing impact: yes**, via `product-catalog`, `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `astronomy-db` — see that service's own runbook for its specific symptom, and `astronomy-db`'s runbook for the actual fix.

## If `cart` is down

Holds the customer's cart. Backed by valkey-cart (Redis-compatible). Health-checked with grpc_health_probe; a failing check marks it unhealthy but never restarts it. Owned by checkout (PD-CHECKOUT).

**Direct callers:** `checkout`, `frontend`.

**Full blast radius (3 services):** `checkout`, `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `cart` — see that service's own runbook for its specific symptom, and `cart`'s runbook for the actual fix.

## If `checkout` is down

The order pipeline. Gets the cart, prices each item (product-catalog + currency), quotes shipping, charges payment, books shipping, empties the cart, emails a confirmation, then publishes the order to Kafka. Owned by checkout (PD-CHECKOUT).

**Direct callers:** `frontend`.

**Full blast radius (2 services):** `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `checkout` — see that service's own runbook for its specific symptom, and `checkout`'s runbook for the actual fix.

## If `currency` is down

Converts prices between currencies. Called by frontend and checkout. Pure compute, no dependencies. Owned by checkout (PD-CHECKOUT).

**Direct callers:** `checkout`, `frontend`.

**Full blast radius (3 services):** `checkout`, `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `currency` — see that service's own runbook for its specific symptom, and `currency`'s runbook for the actual fix.

## If `email` is down

Sends the order-confirmation email. Called by checkout after the order is committed, best-effort. Owned by checkout (PD-CHECKOUT).

**Direct callers:** `checkout`.

**Full blast radius (3 services):** `checkout`, `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `email` — see that service's own runbook for its specific symptom, and `email`'s runbook for the actual fix.

## If `frontend` is down

Serves the web storefront and the JSON API the browser calls. Fans out to every product path. Owned by storefront (PD-STOREFRONT).

**Direct callers:** `frontend-proxy`.

**Full blast radius (1 service):** `frontend-proxy`.

**Customer-facing impact: yes**, via `frontend-proxy`. Expect the first page to land on one of those, not on `frontend` — see that service's own runbook for its specific symptom, and `frontend`'s runbook for the actual fix.

## If `image-provider` is down

Static image host for product photos. Served through frontend-proxy. Owned by storefront (PD-STOREFRONT).

**Direct callers:** `frontend`.

**Full blast radius (2 services):** `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `image-provider` — see that service's own runbook for its specific symptom, and `image-provider`'s runbook for the actual fix.

## If `kafka` is down

The order event bus. checkout produces to it; accounting and fraud-detection consume. Owned by data (PD-DATA).

**Direct callers:** `accounting`, `fraud-detection`.

**Full blast radius (2 services):** `accounting`, `fraud-detection`.

**Customer-facing impact: none directly.** Every service in the blast radius is tier 1/2; the storefront critical path does not run through here.

## If `payment` is down

Charges the card. Pure compute stub, no real PSP. The last synchronous call before the order is confirmed. Owned by checkout (PD-CHECKOUT).

**Direct callers:** `checkout`.

**Full blast radius (3 services):** `checkout`, `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `payment` — see that service's own runbook for its specific symptom, and `payment`'s runbook for the actual fix.

## If `product-catalog` is down

The product database. Shared dependency of frontend and recommendation. Backed by astronomy-db. Owned by catalog (PD-CATALOG).

**Direct callers:** `checkout`, `frontend`, `recommendation`.

**Full blast radius (4 services):** `checkout`, `frontend`, `recommendation`, `frontend-proxy`.

**Customer-facing impact: yes**, via `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `product-catalog` — see that service's own runbook for its specific symptom, and `product-catalog`'s runbook for the actual fix.

## If `quote` is down

Computes a shipping quote (weight/size -> price). Called by shipping. Owned by catalog (PD-CATALOG).

**Direct callers:** `frontend`, `shipping`.

**Full blast radius (4 services):** `frontend`, `shipping`, `checkout`, `frontend-proxy`.

**Customer-facing impact: yes**, via `frontend`, `checkout`, `frontend-proxy`. Expect the first page to land on one of those, not on `quote` — see that service's own runbook for its specific symptom, and `quote`'s runbook for the actual fix.

## If `recommendation` is down

Returns "you may also like" products. Calls product-catalog. Has an in-process cache. Owned by catalog (PD-CATALOG).

**Direct callers:** `frontend`.

**Full blast radius (2 services):** `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `recommendation` — see that service's own runbook for its specific symptom, and `recommendation`'s runbook for the actual fix.

## If `shipping` is down

Quotes a shipping cost and books the shipment. Called by checkout. Owned by checkout (PD-CHECKOUT).

**Direct callers:** `checkout`, `frontend`.

**Full blast radius (3 services):** `checkout`, `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `shipping` — see that service's own runbook for its specific symptom, and `shipping`'s runbook for the actual fix.

## If `valkey-cart` is down

In-memory store behind cart. Redis wire protocol. Owned by platform-eng (PD-PLATFORM).

**Direct callers:** `cart`.

**Full blast radius (4 services):** `cart`, `checkout`, `frontend`, `frontend-proxy`.

**Customer-facing impact: yes**, via `cart`, `checkout`, `frontend`, `frontend-proxy`. Expect the first page to land on one of those, not on `valkey-cart` — see that service's own runbook for its specific symptom, and `valkey-cart`'s runbook for the actual fix.
