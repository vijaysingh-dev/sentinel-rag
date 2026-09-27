# Astroshop observability stack

How telemetry is produced, collected and stored, and how Sentinel reads it.

## Signal flow

Every service is instrumented with an OpenTelemetry SDK and sends traces, metrics and logs over **OTLP** (OpenTelemetry Protocol) to the **otel-collector** — gRPC on 4317, HTTP on 4318. Browser spans come in through the proxy at `/otlp-http/`. The collector then exports:

- traces → **Jaeger** (OTLP gRPC to jaeger:4317), and into the span-metrics connector;
- metrics → **Prometheus** (OTLP HTTP push to prometheus:9090/api/v1/otlp);
- logs → **OpenSearch** (index `otel-logs`);
- every signal also to the collector's `debug` exporter.

Grafana reads all three backends for dashboards.

## The OpenTelemetry collector

Besides OTLP, the collector pulls metrics itself with these receivers:

- `docker_stats` — per-container CPU, memory, network and block I/O from the Docker socket;
- `host_metrics` — the host's CPU, memory, disk, filesystem, network, load and processes;
- `http_check/frontend-proxy` — synthetic HTTP probe of the storefront;
- `nginx` — image-provider's stub status every 10s;
- `postgresql` — astronomy_db statistics via the monitoring_user role;
- `redis` — reads INFO from the shared `redis` container every 10s (redirected there from the disabled valkey-cart by otelcol-config-extras.yml);
- `kafkametrics` — broker, topic and consumer-group metrics (including consumer lag) every 10s;
- `prometheus/ad` — scrapes ad's JVM metrics on :9465 every 10s.

Processors: `resource_detection` (env, docker, system), `memory_limiter` (80% limit, 25% spike), span-name normalisation, and a redaction step that deletes card CVVs, masks card numbers to their last four digits and replaces user email with a SHA-256 hash before any trace leaves the collector.

The `span_metrics` connector turns every span into RED metrics (traces_span_metrics_calls_total, traces_span_metrics_duration_milliseconds), which gives one uniform rate/error/duration source across services regardless of language.

The collector sits on two Docker networks (the astroshop network and `infra`), so its OTLP receivers listen on 0.0.0.0 rather than on the address `otel-collector` resolves to. Bound to that name, they listened on the `infra` address only, and every service not itself on `infra` — all but cart, product-catalog and accounting — silently lost its traces and OTLP metrics. If Jaeger lists only a handful of services, check the receivers' bind address first.

The collector itself has no Docker healthcheck, and is excluded from the health rollup: if it stops, telemetry stops arriving but customers are unaffected.

## Prometheus

Prometheus v3 on host port 9090. Metrics are pushed to it over OTLP rather than scraped from each service; its global scrape interval is 60s to match the SDKs' export interval. Resource attributes such as service.name, service.namespace, service.version and container.name are promoted to labels. Out-of-order samples up to 30 minutes are accepted.

Metric families present in this deployment (names follow OpenTelemetry semantic conventions):

| Family | Examples | Source |
|---|---|---|
| RPC server | rpc_server_call_duration_seconds (ad, on the Java agent: rpc_server_duration_milliseconds) | gRPC services |
| HTTP server | http_server_request_duration_seconds, http_server_active_requests | HTTP services |
| Span metrics | traces_span_metrics_calls_total, traces_span_metrics_duration_milliseconds | collector connector |
| Containers | container_cpu_utilization_ratio, container_memory_usage_total_bytes | docker_stats |
| Host | system_cpu_utilization_ratio, system_memory_utilization_ratio | host_metrics |
| PostgreSQL | postgresql_backends, postgresql_commits_total, postgresql_deadlocks_total | postgresql receiver |
| Database client | db_client_operation_duration_seconds, db_sql_connection_open | product-catalog |
| Cart | demo_cart_add_item_latency_seconds, demo_cart_get_cart_latency_seconds | cart |
| .NET / Go runtimes | dotnet_gc_collections_total, go_goroutine_count | cart, Go services |
| Process | process_memory_usage_bytes, process_cpu_time_seconds_total | SDK process metrics |
| nginx | nginx_requests_total | image-provider |
| Redis | redis_memory_used_bytes, redis_commands_processed_total, redis_keyspace_hits_total | redis receiver (shared redis) |
| Kafka broker | kafka_request_time_99p_milliseconds, kafka_message_count_total, kafka_request_failed_total | kafka |
| Kafka consumer lag | kafka_consumer_group_lag_ratio{group="accounting"} (messages behind) | kafkametrics receiver |
| Node.js runtime | nodejs_eventloop_delay_p99_seconds, v8js_memory_heap_used_bytes | frontend, payment |
| Python runtime | cpython_gc_collections_total, process_memory_usage_bytes | recommendation |
| Business counters | demo_payment_transactions_total, demo_shipping_items_shipped_total, quotes_total | per service |
| HTTP check | httpcheck_status, httpcheck_duration_milliseconds | frontend-proxy probe |
| Feature flags | feature_flag_evaluation_requests_total | flagd clients |
| Collector | otelcol_exporter_sent_spans_total, otelcol_exporter_send_failed_* | otel-collector |

## Querying a service's error ratio and latency

The per-language request metrics differ in name and unit, and currency and email emit none, so the uniform RED source is span metrics. For a service SVC:

- error ratio: `sum(rate(traces_span_metrics_calls_total{service_name="SVC", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m])) / sum(rate(traces_span_metrics_calls_total{service_name="SVC", span_kind="SPAN_KIND_SERVER"}[5m]))`
- p95 duration (ms): `histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="SVC", span_kind="SPAN_KIND_SERVER"}[5m])))`

accounting and fraud-detection have no server spans; use `span_kind="SPAN_KIND_CONSUMER"`. The Kafka broker, redis, postgres and the collector emit no spans at all.

## Which label selects a service

| Metric family | Select a service with |
|---|---|
| SDK metrics and span metrics | `service_name="cart"` |
| container_* (docker_stats) | `container_name="cart"` |
| kafka_consumer_group_* | `group="accounting"` or `group="fraud-detection"` |
| redis_*, postgresql_*, nginx_*, httpcheck_* | no service label — one shared target each |

A selector on the wrong label returns an empty result, not an error; an empty graph means "wrong label" far more often than "zero".

## Jaeger

Jaeger v2 (1200M memory limit) stores traces and serves the UI and query API on host port 16686 under base path /jaeger/ui: the UI is http://localhost:16686/jaeger/ui/ and the API http://localhost:16686/jaeger/ui/api/. The base path applies to the API too, so the bare http://localhost:16686/api/ answers with HTML, not JSON. The proxy route http://localhost:8080/jaeger/ui/ reaches the same UI. A trace is the tree of spans for one request; the span with an error status or the one dominating wall-clock time points at the service to investigate.

## OpenSearch and logs

Logs from every service go to OpenSearch (index `otel-logs`, 1G memory limit) and can be explored in Grafana. Sentinel's RCA agent deliberately does not read logs — it works from metrics and traces only (ADR-003).

## Grafana

Grafana at http://localhost:8080/grafana/ with provisioned datasources for Prometheus, Jaeger and OpenSearch and upstream's demo dashboards.

## How Sentinel reads the platform

| Source | Sentinel setting | Used for |
|---|---|---|
| Prometheus | prometheus_url (http://localhost:9090) | PromQL queries through a read-only API, metric-name allowlist, bounded time range |
| Jaeger | jaeger_url (http://localhost:16686/jaeger/ui) | Trace lookups for a service and time window |
| flagd | flagd_config_path (staged demo.flagd.json) | Which fault flags are currently on |
| incident-generator | incident_log_url (http://localhost:8092) | Recent incidents and scenario status |
| system-monitor | system_monitor_url (http://localhost:8091) | Live health rollup |

Live-state tools are never cached: a cached "healthy" is worse than no answer during an incident.
