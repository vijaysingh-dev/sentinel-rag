# Interview guide

Depth-probe questions per stage, with the answer this codebase supports and the file that proves it.

The pattern in these questions is always the same: an interviewer is checking whether you made decisions or followed a tutorial. The distinguishing signal is not that you know what RRF is — it is that you can say what you rejected, why, and what it cost. Every answer below is structured that way.

---

## Parsing

**"Walk me through how you extract text from a PDF."**

Start with the split that most answers miss: native PDFs have a text layer and scanned ones do not, and they share an extension. `parsing/base._pdf_has_text_layer` samples the first five pages and routes accordingly, because running pdfplumber on a scan returns an empty document rather than an error.

For native PDFs, pdfplumber rather than pypdf, because it exposes word positions and ruling lines — which is what makes table extraction possible at all. Tables are extracted **first** and their bounding boxes excluded from the text pass, otherwise every table's numbers appear twice: once as a grid and once as loose noise.

**"How did you decide between local libraries and a cloud service?"**

I measured it rather than choosing. `benchmarks/parser_bakeoff.py` scores each provider against hand-labelled ground truth on three dimensions and reports cost and latency alongside quality.

The result drove the hybrid design: local matches or beats both clouds on DOCX, PPTX and XLSX — the structure is already in the file, so a layout model has nothing to recover — and loses badly on scans. So the policy is parse locally, score with reference-free heuristics, escalate only below threshold. On our corpus that was within a few points of the best single provider at roughly a quarter of the cost, because 78% never escalated.

**"What does 'reference-free quality score' mean and why not just use accuracy?"**

At ingest time there is no ground truth, so the routing gate cannot compare against anything. `parsing/quality.py` scores five heuristic components — text density, word validity, structure, encoding, table plausibility — and is tuned conservatively: it should rarely call a bad parse good, since calling a good parse bad costs one cloud call.

The accuracy measurement is separate, offline, and used to *choose* providers rather than to route documents: `evaluation/parsing_eval.py` against hand-labelled truth.

**"What's the failure you'd never catch without reading raw extracted text?"**

Reading-order scrambling in multi-column layouts. Every character is correct and the text is unreadable because two columns were interleaved line by line. Character-level metrics score it near perfect. That is why `parsing_eval` measures Kendall's tau on block positions as a separate dimension.

**"Azure or AWS for parsing?"**

Different work, not just different vendors. Azure returns **markdown** with `output_content_format=MARKDOWN`, so headings and tables arrive structured and the chunker consumes them directly. Textract returns a **flat block graph** — LINE, WORD, CELL, LAYOUT nodes with Id relationships — and reconstructing reading order and table grids is your job. Most of `aws_parser.py` is that assembly.

Also: Textract's async API reads from S3 only, so multi-page parsing requires a bucket. There is no inline option. Azure has no equivalent constraint.

---

## Chunking

**"What chunk size did you use?"**

That question has no single answer here, and the reason is the interesting part. Strategy is chosen per document type by `DOC_TYPE_STRATEGY`, because a runbook and a glossary want opposite things.

A glossary entry is atomic — merging two terms into one chunk makes both harder to find, since the vector becomes an average of unrelated concepts. A runbook must never be split at retrieval time, because a retrieved half-runbook is worse than none: someone under incident pressure will execute the half. So `RunbookChunker` embeds individual steps for precision while `context_text` is always the complete procedure.

**"Why do your chunks have two text fields?"**

Because the retrieval unit and the generation unit do not have to be the same thing, and a single-field model makes three strategies inexpressible.

The retriever wants small single-topic units. The generator wants surrounding context. `embed_text` is vectorised, `context_text` is handed to the LLM. Sentence-window embeds one sentence and returns its neighbours; parent-document embeds children and returns the section. Neither is representable with one field.

**"What's the highest-return change you made to chunking?"**

Prefixing the heading path onto `embed_text`. *"Set this to false to skip validation"* is unretrievable — no realistic query lands near it. *"Checkout Configuration > Validation > strict_mode — Set this to false..."* is findable by dozens. It costs a handful of tokens and in practice bought more recall than upgrading the embedding model would have.

**"Do you use semantic chunking?"**

It is implemented and it is not the default. It embeds the whole corpus once at ingest just to decide boundaries, then embeds the chunks again — and in our sweep it did not beat recursive splitting by enough to justify that. `benchmarks/chunking_sweep.py` includes it so the claim is checkable rather than an opinion.

---

## Embedding

**"What's the most common silent bug in an embedding pipeline?"**

Query/document asymmetry. BGE and E5 expect an instruction prefix on the query side only; Cohere Embed v3 requires `input_type` to differ between indexing and querying. Getting either wrong costs several points of recall and produces no error at all.

`BaseEmbedder` makes `embed_query` and `embed_documents` separate methods so an adapter cannot collapse them accidentally. `scripts/embedding/01_embed_local.py` prints the similarity margin with and without the prefix so the cost is visible.

Second place: silent truncation. A 2000-token chunk sent to a 512-token model loses three quarters of its content with nothing in the logs. `_prepare` truncates deliberately and counts it.

**"Explain Matryoshka embeddings."**

`text-embedding-3-*` models are trained so the leading dimensions are usable alone, so `dimensions=512` on a 3072-dimension model returns a shorter vector retaining most of the quality. Index size and query latency both scale with dimensionality, and storage is often the binding constraint before quality is.

In our measurements 3072 → 1024 cost about a point of recall and saved two thirds of the footprint. It also enables a two-stage pattern: search on truncated vectors, rescore the top candidates at full precision.

**"How is Bedrock different from Azure OpenAI for embeddings?"**

Titan has **no batch endpoint** — one InvokeModel call per text — so throughput comes from client-side concurrency rather than batching. Azure accepts hundreds per request. That difference changes the shape of your ingestion code, and a naive port is slow in a way that looks like a network problem.

Cohere on Bedrock does batch, up to 96, and requires `input_type`.

**"How do you handle changing the embedding model?"**

`settings.index_version` fingerprints chunking config, provider, model and dimensions. Every chunk is stamped with it and every query filters on it. Querying an index built with a different model is the single most common production RAG incident, and it presents as a mysterious quality regression rather than an error — the version filter turns it into an empty result set, which is diagnosable.

---

## Vector store and retrieval

**"Why hybrid search?"**

Because the two arms fail on different queries and the failures are complementary. Dense search fails on exact identifiers — `INC-2607` is a token the embedding model has no useful representation for. Lexical search fails on paraphrase — "checkout is slow" and "elevated latency" share no words.

`scripts/vectorstore/01_index_and_search.py` runs the same three queries through dense, lexical and hybrid so the pattern is visible in one screen.

**"How do you combine the two score sets?"**

I do not combine scores, I combine ranks. Cosine similarity lives in [0,1]; BM25 is unbounded and corpus-dependent. Adding them is meaningless and normalising per query is unstable, because the distribution shifts with the query.

RRF contributes `weight / (k + rank)` from each list, with `k=60` damping top positions so one ranker's confident-but-wrong first result cannot dominate. Weighted fusion is implemented for comparison; it can win with a tuned alpha, but the right alpha differs between natural-language and identifier queries, so untuned RRF usually wins in practice.

**"How do you know retrieval is working?"**

I report recall@5 and recall@50 together, because the gap localises the failure. High recall@50 with low recall@5 is a ranking problem and a reranker fixes it. Low recall@50 is a candidate-generation problem and no reranker will help — you fix chunking, embeddings or parsing.

`scripts/evaluation/01_evaluate_stages.py` prints exactly that diagnosis rather than making you infer it.

**"What happens when the embedding service goes down?"**

It degrades to lexical-only rather than failing. That is a genuinely useful mode, not a token gesture: exact-match queries — error codes, service names, version numbers — are the ones lexical handles *best*, so a meaningful fraction of traffic is unaffected.

Every degradation increments `DEGRADED_MODE`, which is what catches a silent multi-day drift into a fallback path that nobody noticed because nothing errored. The mode is also passed to the model, so the eventual answer says its confidence is lower than usual.

**"How do you handle permissions?"**

The filter goes **inside** the query, never after. A post-filter has already loaded restricted content into your process, where a log line or a stack trace can leak it.

Each backend does it natively: array overlap in the pgvector WHERE clause, OData `search.in()` inside Azure's ANN traversal, `terms` in OpenSearch's filter context. `SearchFilter.require_scope()` raises on an empty role list, because an empty filter clause matches everything — the difference between "sees nothing" and "sees the whole corpus".

There is also a second check on the returned results that should never fire, wired to a critical log, plus `tests/test_acl.py` gating releases.

**"Isn't a refusal itself a leak?"**

Yes, and that is the subtler half. "You don't have access to the payment-service incident post-mortem" has already leaked that there was an incident on that service. `ToolResult.denied()` returns an instruction telling the model not to describe what was restricted or confirm it exists, and `tests/test_acl.py` parametrises over the forbidden terms to assert the instruction text does not name them.

**"Do you cache?"**

Three layers, and the rule is: cache knowledge, never telemetry. One conversation mixes both — "what does a circuit breaker do" is stable for months, "is checkout healthy right now" for seconds. Caching the second turns the assistant into an incident: someone asks whether a service recovered, gets a ten-minute-old "healthy", and stops investigating.

So cacheability is a property of the *tool*, via `LIVE_TOOLS`, not of the request. Every key carries role scope and index version, because omitting the first is a data leak and omitting the second serves stale chunks after a re-index.

---

## Reranking

**"What does a reranker do that the embedding model doesn't?"**

A bi-encoder encodes query and document separately and compares vectors — so the model never sees them together, which is what makes it precomputable and scalable. A cross-encoder takes the pair as one input and runs attention across both, so it catches negation and qualifier mismatch. It cannot be precomputed, so it only runs over a small candidate set.

Hence two stages: cheap and wide for recall, expensive and narrow for precision.

**"What can't a reranker fix?"**

A recall miss. If the answer is not in the candidate pool, no reordering surfaces it. That is why recall@50 and recall@5 are reported separately — the first tells you whether reranking is even the right lever.

**"Why a relevance floor?"**

It is what lets the system say "I don't have anything relevant" instead of answering from the five least-bad chunks. In an operations assistant that is the difference between a useful refusal and someone running the wrong runbook.

Floors are per-reranker and never portable: local cross-encoders emit unbounded logits, Azure's semantic ranker roughly 0–4, Bedrock normalised [0,1]. Copying a floor between them either rejects everything or accepts everything.

---

## Generation and evaluation

**"How do you measure whether answers are correct?"**

Faithfulness by atomic claim decomposition, not whole-answer grading. The answer is decomposed into independently checkable claims and each is verified against the retrieved context.

The reason is the failure mode: a whole-answer verdict collapses "eight correct facts and one fabricated one" into a fuzzy score, and that is exactly the case that matters — 90% right and 10% invented will be believed. Decomposition surfaces the fabrication and also neutralises verbosity bias, since length stops being a proxy for quality.

**"LLM judges are unreliable. How do you deal with that?"**

By naming the biases and mitigating each specifically:

- position bias — score one answer at a time against explicit criteria, never pairwise;
- verbosity bias — atomic claim decomposition;
- self-preference — judge with a different model family than the generator;
- rationale-after-score — every schema declares `rationale` **before** the score, so the reasoning conditions the score rather than the reverse.

Plus a free human signal: the analyst's approve/edit/reject on every RCA is a real label, which is why the evaluation layer does not depend solely on judges.

**"How do you put this in CI?"**

Split by determinism. Retrieval metrics need no LLM, so they gate every pull request. Generation metrics need a judge, so they run nightly.

The gate has two tiers: absolute floors, and regression against `benchmarks/baseline.json` committed to git. The second is what catches real problems, because absolute floors drift downward under deadline pressure. Tolerance is 0.03, and confidence intervals are bootstrapped — with a 40-example golden set a two-point improvement is usually noise.

Safety checks are never averaged. A 98% pass rate on "did not leak restricted content" is a failing grade.

**"How did you build your golden set?"**

Hand-written first, grown from production failures. `generate_from_chunks` exists for the cold start but labels its output `synthetic: true`, because questions generated *from* an answer are systematically easier than real ones — they reuse the chunk's vocabulary and never ask something the corpus almost-but-not-quite covers. A system can score 0.95 on a synthetic set and fail users.

`TARGET_MIX` requires 15% unanswerable and 10% ACL-denied examples, and `check_coverage` fails a set that has drifted into all-easy questions. Without negative cases you cannot measure hallucination at all.

---

## Agents

**"Why multi-agent instead of one agent with all the tools?"**

Three reasons, in order of weight. Tool-selection accuracy degrades as the tool count grows — three from three beats three from eleven. Isolation becomes structural rather than instructed: the knowledge specialist cannot execute SQL because it does not hold that tool. And each specialist's context holds only its own tool output.

The decision that makes the split pay for itself is that specialists return **structured evidence to shared state but only a prose summary to the supervisor's messages**. Without that, the orchestrator drowns in the same tool output the specialists were meant to absorb, and you have paid for extra hops to get a worse single-agent system.

**"How do you stop the model inventing citations?"**

Set membership, not judgement. `observed_refs()` scans raw ToolMessage content for handles that actually appeared; any claim citing a ref outside that set is dropped before becoming evidence. A fabricated reference cannot survive regardless of how plausible the claim reads.

It fails in the right direction too: a real claim with a mistyped ref is dropped, which loses information. The opposite failure — a fabrication surviving into a published RCA — is the one that matters.

**"How does human approval work without holding a connection open?"**

`interrupt()` persists the entire graph state to the checkpointer and returns control. No worker, no thread, no memory is held while an analyst takes four hours; the run resumes from the checkpoint when a decision arrives, on a different process if necessary.

That is the difference between human-in-the-loop as a production pattern and as a demo — polling loops do not survive a deploy or a pod eviction.

**"How do you keep generated PromQL safe?"**

Four layers, and I would name the fourth first because it is the only guarantee: a read-only Prometheus HTTP endpoint. Layers one to three are application-level and defeatable in principle.

Above it: a metric-name allowlist (PromQL has no parser in the standard library, so instead of an AST the guard extracts every identifier that looks like a metric name and checks it against `ALLOWED_METRIC_PREFIXES` — an allowlist of good names, not a regex looking for bad keywords), a forbidden-function check (`label_replace`, subqueries nesting range selectors), and a forced bounded time range.

**"What stops someone quietly breaking the design?"**

Tests that assert the architecture, not just the behaviour. `tests/test_trajectory.py` inspects the supervisor's source and fails if it references any specialist tool, because that is the constraint most likely to erode — someone adds a tool "just for triage" and the isolation argument collapses. A constraint stated only in a prompt survives until the next person edits that prompt.

Similarly: `publish_node` is asserted unreachable without `approved_by`, and `DEGRADED_CONFIDENCE_CAP` is enforced in code after the model returns rather than requested in a prompt.

---

## Questions worth asking back

Good signals that you have thought about operating this, not just building it:

- What is the corpus size and change rate? That decides whether ingestion is a nightly job or an event stream, and whether contextual chunking is affordable.
- Is there an existing identity service that maps users to roles? The ACL model assumes one; without it, roles have to come from somewhere less trustworthy.
- What is the tolerance for a wrong answer versus a refusal? That sets the relevance floor, and it is a product decision rather than a technical one.
- Who reviews the golden set? An evaluation suite nobody owns stops reflecting reality within a quarter.
