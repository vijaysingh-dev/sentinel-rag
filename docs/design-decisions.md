# Design decisions

Architecture decision records. Each one states the decision, the reasoning, what was rejected and why, and what it costs. The costs column is the point: a decision without a stated downside has not been thought through.

---

## ADR-001 — Provider selection is per stage, not global

**Decision.** `Settings` carries five independent provider fields: `parse_provider`, `embed_provider`, `vectorstore_provider`, `rerank_provider`, `llm_provider`.

**Reasoning.** A single `cloud = "azure"` setting is tidier and wrong. The best parser and the best embedder are rarely from the same vendor, and in regulated environments different data classes carry different residency rules — a corpus that cannot leave the building still needs an embedder, and it will be the local one regardless of what the rest of the pipeline uses.

The realistic shape is mixed: parse with Azure Document Intelligence because its layout model is strongest, embed with Bedrock Titan because the account is already there, rerank locally because it is free and fast.

**Rejected.** A global provider setting. Cannot express the above at all.

**Cost.** Five settings to reason about instead of one, and a combinatorial space that no benchmark can fully cover. Mitigated by `make doctor`, which reports what is actually usable, and by the per-stage benchmarks, which let you choose each independently.

---

## ADR-002 — A lazy provider registry rather than direct imports

**Decision.** `core/registry.py` maps `(stage, provider)` to a `"module:Class"` string and imports on first use.

**Reasoning.** Three benefits, in order of importance:

- **The local pipeline runs with neither cloud SDK installed.** boto3 and the Azure libraries are heavy and mutually irrelevant. Top-level imports would make every developer install both.
- **Benchmarks can enumerate providers.** `benchmarks/` iterates the registry, so adding a provider requires no benchmark change. That property only exists because providers are enumerable rather than referenced at import sites.
- **Failures are legible.** A missing SDK surfaces as `ProviderUnavailable: parser/azure needs an uninstalled dependency` at construction, not as an ImportError three modules deep during a twenty-minute ingestion run.

**Rejected.** A dependency injection framework. Obscures control flow for no gain at this size. The registry resolves a name to an object and caches it; anything more is ceremony.

**Cost.** Import errors move from startup to first use, so a typo in a spec string is caught later than it would be otherwise. `available_providers()` exists to probe them all up front.

---

## ADR-003 — Chunks carry two text fields

**Decision.** `Chunk.embed_text` is vectorised; `Chunk.context_text` is handed to the LLM.

**Reasoning.** The retriever and the generator want different things. The retriever wants small single-topic units, because a vector averaging five topics is close to nothing in embedding space. The generator wants enough surrounding context to answer.

A single-text-field chunk model forces a compromise between them and makes three strategies **inexpressible**: sentence-window (embed one sentence, return its neighbours), parent-document (embed children, return the section), and runbook (embed steps, return the whole procedure). Those are not exotic — the runbook case is the single most important chunking decision in an operations corpus.

**Rejected.** One text field plus a separate document store keyed by parent id. Works, but doubles the storage systems and makes the retrieval path do a second lookup that the chunk could have carried.

**Cost.** Larger index rows when the two differ, and a mistake with real consequences: embedding `context_text` instead of `embed_text` silently destroys the precision the split exists to provide. Centralised in `BaseEmbedder.embed_chunks` so there is exactly one place to get it wrong.

---

## ADR-004 — Hybrid parsing with quality-gated escalation

**Decision.** Parse locally, score the result with reference-free heuristics, escalate to a cloud service only below `parse_quality_threshold`.

**Reasoning.** On a mixed corpus, most documents are handled perfectly by a free library and a minority are handled badly. The two obvious policies are both wrong: paying per page for everything wastes money on documents that needed nothing, and using only the local parser silently loses the rest. "Silently" is the operative word — a scanned PDF produces an empty document, not an error.

Escalation costs one wasted local parse for documents that need the cloud, which is milliseconds.

**Rejected.**
- *Always cloud.* Roughly 4× the cost on our corpus mix for a few points of average quality, most of which comes from a minority of documents.
- *Always local.* Loses scans entirely.
- *Route by file type alone.* A native PDF and a scanned PDF share an extension. `_pdf_has_text_layer` probes for a text layer precisely because extension is not enough.

**Cost.** A heuristic gate can misjudge. It is tuned conservatively — it should rarely call a bad parse good, since calling a good parse bad merely costs one cloud call. The threshold is corpus-specific and `benchmarks/parser_bakeoff.py` sweeps it.

---

## ADR-005 — One index with ACL filters, not an index per role

**Decision.** All chunks live in one index. `allowed_roles` and `components` are array fields filtered inside the query.

**Reasoning.** Filters are cheap — GIN-indexed array overlap in Postgres, `search.in()` in OData, `terms` in filter context in OpenSearch, all of which run inside the ANN traversal rather than after it. Queries routinely cross corpora: "how do I restart ICE and has this happened before" spans runbooks and incidents, and separate indexes would require fan-out and manual fusion.

Splitting also multiplies operational surface: N indexes to build, N to re-index on an embedding change, N to keep schema-consistent.

**Rejected.** An index per role or per corpus. The triggers that would justify revisiting this are documented: a corpus exceeding ~10M chunks where filter selectivity degrades HNSW recall, or a hard data-residency requirement that forbids co-location. Neither applies yet.

**Cost.** A filter bug is a data leak rather than an empty result. Mitigated in depth: `require_scope()` fails closed on empty roles, `_assert_authorized()` re-checks returned chunks and raises a page-worthy alert, and `tests/test_acl.py` gates releases on both.

---

## ADR-006 — Security filters go inside the query, never after

**Decision.** Every vector store adapter translates `SearchFilter` into its backend's native filter language and applies it as part of the search.

**Reasoning.** A post-filter has already loaded restricted content into your process. From there a log line, an exception traceback, a metrics label or a debugger session can leak it. The content should never cross the boundary at all.

There is a performance argument too — filtering inside the ANN traversal avoids retrieving and discarding — but the security argument is the one that makes it non-negotiable.

**Rejected.** Retrieve wide, filter in Python. Simpler and uniform across backends, and wrong for the above reason.

**Cost.** Three filter dialects to maintain and test. Accepted, because the alternative is a class of bug that does not announce itself.

---

## ADR-007 — Chunking strategy is chosen per document type

**Decision.** `DOC_TYPE_STRATEGY` routes runbooks to `runbook`, glossaries to `glossary`, everything else to `structural`.

**Reasoning.** A runbook and a glossary want opposite things. A glossary entry is atomic: merging "divisor" and "rebalance" into one 500-token chunk makes both harder to find, because the vector represents an average of two unrelated concepts. A runbook must never be split at retrieval time, because a retrieved half-runbook is worse than none — someone under incident pressure will execute the half.

No single strategy serves both. A global setting means picking which document type to serve badly.

**Rejected.** One tuned global strategy. Simpler, and measurably worse on the document types that matter most operationally.

**Cost.** A routing table to maintain, and per-type benchmarking rather than one number. `benchmarks/chunking_sweep.py` reports per strategy so the table can be justified rather than assumed.

---

## ADR-008 — Structural chunking prefixes the heading path

**Decision.** `embed_text` begins with `Document > Section > Subsection`.

**Reasoning.** This is the highest-return change in the chunking layer and it costs a handful of tokens.

*"Set this to false to skip validation"* is unretrievable — no realistic query lands near it in embedding space. *"ICE Configuration > Validation > strict_mode — Set this to false to skip validation"* is findable by dozens of queries.

In practice the prefix buys more recall than upgrading the embedding model, which is worth knowing before spending money on the latter.

**Rejected.** Contextual chunking as the default (ADR-009), which is strictly better and costs an LLM call per chunk. The prefix captures most of the benefit for free.

**Cost.** Slight dilution: the prefix occupies part of the vector's semantic budget. Measurable, small, and outweighed on every corpus tested.

---

## ADR-009 — Contextual chunking available but off by default

**Decision.** `ContextualChunker` wraps any strategy and prepends an LLM-written situating sentence. `CONTEXTUAL_CHUNKING=false`.

**Reasoning.** It works — it recovers most of the context that splitting destroys, including pronouns, implicit subjects and "as described above". But it costs one LLM call per chunk at ingest.

What makes it affordable at all is prompt caching: the document sits in the static prefix, so after the first chunk the provider charges roughly a tenth for those tokens. Without caching it is impractical at corpus scale.

**Rejected.** On by default. The cost should be a deliberate choice made after measuring the gain on a specific corpus, not a default someone discovers on an invoice.

**Cost.** Off by default means most users never try it. Mitigated by the flag and by `benchmarks/chunking_sweep.py` including it.

---

## ADR-010 — RRF for fusion, not weighted score blending

**Decision.** `reciprocal_rank_fusion` with `k=60` is the default; `weighted_fusion` exists for comparison.

**Reasoning.** Cosine similarity lives in [0,1]; BM25 is unbounded and corpus-dependent. Adding them is meaningless. Normalising per query is unstable because the score distribution shifts with the query — one outlier compresses everything else.

RRF discards scores and uses ranks, so it is scale-free, needs no tuning, works with any number of rankers, and is robust to one arm producing garbage. The `k` constant damps top positions so one ranker's confident-but-wrong first result cannot dominate.

**Rejected.** Weighted fusion as the default. It can beat RRF *with a tuned alpha*, but the right alpha differs between natural-language and identifier-style queries — so a single tuned value underperforms on half the traffic, and untuned RRF usually wins in practice.

**Cost.** Loses score magnitude information. Recovered by the reranker, which reassigns absolute relevance scores anyway.

---

## ADR-011 — Cache knowledge, never telemetry

**Decision.** `core/cache.LIVE_TOOLS` names the tools that read live state. `is_cacheable()` returns false for any turn touching one.

**Reasoning.** One conversation mixes both kinds of question. "What does a stale feed mean?" is stable for months. "Is the SGX feed healthy?" is stable for seconds.

Caching the second turns the assistant into an incident: an engineer asks whether a feed recovered, receives a ten-minute-old "healthy", and stops investigating. That is a worse outcome than the assistant being unavailable.

So cacheability is a property of the **tool**, not of the request or the response.

**Rejected.** TTL-based caching for everything. A short enough TTL to be safe for live state makes the cache useless for knowledge; a long enough TTL to be useful for knowledge is dangerous for live state.

**Cost.** Mixed turns are never cached even though their documentation half is stable. Accepted — the alternative is splitting a turn into cacheable and non-cacheable halves, which is complexity for a modest hit-rate gain.

---

## ADR-012 — Every cache key carries role scope and index version

**Decision.** Cache keys are namespaced by `role_scope(roles)` and `settings.index_version`.

**Reasoning.** Two distinct failures:

- Omitting role scope means one caller can receive another caller's answer. That is not a performance bug, it is a data leak, and it is invisible in testing unless you specifically test cross-role cache hits.
- Omitting index version serves chunks embedded with a different model after a re-index. This presents as a mysterious quality regression rather than an error, which makes it expensive to diagnose.

**Rejected.** A single global cache with a role check on read. Requires loading the cached entry to check it, which is the post-filter problem from ADR-006 in a different costume.

**Cost.** Lower hit rate — callers with different role sets do not share entries. Acceptable; roles cluster into a handful of distinct sets in practice.

---

## ADR-013 — Infrastructure failures raise; semantic failures return

**Decision.** `SentinelError` and its subclasses raise. `ToolResult` with a non-OK `Outcome` returns to the model with an instruction.

**Reasoning.** They need different handlers and conflating them breaks both.

Raising on "no documents matched" kills a conversation the agent could have recovered from by rephrasing the query. Feeding a 503 back to the model as text makes it retry a dead service until the step cap trips, burning budget to learn nothing.

The instruction text matters because the reader is a language model deciding what to do next. `"NO_RESULTS: do not answer from general knowledge"` changes behaviour; `"Error: empty result set"` does not.

**Rejected.** Exceptions everywhere with a catch-all translating to text at the agent boundary. Loses the distinction exactly where it is needed.

**Cost.** Two error paths to keep straight. Mitigated by the `guarded()` decorator, which handles the infrastructure half uniformly.

---

## ADR-014 — Supervisor holds no tools, enforced by a test

**Decision.** `rca/agents/supervisor.py` imports no tool. `tests/test_trajectory.py` asserts this by source inspection.

**Reasoning.** The prompt says so too, but a constraint stated only in a prompt survives until the next person edits that prompt. This is the constraint most likely to erode — someone adds a tool to the supervisor "just for triage" and the isolation argument for the entire multi-agent design quietly collapses.

Source inspection is crude and it is also free, deterministic, and impossible to accidentally satisfy.

**Rejected.** Trusting the prompt. Trusting code review — reviewers approve reasonable-looking changes, and adding one tool to the supervisor looks reasonable.

**Cost.** The test is brittle to refactoring; renaming a tool requires updating it. Cheap relative to what it protects.

---

## ADR-015 — Grounding is a set membership test, not a judgement

**Decision.** `observed_refs()` scans raw `ToolMessage` content for citation handles. Claims citing a ref outside that set are dropped before becoming evidence.

**Reasoning.** This converts "did the model make this up?" from a fuzzy LLM-judged question into a deterministic check. A fabricated reference cannot survive, regardless of how plausible the surrounding claim reads.

It also fails in the right direction: a real claim with a mistyped ref is dropped, which is a false negative and merely loses information. The alternative failure — a fabricated claim with a plausible ref surviving into a published RCA — is the one that matters.

**Rejected.** Leaving it entirely to the verifier. The verifier is an LLM and will sometimes accept a confident fabrication; a set membership test never will.

**Cost.** Requires every tool to emit a `ref` and every prefix to be registered in `REF_PREFIXES`. A tool that forgets produces claims that are all silently dropped, which is a loud enough failure to catch quickly.

---

## ADR-016 — Human approval via `interrupt()`, not a polling loop

**Decision.** `approval_node` calls LangGraph's `interrupt()`. The run resumes via `Command(resume=...)` on the decision endpoint.

**Reasoning.** `interrupt()` persists the entire graph state to the checkpointer and returns control. No worker, no thread, no memory is held while a human takes four hours. The run resumes from the checkpoint, on a different process if necessary.

Polling loops and held connections do not survive a deploy, a pod eviction, or an analyst going to lunch. This is the difference between human-in-the-loop as a production pattern and human-in-the-loop as a demo.

**Rejected.** Storing the draft in a database and starting a fresh run on approval. Loses the accumulated state, so the publish step would have to be reimplemented outside the graph and could no longer be verified by the same trajectory tests.

**Cost.** Requires a durable checkpointer. `rca/db.checkpointer()` falls back to in-memory with a loud warning, because silent in-memory checkpointing works in development and breaks the interrupt in production.

---

## ADR-017 — Degraded confidence is capped in code, not requested in a prompt

**Decision.** `writer_node` clamps `draft.confidence` to `DEGRADED_CONFIDENCE_CAP` after the model returns.

**Reasoning.** The prompt asks for it as well, and the prompt will sometimes be ignored. A partial investigation may be right, but it must not claim to be sure — the analyst needs to see the difference at a glance, and that guarantee cannot depend on instruction-following.

The same reasoning applies to the evidence-id filter in the same function: cited ids that do not exist are removed in code, not requested politely.

**Rejected.** Prompt-only. Works most of the time, which is the worst property a safety control can have.

**Cost.** None material. The model's stated confidence is preserved in the trace, so a systematic gap between requested and enforced behaviour is measurable.

---

## ADR-018 — SQL safety in four layers, with the database as the guarantee

**Decision.** sqlglot AST parsing, single-statement check, forced LIMIT, and a database-level read-only role.

**Reasoning.** Layers one through three are application-level and defeatable in principle — a parser has bugs, a keyword list has gaps. Layer four is not: a role without INSERT cannot insert, whatever reaches it.

Parsing rather than regex matters specifically. A regex looking for "DELETE" is defeated by comments, casing and string literals; `tests/test_trajectory.py` includes `"SELECT 1 /* harmless */; DELETE FROM index_values"` as a regression case.

**Rejected.** Prompt instructions alone. Not a security control. Allow-listing specific queries — too rigid for exploratory analysis, which is the entire value of the tool.

**Cost.** A separate database role to provision, which is an operational step that can be forgotten. `config/init-readonly-role.sql` makes it part of the local stack so the default posture is correct.

---

## ADR-019 — Retrieval metrics gate every PR; generation metrics run nightly

**Decision.** `evaluation/metrics.py` is deterministic and needs no LLM. `evaluation/generation_eval.py` needs a judge and is opt-in.

**Reasoning.** This is the practical answer to putting a non-deterministic system in CI. Deterministic metrics are fast, free and reproducible, so they can block a merge. LLM-judged metrics cost money, vary run to run, and would make the gate flaky.

Both matter, so both run — at the cadence each can sustain.

**Rejected.** Everything on every PR (too slow, too flaky, too expensive). Nothing on PRs (evaluation becomes a report nobody reads).

**Cost.** A faithfulness regression can merge and is caught the next night. Accepted: a retrieval regression is the more common failure and is caught immediately, and faithfulness regressions usually follow from retrieval ones.

---

## ADR-020 — Judge schemas put rationale before the score

**Decision.** Every LLM-judge Pydantic model declares `rationale` as its first field.

**Reasoning.** A judge asked for a score first will produce one and then rationalise it, because generation is autoregressive — the tokens justifying the score are conditioned on the score already emitted. Reversing the order makes the score conditioned on the reasoning instead.

It is a one-line change with a measurable effect on agreement with human labels, and it appears throughout: `RouteDecision`, `VerificationResult`, `AnswerJudgement`, `ClaimVerdict`, `Triage`.

**Rejected.** Score-first schemas. Marginally cheaper in output tokens and measurably worse.

**Cost.** More output tokens per judgement. Trivial relative to the accuracy gain.

---

## ADR-021 — Faithfulness by atomic claim decomposition

**Decision.** Decompose an answer into atomic claims, verify each against the retrieved context independently, report the supported fraction.

**Reasoning.** A whole-answer verdict collapses "eight correct facts and one fabricated one" into a single fuzzy score, and that is exactly the case you need to detect — an answer that is 90% right and 10% invented is more dangerous than one that is obviously wrong, because it will be believed.

Decomposition also neutralises verbosity bias: length stops being a proxy for quality when every claim is checked individually.

**Rejected.** Single-pass whole-answer grading. Cheaper, and it hides the failure mode that matters.

**Cost.** N+1 LLM calls per answer instead of one. This is why generation evaluation runs nightly rather than per PR.

---

## ADR-022 — Golden sets must include refusal and ACL cases

**Decision.** `TARGET_MIX` requires 15% unanswerable and 10% ACL-denied examples. `check_coverage()` reports shortfalls.

**Reasoning.** A golden set of only answerable questions cannot measure hallucination. A system that always answers will score well on it and fail users, and the evaluation will report success throughout.

ACL cases carry a second, subtler requirement: the correct behaviour is not merely "no answer" but "no acknowledgement that the document exists". *"You don't have access to the EU_TECH_50 incident post-mortem"* has already leaked that there was an incident on that index — which in a regulated environment can itself be material.

**Rejected.** Measuring only positive cases. Common, and it produces a metric that improves as the system becomes more willing to guess.

**Cost.** Negative cases are harder to write than positive ones, since you must know what the corpus genuinely lacks. Worth the effort — they are the examples that catch real regressions.

---

## ADR-023 — Synthetic golden examples are labelled and treated as drafts

**Decision.** `generate_from_chunks` sets `meta.synthetic = true` and logs a warning that its output needs review.

**Reasoning.** Questions generated *from* an answer are systematically easier than questions asked by someone who does not know the answer. They reuse the chunk's vocabulary, they ask about exactly what is present, and they never ask something the corpus almost-but-not-quite covers.

A system can score 0.95 on a synthetic set and fail on real traffic. The label exists so that number is never quoted without the caveat.

**Rejected.** Treating generated examples as a golden set. Fast, and it produces a metric that measures the generator rather than the system.

**Cost.** Manual review is the bottleneck on golden set size. Accepted — twenty hand-reviewed examples beat two hundred unreviewed ones, and the set should grow from production failures rather than from generation.

---

## ADR-024 — Generated fact sheets rather than RAG over a metadata database

**Decision.** `ingest/factsheet.py` renders a markdown document per index from Kafka events and ingests it normally.

**Reasoning.** New indices launch continuously and nobody writes a wiki page for each. The two obvious options both fail: leaving them undocumented leaves a permanent gap, and chunking rows of a metadata database produces chunks of orphaned field values that retrieve on numbers and mean nothing.

Generating a document from structured metadata gives RAG something it is actually good at — prose about a single entity — and the Kafka trigger means a new index is searchable within minutes of launch.

The boundary is the interesting part: fact sheets carry the *prose* (methodology, operational quirks). Precise questions — "which indices rebalance in March", "how many launched this quarter" — route to SQL instead, because that is exact, always current and cheaper. Knowing which questions belong to which system is the design decision; the fact sheet is just the implementation.

**Rejected.** Indexing the metadata table directly. Produces unretrievable chunks and answers aggregate questions worse than a `GROUP BY` does.

**Cost.** A template to maintain, and fact sheets that go stale if the event stream breaks. Mitigated by handling `retired` events as deletions — an assistant confidently describing a decommissioned index is worse than one that says nothing.

---

## ADR-025 — Logs are queried, not indexed

**Decision.** No log ingestion. Log questions route to a KQL/SQL tool. What *is* indexed is error signatures extracted from closed incidents.

**Reasoning.** Logs are enormous, change constantly, and the useful signal is aggregate ("error rate rose at 09:14") rather than semantic. Embedding them is expensive, the index goes stale within minutes, and retrieval returns individual lines that answer nothing.

Error signatures from resolved incidents are the opposite: small, stable, and exactly the symptom-to-cause mapping RAG handles well. That is the part worth indexing, and it is a few hundred documents rather than a few hundred million lines.

**Rejected.** Indexing a rolling log window. Common, expensive, and produces a system that is confidently out of date.

**Cost.** Log questions need a separate tool and its own guardrails. Worth it — the tool is cheaper to build and correct than a log index would be to keep fresh.

---

## ADR-026 — Line length is 120, not 100

**Decision.** `ruff`'s `line-length` in `pyproject.toml` is 120. Editor rulers, `prettier.printWidth`, and `black-formatter.args` already matched 120; ruff was the outlier at 100.

**Reasoning.** A single width across every tool and file type means the ruler in the editor means the same thing regardless of which formatter is about to run, and a line that looks fine on screen doesn't get silently wrapped or flagged by CI. 100 was tighter than the rest of the toolchain without a documented reason to be.

**Rejected.** Leaving ruff at 100 and lowering everything else to match. Would shrink usable width for JSON/YAML/JS/TS files, which had no independent reason to be narrower.

**Cost.** Existing Python source wrapped for 100 columns doesn't need reflowing, but new code can now run wider — a slightly higher ceiling for line bloat before ruff complains.

---

## ADR-027 — The RCA agent's target system is the OpenTelemetry Demo, not a fictional index platform

**Decision.** Sentinel's domain moves from a fictional "financial index calculation platform" to the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) (`app/otel-demo/`), a real running ~20-service e-commerce deployment, themed here as "Astroshop". `rca/clients.py`, the warehouse tool, the change tool, the prompts and the specialist roster are rewritten against it. The pipeline stages (`parsing/`, `chunking/`, `embedding/`, `vectorstore/`, `retrieval/`, `reranking/`, `evaluation/`) are domain-agnostic and do not change.

**Reasoning.** The financial theme was fiction with every dependency stubbed, so "what breaks in production" was an assertion rather than something demonstrable. The otel-demo is a real system with real OpenTelemetry traces and metrics, a real dependency graph (its own `compose.yaml`), and injectable faults (`flagd`) whose ground truth is exact — `app/incident-generator` records the flag it flipped and when. Re-theming turns every stubbed client into one that reads a live signal, makes the knowledge corpus documentation of a system that actually exists, and makes agent evaluation checkable against `scripts/corpus/_incident_truth.jsonl` instead of a hand-written answer key.

The CLA​UDE.md invariants are unchanged — `require_scope` fail-closed, filters-in-query, `publish_node` needs `approved_by`, `DEGRADED_CONFIDENCE_CAP` enforced in code, supervisor holds no tools. They were never financial; they are structural, and they survive the re-theme verbatim.

The warehouse tool becomes a PromQL tool. The four-layer defence is preserved and re-expressed: parse the expression rather than regex it (PromQL has no parser in the stdlib, so the guard is a metric-name allowlist plus a mandatory bounded range and a forbidden-function list), a single expression only, a forced time bound, and a read-only Prometheus HTTP endpoint — the layer that actually guarantees it, exactly as the read-only database role did.

**Rejected.** Keeping the financial theme and just enlarging the corpus. Less disruption, but it means writing more fiction and the "what breaks in production" claims stay untestable — which is the specific thing this repository exists to avoid.

**Rejected.** Adopting a third-party RAG framework (LlamaIndex, Haystack, RAGFlow) as the base and building the agent on top. Throws away the parts that are the point here — the security model, provider symmetry, the eval gate, the agent tool-isolation constraints — to inherit someone else's abstractions.

**Cost.** A one-time rewrite of five files plus `tests/test_trajectory.py` (which hard-codes `index_values` in its SQL fixtures and the `run_readonly_sql` tool name). The otel-demo is vendored under `app/otel-demo/` (~large, Apache-2.0), kept as read-only input. Some knowledge of the financial framing is now only in git history and `.archive/`.
