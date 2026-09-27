# Embedding

> Stage 3 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#3-embedding); this file is the deep version.
>
> Code: `sentinel/embedding/` · Scripts: `scripts/embedding/` · Benchmark: `benchmarks/embedding_bakeoff.py`

---

## What we used

| Provider | Model | Dims | Notes |
|---|---|---|---|
| local (default) | BAAI/bge-base-en-v1.5 (sentence-transformers) | 768 | free, private, no rate limit |
| openai | text-embedding-3-small / text-embedding-3-large | 1536 / 3072, truncatable down to 256 | one paid escape hatch, not a cloud matrix |

Embedding is **local-first infrastructure, not a cloud-account choice** — ADR-029 in `docs/design-decisions.md`. `openai` exists for when local quality genuinely isn't enough on your corpus, not because there's a maintained Azure/AWS path to keep symmetric with parsing. The old `AzureOpenAIEmbedder` and Bedrock (Titan/Cohere) embedding adapters are archived at `.archive/providers/embedding/` with a README on reviving them if that ever changes.

---

## The big picture

Every earlier stage in this pipeline — parsing, chunking — deals with *text*. Embedding is the stage where text stops being text and becomes a list of numbers, and that switch is the whole reason a RAG system can search a million documents in milliseconds instead of reading all of them.

Here's the idea in plain terms. Imagine every sentence in your knowledge base gets assigned a point in space — not 2D or 3D space, but a space with 768 (or 1536, or 3072) dimensions. A model reads the sentence and decides where that point goes, based on *meaning*, not spelling. "The checkout service is down" and "the payments API stopped responding" end up near each other in this space, even though they don't share a single important word, because the model has learned that they're about similar things. "The checkout service is down" and "the recipe for lasagna" end up far apart. That's it — that's an embedding. It's a coordinate for meaning.

Once every chunk in your corpus has a coordinate, answering a question becomes a geometry problem: embed the question the same way, find the chunks whose coordinates are closest, and those are (probably) the relevant ones. "Closest" is measured with **cosine similarity** — imagine drawing an arrow from the center of the space to each point; cosine similarity asks how similar the *angle* of two arrows is, ignoring how long they are. Two arrows pointing the same direction score close to 1; perpendicular arrows score 0; opposite arrows score -1. This is why the code below cares so much about normalizing vectors to a consistent length — once every arrow is the same length, angle is the only thing left to compare, and angle is what "meaning" translates to in this geometry.

This stage's whole job, then, is: turn a chunk's `embed_text` into one of these coordinate vectors, do it fast and cheaply enough to run over an entire corpus, and do it in a way that a *question* and a *passage* — which are grammatically different kinds of text — end up comparable in the same space. That last part turns out to be full of sharp edges, and most of `sentinel/embedding/base.py` exists to keep you from cutting yourself on them.

---

## Why the base class enforces query/document asymmetry

This is the single most important idea in this stage, and it's also the easiest one to get wrong without anything telling you that you did.

Some embedding models — BGE and E5 are the ones used here — were **trained** with an instruction prefix glued onto the query side only. During training, every "query" example the model ever saw started with a string like `"Represent this sentence for searching relevant passages: "`, but the "passage" examples it was compared against never had that prefix. The model learned a slightly different internal representation for "this is something someone is asking" versus "this is a fact sitting in a document." If you embed a query without that prefix, you're feeding the model an input shape it was never trained to expect for that role, and it embeds it more like a passage than a question — which quietly narrows the gap between how close a *relevant* passage looks and how close an *irrelevant* one looks. Recall drops by several points and there is no error, no warning, no crash. The system just gets a bit worse at finding the right thing, and everyone assumes that's just how good the model is.

Because this bug produces silence instead of a stack trace, `BaseEmbedder` (in `sentinel/embedding/base.py`) makes it structurally impossible to write by accident: `embed_query` and `embed_documents` are two *separate methods*. An adapter has to deliberately implement `_apply_prefix` to treat the two differently — there is no code path where a caller can pass a query into the "documents" method and vice versa without explicitly choosing to. `scripts/embedding/01_embed_local.py` demonstrates the cost directly: it embeds the same query correctly (with the prefix) and incorrectly (as if it were a passage), and prints the margin — the gap between "how similar is this to the right answer" and "how similar is this to a wrong-but-related distractor" — in both cases. The margin shrinks when the prefix is dropped. That's the whole bug, made visible.

---

## Function-by-function walkthrough

### `BaseEmbedder` (`sentinel/embedding/base.py`)

This is an abstract base class — a contract that every provider (`LocalEmbedder`, `OpenAIEmbedder`) has to fulfill. Almost none of the interesting logic lives in the providers; it lives here, once, so no provider can accidentally skip it.

#### `_embed_batch(texts, is_query)` — abstract, provider-specific

Every concrete embedder must implement this. It receives text that has **already** been prepared (prefixed and truncated — see `_prepare` below) and just has to call whatever SDK or model actually turns text into vectors. This is the only method a new provider is required to write; everything else — caching, batching, truncation, the query/document split — is inherited for free. That's deliberate: it means a bug in caching or truncation can only exist in one place, not once per provider.

#### `embed_documents(texts)` — the passage path

This is the method ingestion calls to embed a whole batch of chunks. Walking through what it actually does:

1. If `texts` is empty, return `[]` immediately — a guard against wasting a network round trip or a cache lookup on nothing.
2. Every text is run through `_prepare(text, is_query=False)` — applies the document-side prefix (if the model wants one) and enforces the token limit.
3. It asks the cache for all of them at once (`_cache.get_many`), which is a single batched lookup rather than one call per text — this matters a lot at corpus scale, since a 10,000-chunk re-ingestion run doing 10,000 individual cache round trips would spend more time on network chatter than on anything useful.
4. Whatever the cache didn't have (`missing_idx`) gets embedded for real, **in batches of `self.batch_size`** — not all at once. Sending an entire 10,000-chunk corpus to a model or an API in one call would either exceed a request size limit, exhaust memory, or (for the API case) make a single failure cost you re-embedding everything instead of one batch.
5. `stage_timer(...)` wraps the actual embedding work so the pipeline's observability layer can report how long embedding took and how many were served from cache versus computed fresh — this is what lets you look at a dashboard and know a corpus re-ingest is "10,000 chunks, 9,400 cached, 600 computed" instead of just "it took 40 seconds."
6. Freshly computed vectors are written back into the cache (`put_many`) so the *next* ingestion of the same text costs nothing.
7. The final return re-assembles the list in the original order, mixing cache hits and fresh results — callers never need to know which was which.

Why go to all this trouble instead of just calling `_embed_batch` on everything every time? Because ingestion is re-run constantly — every time you tweak an unrelated document, regenerate the corpus, or re-run a benchmark, most of the text is unchanged from last time. Without the cache, every one of those runs would re-pay the full embedding cost (money for OpenAI, GPU-minutes for local) for text that produces the exact same vector it produced last time.

#### `embed_query(text)` — the question path

Much simpler: one text, one call, `is_query=True`, no batching, and **no caching**. The docstring explains why it isn't cached here: "the retrieval cache upstream already covers the repeated-query case at a more useful granularity." In other words, caching a raw embedding vector only saves you the cost of computing that vector; caching the *retrieval result* (which chunks were returned) saves you the cost of computing the vector *and* running the vector search *and* the reranker. `RetrievalCache` (in `sentinel/core/cache.py`) already does the more valuable version of this, so duplicating a cheaper version here would only add complexity without adding value.

#### `aembed_documents` / `aembed_query` — async wrappers

These exist so the rest of the pipeline (which is largely `async`, since it's serving a chat interface that shouldn't block) can call embedding without stalling the event loop. The base class's default implementation is `asyncio.to_thread(...)` — run the normal synchronous method on a background thread. `OpenAIEmbedder` overrides `_aembed_batch` with a version that adds retry logic (see below), because a real network call benefits from more than just "don't block the event loop" — it also needs to survive a rate limit.

#### `embed_chunks(chunks)` — the actual ingestion entry point

```python
def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
    vectors = self.embed_documents([c.embed_text for c in chunks])
    return [EmbeddedChunk(chunk=chunk, vector=vector, model=self.model, dims=self.dims)
            for chunk, vector in zip(chunks, vectors, strict=True)]
```

One line does all the work, and it's the most important line in the whole stage: `[c.embed_text for c in chunks]`. Not `context_text`. If you've read [`docs/stages/chunking.md`](chunking.md), you know every `Chunk` carries two text fields — a small, precise `embed_text` meant for vectorization, and a larger, self-contained `context_text` meant for the generator to read. This is the one place in the codebase where that split actually gets *acted on*. Get this line backwards — embed `context_text` instead — and nothing crashes. The pipeline runs, chunks get vectors, search returns results. But you've silently thrown away the entire reason the two-field chunk design exists: a sentence-window chunk's `embed_text` is one precise sentence, while its `context_text` is up to seven sentences of surrounding text. Embed the seven-sentence version and you've turned a precision instrument back into a blunt one, and you'll never see an error telling you so — only slightly worse recall that looks like "the embedding model isn't very good."

`EmbeddedChunk` bundles the vector together with `model` and `dims` — stamping *which* model produced this vector directly onto the result, because a vector is meaningless without knowing what produced it (a 768-dimension BGE vector and a 768-dimension E5 vector are not comparable numbers even though they're the same shape).

#### `_prepare(text, is_query)` — where prefixing and truncation actually happen

```python
def _prepare(self, text: str, is_query: bool) -> str:
    text = self._apply_prefix(text, is_query)
    if ntokens(text) > self.max_input_tokens:
        self._truncations += 1
        if self._truncations in (1, 10, 100, 1000):
            log.warning("embedding_truncated", ...)
        text = truncate_tokens(text, self.max_input_tokens)
    return text
```

This is called by both `embed_documents` and `embed_query` before anything reaches the network or the model. Two things happen, in order: the prefix is applied first (so the *prefix itself* counts against the token budget — a model with a long instruction prefix has slightly less room for the actual text), then the combined text is measured with `ntokens` (the same tiktoken-based ruler `chunking/base.py` uses — see `docs/stages/chunking.md` for why a single ruler, even an imperfect one, is used consistently across every stage).

Why does this matter at all, given that chunking already sized chunks to fit a target token budget? Because **every embedding model has its own maximum sequence length, and it silently discards anything past it.** A chunker tuned for a 900-token budget produces chunks that are fine for `text-embedding-3-large` (8191 tokens) but would get truncated by a hypothetical 512-token model. `_prepare` doesn't prevent that — it can't, the model's limit is what it is — but it makes the truncation **loud** instead of silent: `self._truncations` counts every occurrence, and a warning fires at 1, 10, 100, and 1000 truncations (an exponentially-spaced schedule so you get told immediately the first time, and then periodically as it keeps happening, without your logs filling up with the same warning ten thousand times on a bad day). `truncation_count` is exposed as a public property specifically so `benchmarks/embedding_bakeoff.py` and ingestion scripts can report it — a nonzero, climbing truncation count is the signal that your chunk sizes and your embedding model's limit have drifted out of alignment.

#### `_apply_prefix(text, is_query)` — the hook, not the policy

The base class's default is a no-op: return the text unchanged. This is intentional — most embedding models (including OpenAI's) were **not** trained with an asymmetric prefix convention, so "do nothing" is the correct default, and only the adapters that need the special handling (`LocalEmbedder`, for BGE/E5) override it.

#### `describe()` and `__repr__`

Small utility methods that report the embedder's configuration (`provider`, `model`, `dims`, `max_input_tokens`, whether dimension reduction is supported) as a plain dict, and a readable string for logs and debugging respectively. `LocalEmbedder` and `OpenAIEmbedder` each extend `describe()` with a provider-specific detail (`device`/`normalized` for local, `matryoshka` for OpenAI) by calling `**super().describe()` and adding to it — a small pattern worth noticing because it recurs: extend, don't reimplement.

---

### `LocalEmbedder` (`sentinel/embedding/local_embedder.py`)

Wraps `sentence-transformers`, the library that makes running an embedding model on your own machine (CPU or GPU) about five lines of code.

#### `__init__`

Loads the model (`SentenceTransformer(self.model, device=device)` — this downloads the model from Hugging Face the first time and caches it locally after that), then works out two numbers it needs and can't just assume:

- **`dims`** — how many numbers are in each output vector. Looked up first in the hardcoded `KNOWN_DIMS` table; if the model isn't in that table, it falls back to asking the loaded model directly (`get_embedding_dimension()`). The honest answer for *why* a hardcoded table exists alongside a method that can compute the same thing at runtime: it's a fast, dependency-free source of truth for the handful of models this codebase actually uses and benchmarks, useful for documentation and for any code that wants a model's dimensionality without loading the (sometimes multi-hundred-MB) model file first. For anything not in the table, the runtime lookup is the correct fallback rather than a hard failure.
- **`max_input_tokens`** — `min(self._st.max_seq_length or 512, 512)`. Two things are happening in one line: if the library can't report a max sequence length at all, assume 512 (a safe, conservative default for this class of model); and *even if* the model reports something larger, cap it at 512 anyway. Why cap a number that might legitimately be bigger? Because "max sequence length" as reported by the tokenizer config and "the length the model was actually *trained* to perform well at" are not always the same number — some models report a larger technical limit than the window they were meaningfully trained on, and quality quietly degrades past the real training window even though nothing prevents you from feeding in more tokens. 512 is BGE-base's real trained context, so the cap here is "don't trust a number bigger than what this family of model was actually built for."

#### `_apply_prefix(text, is_query)`

```python
def _apply_prefix(self, text: str, is_query: bool) -> str:
    table = QUERY_PREFIXES if is_query else DOC_PREFIXES
    return table.get(self.model, "") + text
```

This is the concrete implementation of the asymmetry the base class's design makes room for. `QUERY_PREFIXES` and `DOC_PREFIXES` are plain dictionaries keyed by exact model name — deliberately **not** a heuristic like "if the model name contains 'bge', add this prefix." An explicit table means a model that isn't listed gets no prefix (the safe default — an incorrect prefix could be worse than none), and adding support for a new model is one dictionary entry, reviewable in a diff, rather than a guess embedded in string-matching logic that might silently misfire on a model it wasn't tested against.

#### `_embed_batch(texts, is_query)`

Calls `self._st.encode(...)` with `normalize_embeddings=self.normalize` (default `True`) and `convert_to_numpy=True`, then converts to a plain Python list of lists with `.tolist()` (because the rest of the pipeline — JSON serialization for caching, the vector store's insert calls — expects plain lists, not numpy arrays). `is_query` is accepted for interface symmetry with the base class but isn't otherwise used here — sentence-transformers' `encode()` doesn't need to know whether it's encoding a query or a document; the asymmetry was already handled by `_apply_prefix` before this method ever sees the text.

**Why `normalize=True` matters, concretely:** an embedding vector has both a *direction* (what it means) and a *length* (roughly, how much the model "had to say"). Longer, more information-dense text tends to produce longer raw vectors. If you don't normalize, comparing vectors with a dot product silently favors longer documents — not because they're more relevant, but because their vectors are longer. Normalizing every vector to length 1 removes that bias entirely: once every vector has the same length, dot product, cosine similarity, and (for unit vectors) Euclidean distance all agree with each other and all measure the same thing — direction, i.e., meaning.

---

### `OpenAIEmbedder` (`sentinel/embedding/openai_embedder.py`)

The one non-local option — a thin wrapper around `api.openai.com`, deliberately minimal (per the module docstring: "the escape hatch for when local embedding quality isn't enough, not a second maintained cloud path"). Not Azure OpenAI — no deployment name indirection, no managed identity, just `model="text-embedding-3-large"` straight in the API call.

#### `__init__`

Resolves the model name and its dimensionality (`MODEL_DIMS`, a hardcoded table — same reasoning as `KNOWN_DIMS` above: a fast, explicit source of truth for the handful of models this repo actually supports). Then the interesting part: **Matryoshka support detection.**

```python
self.supports_dimension_reduction = any(m in self.model for m in MATRYOSHKA_CAPABLE)
self.dims = dims if (dims and self.supports_dimension_reduction) else base_dims
self._request_dims = dims if (dims and self.supports_dimension_reduction) else None
```

Only the `-3-large` / `-3-small` family supports truncating the output dimensionality; the older `text-embedding-ada-002` will reject a `dimensions=` parameter outright if you send one (it's not that it ignores it politely — the API call errors). So `supports_dimension_reduction` is checked *before* anything tries to pass `dimensions=`, and `_request_dims` stays `None` for any model or any caller that didn't explicitly ask for a smaller size — meaning the OpenAI-recommended default behavior (full native dimensionality) is what you get unless you opt into truncation.

#### `_embed_batch(texts, is_query)`

Builds the request, conditionally including `dimensions=` only when `_request_dims` is set, calls the API, and does two things worth calling out:

- **Catches `RateLimitError` and re-raises it as this codebase's own `RateLimited` exception.** This is a small but important piece of plumbing: `sentinel/core/resilience.py` implements one shared retry/backoff policy for the whole pipeline, and it only knows how to recognize `RateLimited`, not every SDK's provider-specific exception type. Translating OpenAI's exception into the shared one means the retry logic doesn't need a special case per provider — every adapter across parsing, embedding, reranking and generation that might hit a rate limit funnels into the same backoff behavior.
- **Re-sorts the response by index before returning.** `response.data` — the OpenAI API does not promise it will return embeddings in the same order you sent the input texts. `sorted(response.data, key=lambda d: d.index)` fixes that. This is an easy bug to never notice in testing (small batches often *happen* to come back in order) and a nasty one to have in production (chunk 3's text silently paired with chunk 7's vector).

It also calls `record_tokens(...)` when usage data is present — feeding the pipeline's cost-tracking observability, since unlike the local model, every OpenAI embedding call has a real dollar cost that the system wants to be able to report on.

#### `_aembed_batch(texts, is_query)`

Overrides the base class's plain "run it on a thread" version with `with_retry(call, attempts=4, operation="openai_embed")` — because a real network call genuinely can transiently fail (rate limits, brief connectivity issues) in ways a local model call cannot, so the async path gets real retry semantics that the sync path (used by scripts and simpler contexts) doesn't need.

---

### `EmbeddingCache` (`sentinel/core/cache.py`)

Not part of the `embedding/` package itself, but every embedder is built with one, so it belongs in this walkthrough.

```python
def _key(self, text: str) -> str:
    return f"{settings.redis_key_prefix_effective}:emb:{self.model}:{hashlib.sha256(text.encode()).hexdigest()}"
```

The cache key is built from the **model name plus a content hash of the (already-prefixed) text** — not the chunk's id, not its position in the document. This single design choice is why re-ingestion is close to free: a chunker change that leaves 95% of chunk text byte-for-byte identical still produces cache hits for that 95%, because the cache doesn't know or care *where* a piece of text came from, only *what* it says and *which model* would embed it. Change the model, and every key changes (the model name is baked into the key), which is exactly what you want — a vector from a different model is a different, incomparable number, so it must never be served in place of one from the currently configured model.

`get_many`/`put_many` batch multiple lookups/writes into a single Redis round trip (`mget`/`mset`) rather than one call per text — the same batching principle as `embed_documents` above, for the same reason: at corpus scale, round-trip latency dominates over the actual work being done.

`embedding_cache_ttl` defaults to `None` — **never expires**. This is a deliberate difference from `RetrievalCache` (15-minute TTL) and `SemanticAnswerCache` (1-hour TTL): those caches can go stale because the *underlying data* they're built from changes (documents get re-ingested, a re-ranking model changes its mind). An embedding vector, by contrast, is a pure function of `(model, exact text)` — the same model will always produce the same vector for the same input, forever, unless the model itself is swapped out (in which case the key changes anyway, since the model name is part of the key). There is nothing for an embedding cache entry to go stale *against*. Every method here also swallows backend errors and treats them as a cache miss rather than letting a Redis hiccup fail an ingestion run — see the module docstring's rule: "a cache failure must never fail the request."

---

## Why these defaults

None of these numbers were fit to a dataset with a grid search; they're either the model publisher's own stated limits, or values chosen for a stated operational reason. Where the reasoning is inferred rather than documented in a comment, that's said explicitly rather than presented as certain.

| constant | value | derivation |
|---|---|---|
| `local_embedding_model` | `BAAI/bge-base-en-v1.5` | The "base" size in the BGE family — a middle point between the small model (faster, a few points less accurate) and the large model (best accuracy, slower and more memory). Good default for a general corpus; swap for `bge-small` on constrained hardware or `bge-large` when quality is the binding constraint and latency isn't. |
| `local max_input_tokens` | `512` (capped) | BGE-base's actual trained sequence length. Capped even if the library reports something larger, because a technical limit and a *meaningfully trained* limit aren't always the same number — see the `LocalEmbedder.__init__` walkthrough above. |
| `local batch_size` | `64` | Balances throughput against local memory: a bigger batch means fewer Python-level calls into the model but a bigger single allocation on CPU/GPU. 64 is a conservative default that runs comfortably on a laptop; raise it on a machine with a real GPU and plenty of VRAM. |
| `openai_embedding_model` | `text-embedding-3-small` | The cheap, still-strong member of the `-3-*` family — the right default for "try OpenAI embeddings" without committing to `-3-large`'s roughly 6x higher price per token before you've measured whether the quality gap matters on your corpus. |
| `openai max_input_tokens` | `8191` | OpenAI's real API-enforced limit for the `-3-*` family. The comment in the code is honest about why this number is rarely hit: chunking already caps chunk size well below this (typically 550–900 tokens), so this ceiling exists as a safety net, not a lever anyone is expected to tune against. |
| `openai batch_size` | `128` | The API comfortably accepts large batches; a bigger batch means fewer HTTP round trips (lower latency overhead) at the cost of a bigger blast radius if one batch call fails (you re-embed up to 128 chunks, not 10,000). 128 is a middle point that favors throughput without making a single failure too expensive to retry. |
| truncation warning schedule | `1, 10, 100, 1000` | Exponentially decaying frequency: you're told immediately the first time it happens (so you notice right away if chunk sizing and the model's limit have drifted apart), and then periodically as it keeps happening, without a bad ingestion run flooding your logs with the same warning thousands of times. |
| `MATRYOSHKA_CAPABLE` dims sampled in the bake-off | `3072, 1024, 512, 256` | Not arbitrary round numbers for their own sake — they sample the practically interesting points along OpenAI's truncation curve: full native size (3072), a ~3x reduction (1024, informally "the sweet spot" in OpenAI's own guidance — most of the quality survives), a ~6x reduction (512), and a ~12x reduction (256, where quality loss becomes clearly visible). Benchmarking exactly these four shows you where the curve is still flat and where it starts to fall off a cliff. |
| `embedding_cache_ttl` | `None` (never expires) | An embedding vector is a deterministic function of `(model, exact text)` — nothing about it can go stale the way a retrieval result or a generated answer can. The only reason to ever evict an entry is storage cost, not correctness, so there's no default TTL. |
| `normalize` (local) | `True` | Without it, cosine/dot-product/L2 distance disagree with each other and longer documents get an unearned similarity boost purely from vector length. Normalizing removes both problems at once — see the `_embed_batch` walkthrough above. |

---

## Common questions

**Q: If I already normalize the embed_text with a heading-path prefix in chunking, why does embedding add its own prefix too?**
They're solving different problems and both are needed. Chunking's heading-path prefix (`"Checkout Configuration > Validation > strict_mode — ..."`) makes a *fragment of text* self-describing regardless of which model embeds it — it's a content-level fix. The embedding model's query/document prefix is a *model-training* artifact — BGE and E5 specifically expect it on the query side because of how they were trained, and it has nothing to do with what the text says. You'd need both even for a chunk with a perfect heading prefix, and you'd need the model prefix even for chunking strategies that don't add a heading path at all (like `sentence_window`).

**Q: What actually happens if I switch `embed_provider` from `local` to `openai` without re-indexing?**
Nothing crashes — that's the dangerous part. The vector store will happily accept new query vectors and compare them against old vectors from a completely different model, in a completely different (and differently-sized) coordinate space. The results will be close to random. `settings.index_version` exists specifically to prevent this: it's a fingerprint built from the chunking config *and* the embedding provider, model, and dimensionality, stamped onto every chunk at ingest time and checked on every query. Change the embedder, and the index version changes, and (assuming the surrounding retrieval code respects it, which it's built to) old chunks simply stop matching new queries rather than matching them wrong.

**Q: Is the local BGE model actually good enough, or is it just the free option?**
Per the (illustrative) bake-off numbers in the overview, local BGE-base lands within about four points of recall@5 compared to the best hosted OpenAI configuration, at zero marginal cost and with no data ever leaving the machine. Whether that four-point gap matters depends entirely on your corpus and your tolerance for it — but for a corpus that can't leave your infrastructure at all (a real constraint for a lot of regulated data), it's not really a trade-off between two options; local is the only option, and knowing the actual quality cost of that constraint (rather than assuming it's large) is the point of running the bake-off yourself.

**Q: What's the actual difference between truncating a vector (Matryoshka) and just using a smaller model?**
A smaller model (like `text-embedding-3-small` instead of `-large`) was trained differently from the ground up — fewer parameters, a different training run, genuinely a different (usually weaker) function from text to meaning. Matryoshka truncation takes the *same* large model's output and just keeps the first N of its numbers, discarding the rest. It works because of how `-3-large`/`-3-small` were specifically trained: the earlier dimensions are trained to carry more of the "important" signal, so keeping a well-chosen prefix of them preserves most of the useful structure. It's less like using a cheaper camera and more like keeping only the most important brushstrokes of a painting — the same painter, a deliberately compressed version of the same painting.

**Q: Why does `embed_query` skip the cache but `embed_documents` uses it?**
Because at ingestion time, the same passage text often does get re-embedded across runs (re-chunking, re-ingesting an unchanged doc), so a document cache pays for itself constantly. A user rarely asks the *exact same question*, word for word, twice — and when they effectively do ("is the checkout service healthy" today versus yesterday), you don't actually want the cached *vector*, you want the cached *retrieval result* or *answer*, both of which are handled by dedicated caches further up the pipeline that also account for role scope and index freshness. A bare query-vector cache here would add complexity for very little payoff.

**Q: Can I mix embedding providers — some chunks embedded locally, some with OpenAI — in the same index?**
Not sensibly, no, even though nothing in the code stops you from trying. Vectors from two different models live in two different, incompatible coordinate spaces — a 768-dimension BGE vector and a 768-dimension (truncated) OpenAI vector are not comparable just because they happen to be the same length. `index_version` guards against the common accidental version of this (switching providers without re-indexing); deliberately mixing them within one index is a case the system doesn't try to protect you from, because there's no legitimate reason to do it.

**Q: Does a longer chunk always produce a "richer" or "better" embedding?**
No — this is a common intuition and it's backwards for retrieval purposes. A longer chunk covering multiple topics produces a vector that's an *average* of those topics, which ends up close to *none* of them in the geometry described above. This is exactly the tension [`docs/stages/chunking.md`](chunking.md) is built around, and it's why `embed_chunks` embeds the small, focused `embed_text` rather than the larger `context_text` — see the `embed_chunks` walkthrough above.

**Q: What happens to a chunk whose text is longer than the model's input limit?**
It gets silently truncated by the model itself if nothing intervenes — which is exactly the failure `_prepare`'s truncation logic exists to make visible instead of invisible. See `_prepare` above and the "what breaks in production" section below.

---

## Result _(illustrative — run `benchmarks/embedding_bakeoff.py`)_

| provider | model | dims | recall@5 | ndcg@5 | ms/1k chunks | usd/1M tokens |
|---|---|---|---|---|---|---|
| openai | text-embedding-3-large | 3072 | 0.87 | 0.81 | 2100 | 0.13 |
| openai | text-embedding-3-large | 1024 | 0.86 | 0.80 | 2000 | 0.13 |
| openai | text-embedding-3-large | 256 | 0.81 | 0.74 | 1900 | 0.13 |
| openai | text-embedding-3-small | 1536 | 0.84 | 0.78 | 1800 | 0.02 |
| local | bge-base-en-v1.5 | 768 | 0.83 | 0.77 | 12000 | 0.00 |

Two things worth reading past the top row. First, truncating 3072 → 1024 costs about a point of recall while saving two-thirds of the index's storage footprint — usually a good trade, which is exactly the kind of thing this benchmark exists to let you decide with a number instead of a guess. Second, the local model sits within about four points of the best hosted configuration at zero marginal cost and zero data egress — for some corpora that isn't a trade at all, it's a requirement, and this repo defaults every embedding call to it for that reason.

Per `CLAUDE.md`'s rule on numbers in documentation: this table is a placeholder shape, not a measured result. Run the benchmark against your own corpus before quoting a number from it.

---

## What breaks in production

- **Changing the embedding model without re-indexing.** The single most common production RAG incident, and it presents as a mysterious, gradual quality regression rather than a clean error — see the FAQ above. `settings.index_version` is the guard; the discipline required is making sure every code path that queries the index actually checks it.
- **Dimension mismatch between embedder and vector store.** `settings.embedding_dims` exists precisely so the vector store schema and the embedder agree by construction rather than by convention — see `sentinel/core/config.py`. Caught at write time if you're lucky (an explicit dimension error), silently wrong retrieval if you're not (some vector store backends will happily zero-pad or reject inconsistently).
- **Re-embedding unchanged text.** Without `EmbeddingCache`, every re-ingestion of a largely-unchanged corpus would re-pay the full embedding cost. Because the cache key is content-derived rather than position-derived, even a chunking change that shuffles most chunk boundaries but leaves the text itself the same still hits the cache for that unchanged text.
- **Silent truncation.** A chunk sized for one model's limits gets silently cut off if pointed at a smaller-limit model — see `_prepare` above. `truncation_count` and the `embedding_truncated` warning are what turn this from invisible into something you'd actually notice; nothing enforces that anyone is watching for it.
- **Treating the query like a document (or vice versa).** The base class makes this hard to do by accident within this codebase's own call paths, but it's exactly the failure mode to check first if recall on a BGE/E5-family model looks inexplicably mediocre — see "Why the base class enforces query/document asymmetry" above and `scripts/embedding/01_embed_local.py`, which measures the cost directly.
- **Embedding `context_text` instead of `embed_text`.** Nothing crashes; you just lose the entire point of two-field chunking. See the `embed_chunks` walkthrough above — this is the single line in the whole stage where that mistake could be made.
- **Assuming OpenAI's embedding response preserves input order.** It doesn't promise to. `OpenAIEmbedder._embed_batch` re-sorts by `.index` before returning; an adapter (or a hand-rolled script calling the API directly) that skips this can silently pair the wrong vector with the wrong chunk.

---

## Files

| path | what |
|---|---|
| `sentinel/embedding/base.py` | `BaseEmbedder` — caching, batching, truncation, the query/document split |
| `sentinel/embedding/local_embedder.py` | sentence-transformers adapter (BGE/E5 family), prefix tables |
| `sentinel/embedding/openai_embedder.py` | plain OpenAI adapter, Matryoshka truncation, rate-limit translation |
| `sentinel/core/cache.py` | `EmbeddingCache` (this stage), plus `RetrievalCache` / `SemanticAnswerCache` (downstream, referenced above for contrast) |
| `sentinel/core/config.py` | `EmbedProvider`, `embedding_dims`, `embedding_model`, `index_version`, credential validation |
| `sentinel/core/registry.py` | `(stage, provider) → class` mapping (`"embedder"` entries) |
| `sentinel/core/types.py` | `Chunk`, `EmbeddedChunk` |
| `scripts/embedding/01_embed_local.py` | demonstrates the query/document prefix margin on real text |
| `scripts/embedding/02_embed_openai.py` | measures what Matryoshka truncation costs at each dimension |
| `benchmarks/embedding_bakeoff.py` | the bake-off — recall, index footprint, throughput, cost per configuration |
| `.archive/providers/embedding/` | retired Azure OpenAI / Bedrock embedding adapters, with a revival README |
