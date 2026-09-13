# Operations runbook

What to look at when the system misbehaves. Ordered by how often each occurs.

---

## Answers are wrong or unhelpful

**First: is it retrieval or generation?** This is the fork that determines everything else, and it is why chunk ids and scores are logged.

Find the trace and read the `retrieval` log line — it carries `query_hash`, `mode`, `candidates`, `returned` and `top_score`.

| Symptom | Cause | Action |
|---|---|---|
| `returned: 0` | nothing passed the relevance floor | check `min_rerank_score` is calibrated for the active reranker — floors are not portable between them |
| `mode` is not `full` | degraded retrieval | check `DEGRADED_MODE` counters; embedding service or hybrid search is failing |
| right chunks returned, wrong answer | generation | check the prompt and the model; retrieval is fine |
| wrong chunks returned | retrieval | continue below |

**If retrieval is at fault**, run the golden set: `make eval`. Compare recall@5 against recall@50.

- Gap is large → ranking problem. Check the reranker is actually running (`rerank` appears in `latency_ms`) and that its floor is not rejecting good results.
- Both are low → the answer is not in the candidate pool. Check the document is indexed at all (`store.chunk_ids_for_doc`), then check parsing quality for that document.

---

## Everything got worse after a deploy

**Check `index_version` first.** A mismatch between the version stamped on chunks and the version the query filters on produces empty or degraded results.

```bash
python -c "from sentinel.core.config import settings; print(settings.index_version)"
```

Any change to chunk strategy, chunk size, overlap, contextual chunking, embedding provider, model or dimensions changes the fingerprint. If it changed, the index needs rebuilding:

```bash
make reindex
```

This is the most common cause of a sudden quality regression, and it is deliberately loud rather than silent — a version mismatch returns nothing instead of returning subtly wrong results.

---

## Retrieval is slow

Read the per-stage `latency_ms` on the `retrieval` log line rather than the total.

| Stage | Typical | If high |
|---|---|---|
| `embed` | 20–80ms | local model on CPU under load; consider a hosted embedder or a GPU |
| `search` | 30–150ms | `ef_search` too high, or filters are unselective; check the index has GIN/keyword indexes on ACL fields |
| `rerank` | 50–200ms | candidate pool too wide — check `retrieve_k`; Bedrock chunks at 100 sources per call |

If total latency is fine but p99 is bad, look at cost per turn too: a pathological query retrieving forty chunks shows up in both.

---

## Costs are rising

```
sentinel_cost_usd{stage=...}
```

Alert on **p99 cost per turn**, not daily spend. Daily spend catches the problem a day late; p99 per turn catches the pathological query while it is still one query.

Common causes, in order:

1. **Contextual chunking enabled on a large ingest.** One LLM call per chunk. Check `CONTEXTUAL_CHUNKING`.
2. **Cloud parsing not escalating properly.** Check the `parse_route` distribution in the `ingestion_complete` log line — if almost everything is `cloud`, the quality threshold is too high for this corpus.
3. **The cheap model is not being used for routing.** `supervisor._router()` should resolve to `cheap=True`. Routing is the highest-volume call in the system.
4. **Agent loops.** Check `sentinel_node_errors_total` and the `repeated_calls` metric in agent evals. `Budget.record_call` raises on identical repeated calls, so a loop should terminate — if costs are high without loops, the step cap may be too generous.

---

## An ACL violation fired

```
log.critical("acl_violation", chunk_id=..., doc_roles=[...], caller_roles=[...])
```

**This should never happen.** It means a chunk passed the store's filter that should not have. Treat as a security incident.

1. Identify the chunk and its `allowed_roles`.
2. Determine whether the chunk is mis-tagged (an ACL rule matched wrongly at ingest) or the filter is broken (an adapter bug).
3. If mis-tagged: fix `config/acl_rules.yaml` and re-ingest that document. The tags are applied at ingest, so a rule change requires re-ingestion to take effect.
4. If the filter is broken: this affects every query. Consider `KILL_SWITCH=true` while investigating.
5. Add a regression case to `tests/test_acl.py` before closing.

---

## Ingestion failed or silently skipped documents

Check the dead-letter queue:

```bash
cat data/dlq.jsonl | jq -r '[.path, .stage, .error] | @tsv'
```

A job that "succeeds" while quietly dropping 12% of the corpus is the failure nobody notices, which is why parse failures are recorded rather than skipped.

| Error | Meaning |
|---|---|
| `ParseFailure` | the parser could not handle the document; check format and whether cloud escalation was available |
| `ProviderUnavailable` | missing SDK or credential — run `make doctor` |
| `RateLimited` | retries exhausted; lower ingestion concurrency |

Also check `no_chunks_produced` warnings: the document parsed but produced nothing, usually a scan with no OCR available.

---

## An investigation is stuck

```bash
curl localhost:8080/v1/investigations/INC-2607
```

| `outcome` | Meaning | Action |
|---|---|---|
| `running` with `awaiting_decision: true` | waiting for a human | someone needs to approve, edit or reject |
| `running` for a long time | a specialist is hanging | check `NODE_LATENCY` and breaker state |
| `invalid` | the alert payload was malformed | check the alerting system's field names |
| `disabled` | kill switch active | check `KILL_SWITCH` |
| `inconclusive` | no evidence gathered | all specialists degraded — check dependency health |
| `publish_failed` | approved but the write failed | the draft is still in state; retry the decision endpoint |

An interrupted run survives a restart **only if the checkpointer is Postgres**. If `checkpointer_fallback` appears in the logs with `kind=memory`, interrupted runs are lost on restart — fix the database connection.

---

## A circuit breaker is open

```
log.error("breaker_opened", service=..., failures=...)
```

Expected behaviour when a dependency is genuinely down. The breaker probes once after the cooldown.

An open breaker is itself useful information in an operations assistant: "I cannot reach Prometheus, which may itself indicate a problem" is a better answer than a generic error and better than a thirty-second wait for the same conclusion.

If a breaker opens repeatedly without the dependency being down, `breaker_threshold` may be too low for normal error rates, or the timeout too tight.

---

## Kill switch

```bash
KILL_SWITCH=true
```

Stops all agent execution at `intake_node` without a deploy. Worth knowing about before you need it, because the moment you need it you will not want to wait for CI.

Retrieval and ingestion are unaffected — this stops the agent graph only.

---

## Useful queries

```promql
# retrieval falling back
rate(sentinel_degraded_total[5m])

# cost distribution, not total
histogram_quantile(0.99, rate(sentinel_cost_usd_bucket[1h]))

# is the reranker actually running
rate(sentinel_stage_duration_seconds_count{stage="rerank"}[5m])

# cache effectiveness by layer
rate(sentinel_cache_events_total{event="hit"}[5m])
  / rate(sentinel_cache_events_total[5m])

# parse quality distribution — a leftward shift means corpus drift
histogram_quantile(0.5, rate(sentinel_parse_quality_bucket[1h]))
```
