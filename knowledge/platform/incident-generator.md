# Incident generator

The incident-generator is a small FastAPI service Sentinel adds to the platform. It turns real OpenTelemetry Demo failure modes on and off as named scenarios, and writes an incident log that is the ground truth for what was injected and when. The load-generator supplies "real users calling real services"; the incident-generator supplies "and sometimes it breaks".

## Where it runs

Container `incident-generator` in the astroshop Compose project (added by compose.extras.yaml), built from `app/incident-generator/`, Python 3.12 + uvicorn on container port 8000, published on **host port 8092**. It mounts the staged flagd directory (`~/.local/share/sentinel-rag/runtime/flagd` → /etc/flagd) and its data directory (`~/.local/share/sentinel-rag/app/incident-generator-data` → /app/data). It starts after flagd.

## How it injects a fault

It edits the same `demo.flagd.json` that flagd watches: sets the scenario's flag `defaultVariant` to the fault variant, waits `duration_s` seconds, then sets it back to the reset variant. Writes go to a temp file and are renamed into place, so flagd never reads half-written JSON. No flagd API and no restart are involved.

## Scenarios

| Scenario | Flag | Fault variant | Duration | What to expect |
|---|---|---|---|---|
| payment_degraded | paymentFailure | 50% | 180s | checkout depends on payment (service_healthy) — expect checkout and frontend to degrade |
| payment_outage | paymentUnreachable | on | 120s | harder failure than payment_degraded — payment is fully unreachable, not just flaky |
| cart_outage | cartFailure | 100% | 150s | cart keeps passing grpc_health_probe while AddItem/GetCart fail — expect a green health tree and a red checkout error ratio |
| ad_service_down | adFailure | on | 150s | ad is a leaf under frontend — isolated failure, should not cascade past frontend |
| ad_high_cpu | adHighCpu | on | 200s | resource exhaustion rather than a hard failure — may show as slow, not down |
| recommendation_cache_failure | recommendationCacheFailure | on | 150s | recommendation depends on product-catalog — watch whether the fault stays isolated |
| product_catalog_failure | productCatalogFailure | on | 150s | product-catalog is a shared dependency of both frontend and recommendation — good multi-parent test |
| shipping_slowdown | intlShippingSlowdown | 10sec | 200s | latency injection, not a hard failure — healthchecks may stay green while checkout gets slow |
| email_memory_leak | emailMemoryLeak | 1000x | 240s | degrades gradually — a good case for noticing a trend before the healthcheck flips |

9 scenarios in total, defined in `app/incident-generator/scenarios.yaml`. Every scenario resets its flag to `off`. Flags with no scenario (adManualGc, failedReadinessProbe, imageSlowLoad, kafkaQueueProblems) can still be flipped by hand in flagd-ui.

## Triggering

Triggering is **manual only**. The chat UI's platform panel calls `POST /trigger/{name}`. An earlier random scheduler was disabled: incidents firing on their own made "why did checkout just fail?" ambiguous while someone was driving a scenario from the UI.

Only one scenario runs at a time. Triggering while one is active returns HTTP 409 ("scenario already running"); an unknown name returns 404.

## API

| Method and path | Returns |
|---|---|
| GET /scenarios | Every scenario definition |
| GET /status | `active` (the running scenario, or null) and `recent` (up to 10 finished runs with start and end timestamps) |
| POST /trigger/{name} | Starts the scenario in the background: `{"triggered": name}` |
| GET /incidents?limit=50 | Raw incident-log events, newest last |

## The incident log

`incidents.jsonl` has one JSON line per state change: `ts` (Unix time), `event` (`incident_start` or `incident_end`), `scenario`, `flag`, `fault_variant`, `note`. A *run* is an incident_start plus its incident_end. Only the newest **5 runs** are kept (whole runs are dropped, never single lines; an active run is never dropped) — it is a live status feed, not an audit log. It survives container restarts because it lives on the host.

## Crash safety

On startup it resets every scenario's flag to its reset variant. A crash or rebuild mid-incident would otherwise leave a fault switched on forever, since the timer that would have switched it off died with the process. Reset targets come from scenarios.yaml, not from the file's current state.

## How Sentinel uses it

Sentinel's chat routes "is an incident running?"-style questions straight to `/incidents` (no LLM involved), and the RCA agent compares its findings against the log. Setting: `incident_log_url`, default http://localhost:8092.

`make capture-incident S=<scenario>` uses it to build agent evaluation cases from real incidents: it triggers the scenario, records every RCA tool's live output mid-incident, runs the live agent, and stores the case with the flag the generator flipped as ground truth.
