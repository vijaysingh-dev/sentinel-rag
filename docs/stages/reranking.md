# Reranking

> Stage 6 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#6-reranking); this file is the deep version.
>
> Code: `sentinel/reranking/` · Scripts: `scripts/reranking/` · Benchmark: `benchmarks/retrieval_sweep.py` (reranking is measured as part of the retrieval sweep, not on its own — see [why](#common-questions))

---

## What we used

| Provider | Model | Score scale | Default floor |
|---|---|---|---|
| local (default) | `cross-encoder/ms-marco-MiniLM-L-6-v2` | unbounded logits | `-2.0` |
| cohere | `rerank-v3.5` | normalised `[0, 1]` | `0.02` |

Reranking is local-first infrastructure, the same reasoning as embedding and vector storage (ADR-029, see `docs/design-decisions.md`): `cohere` is one paid escape hatch for when local quality genuinely isn't enough, not a maintained multi-cloud matrix. Azure AI Search's native semantic ranker and Bedrock Rerank were archived at `.archive/providers/reranking/` when this stage stopped being multi-cloud.

---

## The big picture

Picture a school librarian who has to answer "which book explains why the sky is blue?" There are ten thousand books on the shelves. She can't read all ten thousand right now — that would take weeks. So she does something clever in two steps.

**Step one:** she uses the card catalog. Each book has a short summary card, written once, in advance, without knowing what anyone would ever ask. She scans the cards fast — not reading the whole book, just comparing the question against each summary — and pulls out, say, fifty books whose cards *sound* relevant. This is fast because the hard work (writing the summary) was already done ahead of time, for every book, once.

**Step two:** now that she's down to fifty books instead of ten thousand, she can afford to actually be careful. She picks each of the fifty up, reads the actual passage that seems relevant, and — now that she's looking at the real question *and* the real passage side by side — she can tell that book #12's summary sounded perfect but the passage it points to is actually about sunsets, not the sky being blue, while book #34's summary was mediocre but its passage nails the question exactly. She reorders based on this closer look and hands the asker the best five.

That's retrieval and reranking. Step one — the embedding search from the [retrieval stage](retrieval.md) — is the card-catalog pass: cheap, done ahead of time, wide net, but working from a summary rather than the real thing. Step two, this stage, is the librarian actually reading the shortlist side-by-side with the question. It's slower per item, so it only runs on the fifty candidates that already survived the first cut, not the whole corpus. That's the whole reason reranking is a *second* stage rather than just doing this from the start: doing the careful, expensive comparison for all ten thousand books would take too long, but doing it for the best fifty is affordable and catches mistakes the fast pass made.

The technical names for these two passes are **bi-encoder** (the card-catalog pass — your embedding model) and **cross-encoder** (the careful pass — the reranker). The next section explains exactly why they behave so differently, because that difference is the entire reason this stage exists as a separate step in the pipeline rather than being folded into retrieval.

---

## Why: bi-encoder vs cross-encoder

This is the distinction that justifies the stage, and the one an interviewer is most likely to probe, so it's worth being precise about the mechanics, not just the analogy.

A **bi-encoder** is your embedding model (see [`docs/stages/embedding.md`](embedding.md)). It takes the query and a candidate passage and encodes each of them **separately**, with no knowledge of the other, into a fixed-length vector. Similarity is then just the angle between those two vectors (cosine similarity). Crucially, every document in your corpus can be embedded **once, offline, at ingest time**, long before anyone types a query — that's what makes it possible to search millions of chunks in milliseconds. The cost of that scalability is that the model producing a document's vector has literally never seen the query it will eventually be compared against, so it cannot notice anything about the *relationship* between the two — only their independent positions in vector space.

A **cross-encoder** is a different kind of model entirely. Instead of encoding the query and the passage separately, it takes them **together**, concatenated into one input (`f"{query} [SEP] {passage}"` conceptually), and runs the whole pair through a transformer's attention mechanism at once. Because attention now runs *across* both texts simultaneously, the model can notice things a bi-encoder structurally cannot: that the passage contains a negation the query doesn't expect ("does NOT require a restart" vs. a query asking what *does* require one), that a qualifier changes the meaning ("only during a canary rollout"), or that two texts share a lot of vocabulary but are actually about opposite things. The cost is that this joint encoding **cannot be precomputed** — you don't know the query in advance, so you can't score a passage against it until the query actually arrives. That means a cross-encoder can only ever be run over a small number of candidates at query time, never the whole corpus.

This is why the pipeline is two stages and not one:

- **Stage one (retrieval)** uses the bi-encoder because it has to survey a huge search space cheaply. It optimizes for **recall** — did the right answer make it into the candidate pool at all?
- **Stage two (this stage, reranking)** uses the cross-encoder because it only has to judge a small shortlist carefully. It optimizes for **precision** — of the candidates that made it through, which ones are *actually* the best answer, in the right order?

**The corollary matters as much as the technique, and it's worth internalizing before anything else in this document: a reranker cannot recover a recall miss.** If the true answer never made it into the top-`k` candidates retrieval handed over, no amount of careful reordering downstream can surface it — the reranker only ever sees the candidates it's given. If your system is missing answers, check `recall@50` from the retrieval stage before touching anything here; a bigger or better reranker will not fix a candidate-generation problem. See [`docs/stages/retrieval.md`](retrieval.md#why-two-stages) for how the pipeline diagnoses which of the two stages is actually failing.

---

## The output contract: `BaseReranker` (`reranking/base.py`)

Every adapter — local or Cohere — implements the same abstract base class, so the rest of the pipeline (`retrieval/pipeline.py`) never has to know or care which one is active. That uniformity is what lets `scripts/reranking/01_rerank_compare.py` swap `--provider local` for `--provider cohere` and get a directly comparable before/after.

```python
class BaseReranker(ABC):
    name: str = "base"
    model: str = ""
    default_floor: float = 0.0

    @abstractmethod
    def _score(self, query: str, texts: list[str]) -> list[float]:
        """Returns one relevance score per candidate, in input order."""
```

`_score` is the one method each adapter actually has to implement — "given a query and a list of candidate texts, return a list of the same length with one number per candidate telling us how relevant it is." Everything else — sorting, filtering, timing, bookkeeping — lives once in the base class so no adapter can get it slightly wrong.

### `rerank(query, candidates, top_n=5, floor=None)` — what actually happens on every call

```python
def rerank(self, query, candidates, top_n=5, floor=None):
    if not candidates:
        return []
    floor = self.default_floor if floor is None else floor
    with stage_timer("rerank", self.name, candidates=len(candidates)) as extra:
        scores = self._score(query, [c.chunk.context_text for c in candidates])
        extra["scored"] = len(scores)
    ranked = sorted(zip(candidates, scores), key=lambda pair: -pair[1])
    out = []
    for rank, (item, score) in enumerate(ranked[:top_n]):
        if score < floor:
            continue
        item.rerank_score = float(score)
        item.score = float(score)
        item.rank = rank
        item.retriever = f"{item.retriever}+{self.name}"
        out.append(item)
    return out
```

Walking through this line by line, and why each line is there:

- **`if not candidates: return []`** — an empty candidate list is not an error (retrieval legitimately found nothing), so it returns an empty result rather than calling `_score` on an empty list, which some SDKs (Cohere's included) would reject or choke on.
- **`floor = self.default_floor if floor is None else floor`** — the caller can override the floor per call, but if they don't, each adapter's own calibrated default kicks in. This is deliberate: see [why floors aren't portable](#why-the-relevance-floor-is-not-optional-and-is-never-portable) below.
- **`stage_timer(...)`** — a context manager (`core/observability.py`) that times the block, records latency into Prometheus histograms, and logs `stage_failed` with the exception type if anything inside raises. It's the same timing wrapper every other stage uses, which is what lets `docs/stages/evaluation.md`-style tooling compare per-stage latency on an equal footing across the whole pipeline. `extra["scored"] = len(scores)` attaches how many candidates were actually scored to that log line, for free, without a second log call.
- **`scores = self._score(query, [c.chunk.context_text for c in candidates])`** — this line is worth stopping on: it reranks **`context_text`**, not `embed_text`. Those two fields (see [`docs/stages/chunking.md`](chunking.md#the-shared-machinery-basepy)) can differ — `embed_text` is the small, heading-prefixed string that was vectorised, while `context_text` is the larger, more readable string that gets handed to the generator. The reranker's whole job is to judge what will actually answer the user's question, and the user's question will be answered from `context_text` — the LLM never sees `embed_text` at all — so scoring anything else would be judging the wrong artifact. A chunk whose `embed_text` is a terse "Checkout Configuration > Validation > strict_mode" heading-prefixed fragment but whose `context_text` is the full paragraph needs to be scored on that full paragraph, not the search-optimized fragment.
- **`ranked = sorted(zip(candidates, scores), key=lambda pair: -pair[1])`** — sorts *all* scored candidates, not just the ones that will be returned, descending by score (the `-pair[1]` negation turns Python's default ascending sort into descending without a separate `reverse=True`, which is just a style choice, not a behavioral one).
- **`for rank, (item, score) in enumerate(ranked[:top_n])`** — only the top `top_n` (default 5, but the pipeline actually passes `settings.rerank_top_n`, default 6 — see [defaults](#why-these-defaults) below) are considered for return at all. The floor check happens **inside** this loop, i.e., only against the already-best `top_n` candidates — the floor never has a chance to reject a candidate ranked 7th in favor of promoting one ranked 8th; it can only shrink the top slate, never reorder it.
- **`item.rerank_score = float(score); item.score = float(score)`** — both fields get updated. `rerank_score` preserves what the reranker specifically said (kept as its own field on `RetrievedChunk` for diagnostics — see [why keep multiple score fields](#common-questions) below); `score` is overwritten because it's the field everything downstream (assembly, logging, the UI) reads as "the" relevance score, and after reranking, the reranker's opinion is the most informed one available.
- **`item.retriever = f"{item.retriever}+{self.name}"`** — appends `+local` or `+cohere` onto whatever the retriever field already said (e.g. `"hybrid"` becomes `"hybrid+local"`). This is a cheap, free audit trail: reading `item.retriever` off a final result tells you the whole provenance of how that chunk got there without needing to cross-reference a separate log line.

### `async def arerank(...)` — the async twin

```python
async def arerank(self, query, candidates, top_n=5, floor=None):
    import asyncio
    return await asyncio.to_thread(self.rerank, query, candidates, top_n, floor)
```

The base class's `arerank` just runs the synchronous `rerank` in a thread pool (`asyncio.to_thread`) rather than reimplementing the logic. That's fine for the local cross-encoder, whose actual work is CPU-bound (a real network call gains nothing from `asyncio` unless it's genuinely non-blocking I/O). `CohereReranker` overrides `arerank` itself (see below) precisely because it *is* I/O-bound and benefits from retry logic on the async path specifically — that override, and the fact that it's easy to miss, is called out in [what breaks](#what-breaks-in-production).

### `describe()` — the introspection hook

```python
def describe(self) -> dict[str, Any]:
    return {"provider": self.name, "model": self.model, "default_floor": self.default_floor}
```

A small, uniform way for logging, the doctor script, or a debugging session to ask "what reranker am I actually running, on what model, with what floor?" without reaching into private attributes. `CrossEncoderReranker` extends it with the device the model landed on (`cpu` vs `cuda`), which matters a lot for the latency numbers in the [result table](#result-illustrative).

---

## Per-provider mechanics

### local (`local_reranker.py`) — `CrossEncoderReranker`

```python
class CrossEncoderReranker(BaseReranker):
    name = "local"
    default_floor = -2.0  # ms-marco logits; calibrate on your own golden set

    def __init__(self, model=None, device=None, batch_size=32, max_length=512):
        from sentence_transformers import CrossEncoder
        self.model = model or settings.local_rerank_model
        self.batch_size = batch_size
        self._encoder = CrossEncoder(self.model, device=device, max_length=max_length)

    def _score(self, query, texts):
        pairs = [(query, text) for text in texts]
        return [float(s) for s in self._encoder.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)]
```

- **The `sentence_transformers` import lives inside `__init__`, not at module top level.** This is a repo-wide convention (see CLAUDE.md's "adding a provider" section): the provider registry loads adapters lazily, so someone running only the OpenAI generation path never has to have `sentence-transformers` (and its PyTorch dependency, which is large) installed at all. Importing at call time rather than import time is what makes that possible.
- **`model or settings.local_rerank_model`** — `settings.local_rerank_model` defaults to `"cross-encoder/ms-marco-MiniLM-L-6-v2"`, but any caller can override it per-instance (useful for the bake-off in `benchmarks/retrieval_sweep.py`, which needs to try several models in one process).
- **`pairs = [(query, text) for text in texts]`** — this is the concrete, literal expression of "cross-encoding": the model's `predict` method is handed a list of `(query, candidate)` **pairs**, not two separate lists. That pairing is what lets the underlying transformer attend across both texts jointly, which is the entire mechanical difference from the embedding model discussed above.
- **`batch_size=32`** — MiniLM is small enough that batches of 32 pairs process comfortably on CPU without blowing memory; it's a throughput knob, not a correctness one, and can be raised on a GPU.
- **`max_length=512`** — the model's own maximum input length in tokens for the *combined* query+passage pair (not per-text). Longer inputs get silently truncated by the underlying tokenizer — worth knowing if you ever wonder why a very long `context_text` doesn't seem to be fully considered.

### cohere (`cohere_reranker.py`) — `CohereReranker`

```python
class CohereReranker(BaseReranker):
    name = "cohere"
    default_floor = 0.02  # normalised [0,1]; well below this is noise

    def _score(self, query, texts):
        try:
            response = self._client.rerank(model=self.model, query=query, documents=texts)
        except ApiError as exc:
            if exc.status_code == 429:
                raise RateLimited(str(exc)) from exc
            raise
        scores = [0.0] * len(texts)
        for result in response.results:
            scores[result.index] = result.relevance_score
        return scores
```

- **The re-projection step (`scores[result.index] = ...`) is not decorative.** Cohere's API returns results **sorted by relevance**, each carrying an `index` field pointing back at its position in the original `documents` list you sent — it does not return scores in input order. If the adapter just zipped `response.results` against `texts` positionally, every score would be silently attached to the wrong candidate. Building a same-length `scores` array and writing into it by `result.index` is what keeps the contract with the base class (`_score` returns scores "in input order") honest.
- **`ApiError` with `status_code == 429` is translated into `RateLimited`** (`core/resilience.py`), a type the rest of the pipeline's retry logic already knows how to recognize and back off on, rather than an opaque Cohere-specific exception type leaking out of this module. Every other `ApiError` is re-raised unchanged — this adapter only special-cases the one error type it has a specific answer for.
- **`async def arerank` is overridden**, unlike the local adapter which relies on the base class's generic thread-pool wrapper:

```python
async def arerank(self, query, candidates, top_n=5, floor=None):
    async def call():
        return await asyncio.to_thread(self.rerank, query, candidates, top_n, floor)
    return await with_retry(call, attempts=3, operation="cohere_rerank")
```

  This wraps the same thread-pool call the base class would make, but adds `with_retry` (`core/resilience.py`, `attempts=3`) around it — because a network call to a third-party API can transiently fail or rate-limit in ways a local in-process model call structurally cannot. The **synchronous** `rerank()` inherited from the base class does *not* get this retry wrapping, since it's never overridden — see [what breaks](#what-breaks-in-production) for why that asymmetry matters if you ever call the sync path against Cohere from a script.
- **Cohere's own request-size truncation is not reimplemented here.** If a `context_text` is very long, Cohere's API truncates it before scoring on its own terms; the adapter doesn't attempt to replicate or pre-truncate to match, because doing so without knowing Cohere's exact tokenizer would just be a second, possibly-wrong guess at the same problem.

---

## Why the relevance floor is not optional, and is never portable

The floor is what lets the system answer **"I don't have anything relevant to that"** instead of confidently presenting the five least-bad chunks it happened to retrieve. Without a floor, every query gets an answer, even when the honest answer is "this isn't documented" — and in an operations assistant giving root-cause guidance, a fluent wrong answer is far more dangerous than a visible "I don't know," because someone under incident pressure may act on it. This is the same philosophy behind grounding and citation validity elsewhere in the pipeline (see [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md), Non-negotiable invariants in `CLAUDE.md`): the system is built to refuse rather than fabricate.

**Floors are calibrated per reranker and must never be copied between them, because the underlying score scales are unrelated numbers that happen to share the name "score."**

- The local cross-encoder (`ms-marco-MiniLM-L-6-v2`) emits **raw, unbounded logits** — the direct output of a neural network's final layer, with no normalization applied. These can be any real number; there's no guaranteed range. A logit of `-2.0` roughly marks the boundary past which the model is confidently saying "not relevant," but that number is an empirical observation about *this specific model*, not a property of relevance in general.
- Cohere's `rerank-v3.5` emits a **normalised score in `[0, 1]`**, guaranteed by Cohere's API regardless of model version. A score near `0.02` is close to the bottom of that range — "essentially noise."

If you took the local model's floor (`-2.0`) and applied it to Cohere's `[0, 1]` scale, **every single result would pass** — `-2.0` is below anything Cohere could ever emit — so the floor would do nothing at all, silently disabling the refusal behavior. If you did the reverse and applied Cohere's `0.02` floor to the local model's unbounded logits, you'd likely **reject almost everything**, since a "very relevant" logit from MiniLM might easily be a number like `-0.3` or `1.5`, both of which are `< 0.02`'s neighborhood-implying-normalized-scale but mean something completely different in logit-space. Either mistake is silent — nothing errors, the system just quietly stops refusing (or starts over-refusing) — which is exactly why it's worth stating explicitly rather than trusting it to be obvious.

---

## Why these defaults

| constant | value | where | derivation |
|---|---|---|---|
| local floor | `-2.0` | `local_reranker.py` | ms-marco-MiniLM logits are unbounded; empirically, scores comfortably above 0 are clearly relevant and scores well below 0 are clearly not. `-2.0` is a conservative cushion below the boundary rather than exactly at it — the docstring itself says "calibrate on your own golden set," i.e. this is a reasonable starting prior, not a value fitted against this repo's own corpus. Per CLAUDE.md's "when numbers appear in documentation," treat it as illustrative until you've run `parsing_eval.py`'s sibling for retrieval (`evaluation/retrieval_eval.py`) against a labelled set and looked at where true-relevant vs true-irrelevant scores actually separate. |
| cohere floor | `0.02` | `cohere_reranker.py` | Cohere guarantees a normalised `[0, 1]` scale, so a floor near the bottom of that range is a much easier number to reason about than the local model's — "close to zero" straightforwardly means "the model saw almost nothing in common." `0.02` rather than `0.0` leaves a small margin so a literal zero (which can happen with truly unrelated text) and near-zero noise are both excluded. |
| `settings.rerank_top_n` | `6` | `core/config.py` | what actually reaches the prompt after reranking. Six is small enough to keep the generator's context focused (recall from [`docs/stages/chunking.md`](chunking.md) that a vector, and by extension a chunk, is worst when it's asked to represent too much at once — the same logic applies to a *set* of chunks handed to an LLM: more isn't free, it's competing context) while leaving room for a question that genuinely needs a few different passages to answer (a comparison question, for instance). |
| `settings.min_rerank_score` | `0.0` | `core/config.py` | the *pipeline-level* override floor, separate from each adapter's own `default_floor`. It defaults to `0.0`, and `retrieval/pipeline.py` passes it as `floor=settings.min_rerank_score or None` — because `0.0 or None` evaluates to `None` in Python, the default value of `0.0` actually means "don't override; let the adapter's own calibrated floor apply." This is a deliberate way of encoding "no override" without a separate `None`-typed setting field, but it also means setting `MIN_RERANK_SCORE=0.0` explicitly in `.env` is indistinguishable from never setting it — see [what breaks](#what-breaks-in-production). |
| candidate pool width (`k`, set by retrieval) | wider than `top_n` | `retrieval/pipeline.py` | not a reranking constant per se, but load-bearing for this stage: the reranker needs genuine room to *reorder*. If retrieval only ever hands over 5 candidates and `top_n` is also 5, the reranker can only ever shuffle the same five items — it cannot pull in a 6th candidate that retrieval ranked low but is actually the best answer. See [`docs/stages/retrieval.md`](retrieval.md) for the `k=50` default this stage receives from. |
| batch size (local) | `32` | `local_reranker.py` | a throughput knob for `CrossEncoder.predict`, not an accuracy one — chosen to keep memory comfortable on CPU while still batching enough pairs at once to amortize per-call overhead. Raise it if you have GPU memory to spare; lower it if you see out-of-memory errors on a constrained machine. |
| `max_length` (local) | `512` | `local_reranker.py` | the underlying MiniLM model's own trained maximum sequence length for the combined query+passage pair; not a value this codebase chose, but a property of the pretrained model being used. |

---

## Common questions

**Why does `RetrievedChunk` keep `dense_score`, `sparse_score`, *and* `rerank_score` instead of just one `score`?** Because each one answers a different diagnostic question, and collapsing them early throws that information away. A chunk with a high dense (embedding) score but a low rerank score tells you the embedding was fooled by surface-level similarity — words that *sound* related without the passage actually answering the question — while a chunk that only has a `sparse_score` (BM25, keyword match) and no meaningful dense contribution tells you the query was more lexical (an exact error code, say) than semantic. Keeping all three around, even after `score` is overwritten with the final reranked value, means a debugging session or an evaluation report can still ask "was this a ranking-quality problem or a candidate-generation problem?" after the fact. See `core/types.py`'s docstring on `RetrievedChunk` for the same point in the source.

**If reranking is strictly better, why not just rerank everything and skip the bi-encoder search entirely?** Because a cross-encoder scores one query-passage pair at a time and cannot be precomputed — running it over an entire corpus of thousands or millions of chunks for every single query would be far too slow for an interactive system. The bi-encoder's whole value is that document embeddings are computed once, offline, and then a fast vector search narrows a huge corpus down to a small, affordable shortlist before the expensive, accurate model ever runs. This stage is only affordable *because* it's the second stage.

**What happens if the reranker is unavailable — a network outage for Cohere, or a missing model file locally?** `retrieval/pipeline.py` resolves the reranker lazily and catches the exception if it can't be constructed, logging `reranker_unavailable` and falling back to running with `reranker = None`. When that happens the pipeline increments `DEGRADED_MODE.labels("retrieval", "no_rerank")` — a Prometheus counter — so a production system that's silently lost reranking for a day shows up as a visible metric rather than as a slow, unexplained quality regression nobody notices until someone complains. The same thing happens if a call to `reranker.rerank(...)` itself raises mid-run (`rerank_failed`), not just at construction time. See `docs/stages/retrieval.md`'s "Degradation ladder" for the parallel idea applied to the retrieval arms themselves.

**Why is `context_text`, not `embed_text`, scored here?** Because the reranker's job is to judge what the user's question will actually be answered *from*, and the generator only ever sees `context_text` (see [`docs/stages/chunking.md`](chunking.md#the-shared-machinery-basepy) for the two-field chunk design). Scoring `embed_text` instead would mean optimizing the ranking for a string the LLM never reads.

**Does latency from reranking matter, and what can be done about it?** Yes — reranking is the second-largest latency contributor in the pipeline after generation itself (see [`docs/stages/retrieval.md`](retrieval.md)'s result table: reranking adds roughly 115ms in the illustrative benchmark, for a large jump in recall@5). `arerank` exists specifically so reranking can run concurrently with other independent work in an async pipeline rather than blocking serially, and Cohere's network round-trip in particular benefits from not blocking the event loop while waiting on it.

**Is a bigger, slower reranker always better?** Not necessarily, and this is worth measuring rather than assuming — `BAAI/bge-reranker-large` (mentioned in `local_reranker.py`'s docstring) is stronger than MiniLM but meaningfully slower; whether that trade is worth it depends entirely on your latency budget and how much headroom recall@50-vs-recall@5 shows there is left to gain from better ranking. Per CLAUDE.md's rule on retrieval-quality changes: run `make eval` before and after, and look at the bootstrap confidence interval, not just the point estimate.

**Can I mix `min_rerank_score` overrides safely across both providers?** Only with real caution — because `settings.min_rerank_score` is a single global setting applied identically regardless of which reranker is active, setting it to a nonzero value calibrated for one provider's scale will silently misbehave against the other (see [why floors aren't portable](#why-the-relevance-floor-is-not-optional-and-is-never-portable)). In practice this means: leave `MIN_RERANK_SCORE` at its default of `0.0` (meaning "use the adapter's own floor") unless you've deliberately chosen one reranker and calibrated the override specifically for its scale.

---

## Result _(illustrative — see `benchmarks/retrieval_sweep.py`)_

| configuration | recall@5 | recall@50 | mrr | p50 ms |
|---|---|---|---|---|
| hybrid + rerank, k=50 | 0.87 | 0.96 | 0.78 | 210 |
| hybrid, no rerank | 0.71 | 0.96 | 0.58 | 95 |

Reranking is worth **+0.16 recall@5** here for roughly 115ms of added latency — the single largest lever in the whole retrieval pipeline. The important detail is that `recall@50` is **identical** with and without it: reranking never adds a candidate that wasn't already retrieved, it only reorders the ones that were. That is the concrete, measured expression of "a reranker cannot recover a recall miss" from earlier in this document. Per CLAUDE.md's rule on illustrative numbers, don't quote these as measured — run the benchmark against your own corpus.

---

## What breaks in production

- **Reranking a pool that's too narrow.** If the candidate set handed to `rerank()` is barely wider than `top_n`, the reranker has almost no room to actually reorder anything — it can only reshuffle the same handful of items retrieval already picked. `k` (the retrieval candidate depth) needs to be meaningfully wider than `top_n`; see the [defaults table](#why-these-defaults) above.
- **Reranking the wrong text.** `BaseReranker.rerank` deliberately scores `context_text`, not `embed_text` — if a future change to this code (or a custom adapter) accidentally scores `embed_text` instead, the reranker would be optimizing for a heading-prefixed search fragment instead of what the generator actually reads, silently degrading answer quality without any visible error.
- **Copying a floor between providers.** As covered above, `-2.0` and `0.02` mean opposite things on their respective scales; copying one to the other either disables the "I don't know" refusal entirely or rejects almost everything.
- **The `settings.min_rerank_score` default being indistinguishable from "not set."** Because `0.0 or None` is `None` in Python, there's no way to tell from the config alone whether someone deliberately wants a floor of exactly `0.0` (disabling filtering) or simply never touched the setting. In practice this means `0.0` behaves as "adapter default," not as a literal zero floor — worth knowing before assuming a value in `.env` is doing what its number suggests.
- **The synchronous `rerank()` path against Cohere has no retry wrapping.** `CohereReranker` only adds `with_retry` around its **async** `arerank` override; a script or tool calling the inherited synchronous `rerank()` directly against Cohere gets no automatic retry on a transient failure or rate limit, unlike the production query path (which is async). This is fine for the production pipeline (which always goes through `arerank`), but a footgun for a quick script or REPL session calling `rerank()` directly and being surprised by an unhandled `RateLimited`.
- **Latency budget creep.** Reranking is the second-largest latency contributor after generation. If a system starts feeling slow, check whether reranking's candidate pool (`k`) quietly grew, or whether a larger, slower model (like `bge-reranker-large`) got swapped in without re-measuring the latency trade.
- **A dead or misconfigured reranker degrading silently.** `retrieval/pipeline.py` catches both construction failures and per-call failures and falls back to `reranker=None`, incrementing `DEGRADED_MODE`. This is the *correct* behavior (degrade, never crash — see the agent layer's philosophy in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#9-agent-layer)), but it means a broken reranker doesn't throw a loud error — it just quietly stops improving answer quality. Watch the `DEGRADED_MODE` metric, not application logs, to catch this in production.

---

## Files

| path | what |
|---|---|
| `sentinel/reranking/base.py` | `BaseReranker`, the shared `rerank`/`arerank`/`describe` logic, the floor-filtering contract |
| `sentinel/reranking/local_reranker.py` | `CrossEncoderReranker` — sentence-transformers MiniLM cross-encoder |
| `sentinel/reranking/cohere_reranker.py` | `CohereReranker` — hosted rerank API, retry-wrapped async path |
| `sentinel/core/types.py` | `RetrievedChunk` — the three-score-field model this stage writes into |
| `sentinel/core/config.py` | `RerankProvider`, `rerank_top_n`, `min_rerank_score`, `local_rerank_model`, `cohere_rerank_model` |
| `sentinel/core/registry.py` | how `rerank_provider` resolves to an adapter class |
| `sentinel/core/resilience.py` | `RateLimited`, `with_retry` — used by the Cohere adapter's async path |
| `sentinel/retrieval/pipeline.py` | where reranking is actually invoked, and where it degrades on failure |
| `scripts/reranking/01_rerank_compare.py` | before/after position-change demo — run this to see reranking in action on real queries |
| `benchmarks/retrieval_sweep.py` | the bake-off that measures reranking's contribution alongside retrieval configuration |
| `.archive/providers/reranking/` | the retired Azure semantic ranker and Bedrock Rerank adapters, with a README on reviving one |
