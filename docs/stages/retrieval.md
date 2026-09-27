# Retrieval

> Stage 5 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#5-retrieval); this file is the deep version.
>
> Code: `sentinel/retrieval/` (`pipeline.py`, `fusion.py`, `query_transform.py`) · Benchmark: `benchmarks/retrieval_sweep.py` · Offline eval: `sentinel/evaluation/retrieval_eval.py` · Cache: `sentinel/core/cache.py`

---

## What we used

```
query
  -> rewrite (multi-turn only, gated by a cheap regex)
  -> cache lookup (namespaced by role scope + index version)
  -> hybrid search: BM25 + vector, fused by RRF        [k=50, wide, recall-oriented]
  -> cross-encoder rerank                               [n=6, narrow, precision-oriented]
  -> relevance floor
  -> dedupe by parent / content
  -> optional MMR for diversity
  -> assemble within a token budget
```

Everything above lives behind one class, `RetrievalPipeline` (`pipeline.py`), and its single public
entry point, `retrieve()`. Query transformation (`query_transform.py`) sits in front of it and is
called by whoever owns the conversation (the RCA agent's knowledge specialist), not by the pipeline
itself — the pipeline always receives a single, already-standalone query string.

---

## The big picture

Imagine you ask a librarian a question. A bad librarian either (a) hands you every book that
contains any of your words, in no particular order, and lets you dig through them, or (b) reads
your mind and hands you exactly one book, which is great when they're right and useless when
they're wrong. A good librarian does something in between: they quickly pull a wide stack of
plausibly-relevant books off the shelves (they'd rather grab ten too many than miss the one you
need), and only *then* do they sit down and actually read the first paragraph of each one to figure
out which two or three are worth putting in your hands.

That two-step process — cast a wide net, then read carefully and narrow it down — is retrieval.
Stage one (**search**) is optimized to not miss anything: it is allowed to be a little sloppy about
ordering as long as the right answer is *somewhere* in the pile. Stage two (**reranking**, the next
stage in this pipeline) is optimized to get the ordering right among a small pile it trusts stage
one already filled correctly. Conflating the two into one step is the single most common design
mistake in home-grown RAG systems: people either skip the wide net (miss things) or skip the careful
read (return five near-duplicates of the same fact, none of them the best one).

Retrieval is also the stage where a RAG system's two failure modes become visible as *numbers*
instead of vibes. `recall@5` — "was the right chunk in my top 5?" — tells you whether the *ranking*
is good. `recall@50` — "was the right chunk in my top 50 at all?" — tells you whether the
*candidate generation* found the answer in the first place. Reading these two side by side is how
you diagnose a bad answer without guessing: if recall@50 is high but recall@5 is low, the answer was
in the haystack and got ranked badly — fix the reranker. If recall@50 is low, the answer was never
in the haystack — no amount of re-ranking will help, and you have to go back and fix retrieval,
chunking, or even parsing. `scripts/evaluation/01_evaluate_stages.py` prints exactly this diagnosis
so you never have to eyeball it.

The rest of the pipeline (caching, deduplication, the token budget) exists to make that two-step
search cheap to repeat and safe to hand to a language model afterward — a search that returns five
copies of the same paragraph, or a search that silently returns a different customer's restricted
document, is not "retrieval working correctly but a little rough," it is a bug, and several pieces
of this stage exist specifically to make those bugs structurally hard to write.

---

## Why two stages, in more depth

Recall is stage one's job; precision is stage two's. They have genuinely different failure modes,
which is exactly why splitting the diagnosis (recall@5 vs recall@50) is so useful — it tells a
developer which of two very different fixes to reach for:

- **high recall@50, low recall@5 → a ranking problem.** The right chunk is buried at position 30. A
  reranker (stage 6) fixes this directly — its whole job is reordering a candidate pool it doesn't
  need to search from scratch.
- **low recall@50 → a candidate-generation problem.** The right chunk never made it into the top 50
  at all. No reranker can promote a chunk that was never handed to it. The fix is earlier in the
  pipeline: widen `k`, improve the embedding model, fix chunking, or — as the parsing stage doc
  shows with the Suzuki Brezza brochure — fix a parse that silently destroyed the content before it
  ever reached the index.

### Why RRF rather than adding the scores together

The obvious-looking shortcut — "just average the vector similarity score and the BM25 score" —
doesn't work, and the reason is worth understanding rather than memorizing: **the two scores are not
measured on the same ruler.** Cosine similarity (the vector arm) always lives between 0 and 1. BM25
(the keyword arm) is unbounded and its scale depends on the whole corpus — a BM25 score of "12" means
something different in a corpus of 50 documents than in one of 50,000. Adding a number that means
"between 0 and 1, always" to a number that means "who knows, ask the corpus" produces a value with
no consistent meaning. You could try to rescale ("normalize") the scores per query, but that's
unstable too: normalizing depends on the min and max score *in that query's result set*, so the same
absolute score gets treated differently query to query, and one freak high-scoring outlier squashes
every other score toward zero.

Reciprocal Rank Fusion (RRF) sidesteps the whole problem by **throwing the scores away** and using
only the **rank** — "this chunk was the 3rd best result," full stop, regardless of what number
produced that position. A rank is comparable across systems in a way a raw score never is. See
`fusion.reciprocal_rank_fusion` below for the actual formula and why `k=60`.

Weighted score blending (`weighted_fusion`) is implemented too, purely so the benchmark can show you
the comparison on your own data. It *can* beat RRF, but only with a hand-tuned blend weight
(`alpha`), and the right `alpha` for a natural-language question ("what causes checkout latency
spikes?") is typically different from the right `alpha` for an identifier-style query ("error code
PAY-503"). A single tuned value therefore systematically underperforms on whichever query style it
wasn't tuned for — which is why untuned, parameter-free RRF usually wins in practice, and is the
default (ADR-010).

---

## Function-by-function walkthrough

### `RetrievalPipeline.__init__` (`pipeline.py`)

```python
def __init__(self, store=None, embedder=None, reranker=None, use_cache: bool = True):
    self.store = store or get_provider("vectorstore")
    self.embedder = embedder or get_provider("embedder")
    self._reranker = reranker
    self._reranker_resolved = reranker is not None
    self.cache = RetrievalCache() if use_cache else None
```

**What:** wires up the store, embedder and (lazily) the reranker, defaulting each to whatever the
adapter registry currently has configured (`get_provider`, `core/registry.py`), so a caller in
production code never has to know which provider is active.

**Why the reranker is special-cased with a lazy `@property` instead of being resolved here too:** a
reranker is allowed to be missing (a local cross-encoder model failed to download, or the operator
never installed the `reranking` extra) and the pipeline should still work in a degraded mode rather
than fail to even construct. Resolving it eagerly in `__init__` would mean "no reranker installed"
crashes the whole retrieval pipeline before a single query runs. Resolving it lazily, and catching
`ProviderUnavailable` when it's first *used*, means the failure is deferred to the one place that can
actually do something sensible about it — log a warning and fall back to unre-ranked candidates.

### `RetrievalPipeline.retrieve()` — the entry point

This is the only method most callers ever touch, so it's worth walking through in the order it
actually executes:

1. **Resolve defaults.** `k`, `top_n` and `use_mmr` fall back to `settings` values
   (`retrieve_k=50`, `rerank_top_n=6`, `use_mmr=False`) when the caller doesn't override them — see
   [Why these defaults](#why-these-defaults) for where each number comes from.

2. **Build and validate the security filter.**
   ```python
   filters = SearchFilter(tenant_id=tenant_id, roles=roles, components=components or [],
                           doc_type=doc_type, index_version=settings.index_version)
   filters.require_scope()
   ```
   `SearchFilter` (`vectorstore/base.py`) is a backend-neutral description of who is asking and what
   they're allowed to see. `require_scope()` raises `AuthorizationViolation` if `roles` is empty —
   **on purpose, loudly, before any search runs.** An empty role list, left unchecked, would render
   as an empty filter clause in the eventual SQL query, and an empty filter clause matches
   *everything*. That single line is the difference between "this caller is refused" and "this
   caller silently sees the entire corpus," and it's why it's listed as a non-negotiable invariant in
   `CLAUDE.md`. Note that `roles` and `components` come from the caller's authenticated session
   (`rca/runtime` contextvars in the agent layer) — never from a tool argument a model could write,
   because anything an LLM can set is something a prompt injection can set too.

3. **Check the retrieval cache.** `role_scope(roles)` turns the caller's role set into a short hash
   used to namespace the cache — see [`RetrievalCache`](#the-three-cache-layers-corecachepy) below.
   On a hit, the pipeline returns immediately with `mode="cache"` and skips search entirely.

4. **Search** (`_search`, the degradation ladder — see below).

5. **Defense-in-depth authorization check** (`_assert_authorized`). This runs *after* the filtered
   search, on the results the filter should already have restricted correctly. It should never fire.
   It exists anyway because a mis-tagged document or a filter bug in one specific backend adapter is
   exactly the kind of thing that should page a human loudly (`log.critical` + raise) rather than
   quietly leak — defense in depth means the second layer catches what the first layer was supposed
   to prevent, not "the first layer is good enough so we don't need a second."

6. **Rerank** (`_rank`), **dedupe** (`dedupe_by_parent` then `dedupe_by_content`), **optionally
   diversify** (MMR), then **fit the context budget** (`_fit_budget`).

7. **Record a metric, cache the result if the search wasn't degraded, log, and return.**
   `mode == "full"` gates caching deliberately: a degraded-mode result (BM25-only because embeddings
   were down, or unreranked because the reranker failed) should not get cached and served to the
   *next* caller as if it were a normal, healthy answer. Caching only healthy results means an
   outage self-heals from the cache the moment the dependency comes back, instead of poisoning
   answers for the cache's whole TTL.

### `RetrievalPipeline._search()` — the degradation ladder

```
full  →  dense_only  →  bm25_only
```

This is the method that decides *how* to search, and it's written as a chain of `try`/`except`
fallbacks rather than one search call, because each failure mode has a different, genuinely useful
next step:

- **Try to embed the query.** If the embedding provider is down, don't fail the whole request —
  fall back straight to `store.sparse_search` (pure BM25 keyword search) and label the result
  `mode="bm25_only"`. This is called out explicitly in the docstring as "a genuinely useful degraded
  mode, not a token gesture": the queries lexical search handles *best* — error codes, incident IDs,
  exact version numbers — are exactly the queries where a missing embedding model costs you nothing.
- **If embedding succeeded, try `store.hybrid_search`** (vector + keyword, fused). If the vectorstore
  can't do hybrid for some reason, fall back to `store.dense_search` (vector only) and label it
  `mode="dense_only"`.
- **If even the fallback search fails, raise `ServiceUnavailable`.** This is the one place an
  exception is allowed to propagate — there's no cheaper fallback left, so per ADR-013's rule
  ("infrastructure failures raise; semantic failures return"), a truly broken vectorstore is a
  real infrastructure failure that should surface as an error, not be quietly swallowed into an
  empty result set that looks like "no matching documents."

Every rung of the ladder increments `DEGRADED_MODE` (a Prometheus counter labeled by stage and
mode). That metric is the whole point of writing the ladder this way instead of just letting things
fail: without it, a system can drift into `bm25_only` for days after an embedding provider quietly
breaks, and nothing *errors* — searches keep returning results, just worse ones, and nobody notices
until someone asks "why does search feel dumber lately."

### `RetrievalPipeline._rank()` — reranking with a safety net

Calls `reranker.rerank(query, candidates, top_n=top_n, floor=settings.min_rerank_score or None)`.
If the reranker property resolves to `None` (not installed) or the call itself throws, this method
**does not fail the request** — it falls back to `candidates[:top_n]`, i.e. "trust whatever order the
first-stage search produced." That's a worse ranking, not a broken pipeline, which is exactly the
"degrade, never fail" principle this whole stage is built around (and the same principle the RCA
agent layer applies at the level of a whole investigation, per ADR-017).

### `RetrievalPipeline._assert_authorized()`

Walks every result and checks `chunk.allowed_roles` and `chunk.components` against the caller's
roles/components one more time, raising `AuthorizationViolation` (which is logged at `CRITICAL`, not
just returned as a failed search) on any mismatch. Worth repeating because it's easy to read past:
this check running at all is a sign something upstream already went wrong — the filtered search was
supposed to make an unauthorized result impossible to retrieve in the first place. This function is
the alarm bell, not the lock.

### `_fit_budget()` — assembling a prompt is a knapsack problem, not "take the top 5"

```python
def _fit_budget(results, budget_tokens):
    kept, used = [], 0
    for item in results:
        cost = item.chunk.token_count or ntokens(item.chunk.context_text)
        if used + cost > budget_tokens:
            continue
        kept.append(item)
        used += cost
    return kept
```

**What:** walks the ranked results in order and greedily keeps whichever ones still fit inside
`context_budget_tokens` (4000 by default), skipping — not stopping at — any chunk that would blow
the budget, so a single oversized chunk doesn't prematurely truncate the rest of a good list.

**Why this needs to exist at all:** a naive "just take the top 5" assumes every chunk costs roughly
the same number of tokens. That assumption breaks the moment `parent_document` chunking is in the
mix (see the chunking doc) — a parent-document chunk can be 1800 tokens on its own, and "top 5" of
those alone could blow past a 4000-token budget on its own, silently truncating the prompt sent to
the LLM in a way nothing in the code would flag. Treating this as an explicit knapsack — walk in
rank order, keep what fits, skip what doesn't — makes the budget a hard guarantee instead of an
assumption that happens to hold on typical corpora.

### `_dehydrate()` / `_rehydrate()` — why the cache stores plain dicts, not `RetrievedChunk` objects

`RetrievedChunk` wraps a `Chunk` dataclass; neither is JSON-serializable out of the box, and neither
should be trusted to round-trip perfectly through a library upgrade months later. `_dehydrate` turns
a result into a flat, explicit dict of exactly the fields the cache needs; `_rehydrate` reconstructs
a `RetrievedChunk` from that dict on a cache hit, with `.get(..., default)` fallbacks for any field
added to `Chunk` after some entries were already cached. The explicitness is deliberate: a cache
entry written by an older version of the code should still deserialize into something usable by a
newer version, rather than throwing a `KeyError` the day someone adds a field to `Chunk`.

### `fusion.reciprocal_rank_fusion()`

```python
scores[cid] = scores.get(cid, 0.0) + weight / (k + rank)
```

**What:** for each ranked list (e.g. the dense-search list and the BM25 list), every item earns
`weight / (k + rank)` points, and a chunk's total score is the sum of what it earned across every
list it appeared in — so a chunk that shows up near the top of *both* the vector search and the
keyword search outranks one that was merely first in one of them.

**Why `k=60` specifically:** picture what happens without it — a chunk ranked #1 by one arm would
score `1/1 = 1.0`, ranked #2 would score `1/2 = 0.5`. That's an enormous, cliff-like gap between
adjacent ranks near the top, meaning the #1 result from one ranker (even if that ranker is
occasionally confidently wrong) would almost always steamroll a #2-or-lower result from the other
ranker, no matter how much better the second ranker's overall judgment was. Adding a constant `k`
before the rank flattens that curve: `1/(60+1) ≈ 0.0164` vs `1/(60+2) ≈ 0.0161` — adjacent ranks near
the top now differ by a tiny amount instead of a factor of two. `k=60` is the conventional value from
the original RRF paper and from what most production hybrid-search systems settle on; the exact
number matters less than having *some* damping constant, and 60 has enough of a track record across
unrelated systems that re-deriving it from scratch on a 40-example golden set would mostly add noise.

**A subtlety worth catching:** the `best` dict merges the arm-specific scores (`dense_score`,
`sparse_score`, `rerank_score`) from whichever occurrence of a chunk is first seen, so a chunk that
appears in both the dense and sparse lists doesn't lose the information about *which* arms found it —
useful for debugging why something ranked where it did.

### `fusion.weighted_fusion()`

Min-max normalizes both arms to `[0, 1]` per query, then blends them as
`alpha * dense_norm + (1 - alpha) * sparse_norm`. Exists purely as a comparison baseline for
`benchmarks/retrieval_sweep.py` — see [Why RRF](#why-rrf-rather-than-adding-the-scores-together)
above for why it isn't the default. Note the failure mode baked into min-max normalization itself:
if `span == 0` (every item in an arm scored identically), it falls back to giving everything a score
of `1.0` rather than dividing by zero — a defensive edge case, not a meaningful signal.

### `fusion.maximal_marginal_relevance()` (MMR)

**What:** given a ranked list and the query vector, greedily builds an output list one item at a
time: the first pick is just whatever's most relevant to the query; every pick after that balances
relevance against **novelty** — how different this candidate is from everything already picked —
using the `lam` (lambda) dial: `lam * relevance - (1 - lam) * redundancy`.

**Why it exists:** the naive top-5 can be five near-identical restatements of the same fact, which
happens routinely with `sentence_window` chunking (heavily overlapping windows) or any corpus with
repeated boilerplate. MMR trades some raw relevance for guaranteeing the five results actually say
five different things. **Why it's off by default (`use_mmr: bool = False`):** that trade is the wrong
one for narrow factual lookups — "what's the timeout value for the checkout service's retry
policy?" wants the single most relevant chunk repeated in every form the corpus has it, not four
other things for the sake of variety. MMR earns its cost specifically on multi-document,
multi-faceted questions ("what changed about checkout in the last week?"), so it's an opt-in dial,
not a default-on behavior.

### `fusion.dedupe_by_parent()` / `dedupe_by_content()`

`dedupe_by_parent` keeps at most `keep` (default 1) result per `parent_id`; `dedupe_by_content` keeps
at most one result per `content_hash`. Both are cheap, deterministic set-membership checks, run in
that order right after ranking. **Why both, and why in this order:** `dedupe_by_content` alone
wouldn't catch `parent_document` chunking's actual failure mode — five *different* child chunks
(different `chunk_id`, different `content_hash`, since each child is a distinct small piece of text)
that all resolve to the *same* parent section text in `context_text`. Only `parent_id` captures that
relationship. `dedupe_by_content` still matters separately for near-duplicate overlap windows from
`sentence_window` chunking, where two adjacent windows can share most of their text but aren't
literally the same parent section. Skipping either one is exactly the "what breaks in production"
bug called out in both this doc and the chunking doc: five results eating the entire context budget
on one section of one document.

### `query_transform.needs_rewrite()` and `rewrite_query()`

```python
DEPENDENT = re.compile(r"\b(it|its|this|that|these|those|they|them|the (first|second|third|last|"
                        r"other|same)|there|then|he|she|his|her)\b", re.IGNORECASE)
```

**What:** `needs_rewrite` is a cheap regex gate — true only if there's conversation history *and* the
latest message contains a pronoun or a referring phrase like "the second one." `rewrite_query` only
pays for an LLM call when that gate fires; otherwise it returns the message unchanged.

**Why a regex gate in front of an LLM call, rather than always rewriting:** the majority of queries
in a real conversation are already standalone ("what's the retry policy for the payment service?"
needs no rewriting). Calling an LLM on every single query to resolve references that mostly aren't
there would add latency and cost to the common case to handle the uncommon one. The regex is
deliberately cheap and over-inclusive — it will trigger a rewrite call sometimes when it wasn't
strictly needed (a false positive costs one wasted LLM call), but it will essentially never *miss* a
message that actually needs rewriting (a false negative embeds nonsense and silently returns wrong
results), which is the correct asymmetry to bias toward.

**Why this matters more than almost anything else in retrieval:** "What about the second one?" has
no meaning on its own — embedded by itself, it retrieves whatever's near the *word* "second" in
vector space, which is close to random for most corpora. Any multi-turn assistant that skips this
step works fine on the first question of every conversation and silently degrades from the second
question onward — which the docstring calls out as "the most common unreported bug in deployed
assistants," because it never produces an error, just a wrong answer that looks like the model being
dumb rather than like a retrieval bug.

**Why it falls back to the original message on any failure**, rather than erroring: a failed rewrite
should degrade to "search the literal question" — worse than a good rewrite, but far better than
failing the whole turn over what's fundamentally a quality-of-search issue, not an infrastructure
one.

### `query_transform.multi_query()`, `hyde()`, `decompose()`

Three more transformations, each solving a different vocabulary or structure mismatch, and each with
the identical "log the failure, return something safe" fallback shape as `rewrite_query`:

- **`multi_query`** generates paraphrases of the question using different vocabulary — useful because
  the person asking ("why is checkout slow?") and the person who wrote the documentation ("checkout
  service latency degradation") don't always use the same words, and a single embedding can miss a
  chunk that's a perfect match phrased differently. Costs `n` extra retrievals, so it's for
  high-value or batch paths, not every interactive query.
- **`hyde`** — Hypothetical Document Embeddings — asks the LLM to *write a fake answer* to the
  question, as if it were documentation, and embeds *that* instead of the question. The reasoning is
  subtle but important: an embedding model was trained mostly on matching similar *kinds* of text to
  each other, and a fabricated answer written in documentation style sits closer, in embedding space,
  to real documentation than a question does (a question is phrased as a question; a chunk is phrased
  as an answer). This helps a lot on jargon-heavy queries where the asker's vocabulary is genuinely
  far from the documentation's — and hurts when the model hallucinates a plausible-sounding answer in
  the *wrong domain entirely*, which is why it's opt-in, not default.
- **`decompose`** splits a genuinely multi-part question ("how does the checkout service's retry
  policy differ from the payment service's?") into independent sub-questions that can each be
  retrieved separately and then compared, rather than trying to find one chunk that already contains
  the comparison.

---

## The three cache layers (`core/cache.py`)

| layer | class | key includes | TTL | what it stores |
|---|---|---|---|---|
| embedding | `EmbeddingCache` | model + content hash | never expires by default | the vector for a piece of text |
| retrieval | `RetrievalCache` | index_version + role scope + filter fingerprint + query hash | 900s (15 min) | the ranked chunk-id list for a (query, filter) pair |
| semantic answer | `SemanticAnswerCache` | role scope + index_version namespace | 3600s (1 hr) | a full prior answer, matched by cosine similarity ≥ `answer_cache_threshold` |

**The rule every layer obeys: cache knowledge, never telemetry** (ADR-011). One conversation with
this assistant genuinely mixes two very different kinds of question: "what does a stale price feed
mean?" is stable for months — cache it aggressively. "Is the SGX feed healthy right now?" is stable
for seconds — caching *that* turns a helpful assistant into a liability, because an engineer could
ask whether an incident has recovered, get a ten-minute-old "yes, healthy," and stop investigating a
live problem. So cacheability is decided **per tool**, not per request: `LIVE_TOOLS` is the explicit
set of tool names whose output is live state (`query_prometheus`, `find_traces`, `current_flags`,
etc. — see the full list in the file), and `is_cacheable(tools_called)` returns `False` if *any* tool
in a turn touched live state, even if the rest of the turn was pure documentation lookup. This is
also why `RetrievalPipeline.retrieve()` only writes to the cache when `mode == "full"` — the cache
itself only ever needs to worry about *this* particular search's freshness, and the agent layer above
it separately worries about whether the *conversation turn* as a whole is safe to cache.

**Why every key includes both role scope and index version, not just a query hash** (ADR-012):
omitting `role_scope(roles)` means two different callers with different permissions could share a
cache entry — not a performance bug, a data leak, since caller B could receive an answer built from
documents only caller A was allowed to see. Omitting `index_version` means a re-index (new chunking
config, new embedding model, new dimensions) leaves stale cached results pointing at chunk ids that
may no longer mean the same thing, or may not exist at all — and this failure is especially nasty
because it doesn't error, it just quietly serves wrong or stale-feeling answers, which reads to a
user as "the quality got worse" rather than as an obvious bug.

**Why `SemanticAnswerCache`'s threshold deserves real suspicion before you trust it:** it matches on
*meaning*, via cosine similarity between the new question's embedding and a previously-cached
question's embedding — and "how do I **enable** delivery" and "how do I **disable** delivery" are
extremely close neighbors in embedding space (they share almost every word) while being opposite
questions. A threshold picked casually, or tuned only on a set of paraphrase pairs that never
included this kind of near-miss, will happily return the *wrong* cached answer with high confidence.
`0.95` is deliberately conservative — see [Why these defaults](#why-these-defaults) — but the
docstring is explicit that it "deserves calibration on your own data before you trust it," which is
as much a warning to a future maintainer as it is documentation.

---

## Why these defaults

| constant | value | where | derivation |
|---|---|---|---|
| `retrieve_k` | `50` | `settings` | the candidate-pool width for stage one. Wide enough that the true answer is very rarely outside it (recall@50 is meant to be close to a ceiling), narrow enough that reranking 50 candidates with a cross-encoder stays fast (tens to low hundreds of milliseconds — see the reranking stage doc). `benchmarks/retrieval_sweep.py` explicitly sweeps candidate depth to find the "knee" beyond which more candidates cost latency and buy nothing; 50 is that knee on the corpora this repo has benchmarked. |
| `rerank_top_n` | `6` | `settings` | how many chunks actually reach the prompt. Small on purpose: every chunk costs prompt tokens and reading attention, so the number that survives to the LLM should be "confidently enough" rather than "as many as fit." 6 leaves room for a heading-prefixed chunk plus a little overlap slack inside a modest context budget. |
| `rrf_k` | `60` | `settings` (`fusion.py`) | damping constant in reciprocal rank fusion. See [`reciprocal_rank_fusion`](#fusionreciprocal_rank_fusion) above for the full derivation — flattens the huge score gap between rank #1 and rank #2 that a bare `1/rank` formula would produce. Conventional value from the RRF literature; not corpus-tuned. |
| `hybrid_alpha` | `0.5` | `settings` | the dense-vs-sparse blend weight *only* used by `weighted_fusion`, the non-default comparison path. `0.5` is a neutral starting point (equal trust in both arms) precisely because this repo's position is that no single alpha is right for every query style — see ADR-010. |
| `mmr_lambda` | `0.6` | `settings` | relevance-vs-novelty trade-off inside MMR. Above 0.5 means relevance still dominates novelty — MMR here is meant to break up near-duplicates, not aggressively chase diversity at the expense of picking marginally-relevant results just because they're different. |
| `use_mmr` | `False` | `settings` | off by default. See [`maximal_marginal_relevance`](#fusionmaximal_marginal_relevance-mmr) above — diversity is the wrong default for narrow factual lookups, which are the majority of queries this system expects. |
| `min_rerank_score` | `0.0` | `settings` | a *neutral placeholder*, not a real floor — `0.0` disables floor filtering by default at the pipeline level. Each reranker sets its own real floor (local cross-encoder: `-2.0`; Cohere: `0.02`) because raw score scales are completely different between an unbounded logit and a normalized [0,1] score — see the reranking stage doc. A floor copied from one reranker to the other would either reject everything or accept everything. |
| `retrieval_cache_ttl` | `900` s (15 min) | `settings` | short enough that an incremental document update (one that doesn't bump `index_version`) doesn't stay stale in the cache too long, long enough to absorb genuinely repeated queries within a conversation or across nearby users without hitting the vectorstore every time. |
| `answer_cache_ttl` | `3600` s (1 hr) | `settings` | answers change less often than retrieval candidate sets do (an answer is a synthesis, not a raw search result), so it can safely live longer — but still bounded, since the underlying documents can still be edited within the hour. |
| `answer_cache_threshold` | `0.95` | `settings` | deliberately high (near-exact-match) cosine similarity, precisely because of the "enable vs disable delivery" near-miss problem described above. A lower threshold would raise the hit rate but also raise the odds of returning a confidently wrong cached answer to a subtly different question — and a wrong cached answer is a worse failure than a cache miss (which just costs a little extra latency doing the work for real). |
| `context_budget_tokens` | `4000` (per-call default, not a `settings` field) | `RetrievalPipeline.retrieve()` | a conservative slice of a typical LLM context window, leaving the rest for the system prompt, conversation history, and the model's own output. Callers can override it per use case. |

---

## Common questions

**Why not just use vector search alone — isn't that "real" semantic search?**
Because embeddings are bad at exact tokens — error codes, version numbers, service names with
unusual casing, anything where the literal characters matter more than the meaning. BM25 (keyword
search) is bad at paraphrase and synonymy. They fail on almost disjoint sets of queries, which is
exactly the situation where combining two weak-in-different-places signals beats either alone. The
degradation ladder makes this concrete: when the embedding model is down, BM25-only isn't a crippled
fallback, it's a genuinely competent mode for a meaningful slice of real traffic.

**What does it mean if recall@5 and recall@50 are both low?**
Candidate generation is broken and reranking can't be the fix — go earlier in the pipeline. Check
whether the right chunk exists in the index at all (a parsing or chunking problem), whether
`index_version` matches what you think is indexed (a stale-index problem), or whether the query
itself needs a transformation (jargon mismatch → try HyDE or multi-query).

**Why does the pipeline check authorization twice (once via the filter, once via `_assert_authorized`)?**
Because "the filter should have handled it" is exactly the kind of assumption that, when it silently
breaks (a new vectorstore backend with a filter-translation bug, a chunk that got tagged with the
wrong `allowed_roles` during ingestion), turns into a data leak with no error message anywhere. The
second check is cheap insurance that turns a silent leak into a loud, logged, `CRITICAL` exception.

**Why is the query cache separate from the answer cache instead of just one cache?**
They cache different things at different layers and have different correctness requirements. The
retrieval cache stores *which chunks* matched a query — cheap to compute wrongly-cached (a stale
chunk list mostly just means slightly stale ranking). The answer cache stores a *full synthesized
answer* matched by semantic similarity rather than an exact key — a wrong hit there returns
confidently wrong prose to a different question, which is a much worse failure, hence the much
higher (0.95) similarity bar before it's trusted.

**If `use_mmr` is off by default, why does the pipeline embed every candidate chunk before even checking `use_mmr`?**
It doesn't — look again at `retrieve()`: the per-chunk embedding calls needed for MMR only happen
inside the `if use_mmr and len(results) > 1:` block. With MMR off (the default), that cost is never
paid.

**Does a cache hit skip the security filter?**
No — the cache key itself is namespaced by `role_scope(roles)`, so a caller with a different role set
produces a different cache key and simply cannot hit an entry cached under someone else's scope.
There's no separate "check permissions on the cached result" step because the key design makes an
unauthorized hit structurally impossible rather than something that has to be checked for on every
read (this is the same reasoning `SearchFilter.require_scope()` uses, applied to the cache layer
instead of the search layer — see ADR-012's explicit rejection of "check on read").

**What happens to a query that needed HyDE, multi-query, or decomposition — does `RetrievalPipeline` know about any of that?**
No, and that's deliberate. `query_transform.py`'s functions are called by whoever orchestrates the
conversation (the RCA agent's knowledge specialist), *before* `retrieve()` is ever invoked. The
pipeline's job is "take one standalone query string and search well"; deciding *whether* and *how* to
transform a query first is a separate concern with its own cost/benefit trade-offs per technique. This
keeps `RetrievalPipeline` simple and testable independent of which (if any) transformation produced
the query it received.

---

## Result _(illustrative — run `make bench-retrieval`)_

| configuration | recall@5 | recall@50 | mrr | p50 ms |
|---|---|---|---|---|
| hybrid + rerank, k=50 | 0.87 | 0.96 | 0.78 | 210 |
| hybrid + rerank, k=25 | 0.85 | 0.93 | 0.76 | 160 |
| hybrid, no rerank | 0.71 | 0.96 | 0.58 | 95 |
| dense only + rerank | 0.79 | 0.91 | 0.70 | 190 |
| lexical only + rerank | 0.64 | 0.82 | 0.55 | 120 |

Reranking is worth **+0.16 recall@5** here for about 115ms — the single largest lever in the whole
pipeline. Notice recall@50 is essentially unchanged with or without it: that's the point of splitting
the diagnosis this way — the reranker *reorders* a pool it doesn't widen. The candidate-depth knee
sits around `k=50`: `k=25` trades a small amount of recall@50 for meaningfully lower latency, but
pushing `k` much higher than 50 mostly just costs reranking time without moving recall further.

---

## What breaks in production

- **Cross-role cache poisoning.** A cache key that forgets `role_scope` serves one caller's answer to
  another. This is why `role_scope` is baked into the key construction itself (`_key` methods) rather
  than left to callers to remember to pass — see ADR-012.
- **Silent staleness after a re-index.** A cache key that forgets `index_version` keeps serving
  chunk ids from the previous embedding model or chunking config after a re-index, and it doesn't
  error — it just feels like quality regressed for no reason. Same fix: baked into every `_key`.
  See also the embedding stage doc's own version of this failure.
  
- **Multi-day drift into a degraded mode nobody notices.** If the embedding provider silently breaks,
  the pipeline keeps working (`bm25_only`) and keeps returning *results* — just worse ones, with no
  error anywhere in the logs to grep for. `DEGRADED_MODE` is a metric precisely so this shows up on a
  dashboard as "percentage of traffic in bm25_only mode trending up" instead of as a support ticket
  three weeks later.
- **A reranker floor copied from the wrong provider.** Local cross-encoder scores are unbounded
  logits (a sane floor is negative, like `-2.0`); Cohere's are normalized to `[0, 1]` (a sane floor is
  small and positive, like `0.02`). Copying one to the other either rejects every result or accepts
  every result. Floors are per-reranker and never portable — enforced by each adapter setting its
  own, not by a single global `settings.min_rerank_score`.
- **`parent_document` or `sentence_window` chunking without both dedupe passes.** Skipping either
  `dedupe_by_parent` or `dedupe_by_content` lets several near-identical or literally-identical results
  through, which quietly wastes most of the context budget on one section of one document instead of
  giving the LLM a genuinely diverse set of evidence. See the chunking stage doc's version of this
  same bug.
- **A conversational follow-up embedded literally.** Skipping query rewriting (or the regex gate
  mis-firing as a false *negative*, which the pattern is deliberately biased against) sends "what
  about the second one?" straight to the embedder, which retrieves near-random results with no
  error — it just looks like the model "got confused," when the actual bug is one stage earlier.
- **A budget-fitting pass that assumes uniform chunk size.** Without `_fit_budget`'s explicit
  knapsack walk, a single oversized `parent_document` chunk near the top of the ranking can silently
  consume the entire prompt budget, truncating everything after it with no error.

---

## Files

| path | what |
|---|---|
| `sentinel/retrieval/pipeline.py` | `RetrievalPipeline`, `RetrievalResult`, the degradation ladder, budget fitting, cache (de)hydration |
| `sentinel/retrieval/fusion.py` | `reciprocal_rank_fusion`, `weighted_fusion`, `maximal_marginal_relevance`, `dedupe_by_parent`, `dedupe_by_content` |
| `sentinel/retrieval/query_transform.py` | `needs_rewrite`, `rewrite_query`, `multi_query`, `hyde`, `decompose` |
| `sentinel/core/cache.py` | `EmbeddingCache`, `RetrievalCache`, `SemanticAnswerCache`, `LIVE_TOOLS`, `is_cacheable` |
| `sentinel/vectorstore/base.py` | `SearchFilter`, `BaseVectorStore.hybrid_search` (the generic fallback fusion path) |
| `sentinel/core/config.py` | every `settings.*` default named in this doc |
| `sentinel/evaluation/retrieval_eval.py` | recall@k / precision@k / MRR / nDCG computation |
| `benchmarks/retrieval_sweep.py` | the sweep that produces the result table and finds the candidate-depth knee |
| `scripts/evaluation/01_evaluate_stages.py` | prints the recall@5-vs-recall@50 diagnosis for a single run |
