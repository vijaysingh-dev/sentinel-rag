# Chunking

> Stage 2 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#2-chunking); this file is the deep version.
>
> Code: `sentinel/chunking/` (one module per strategy under `chunking/strategies/`, numbered in build order) · Benchmark: `benchmarks/chunking_sweep.py` · Routing: `chunking/strategies.DOC_TYPE_STRATEGY`

---

## What we used

Nine strategies behind one interface (`BaseChunker` in `chunking/base.py`), selected **per document type** by a routing table rather than one global choice.

| doc type | strategy | one-line reason |
|---|---|---|
| runbook | `runbook` | embed each step, always return the whole procedure |
| glossary | `glossary` | one term per chunk; entries are atomic |
| methodology, incident, factsheet, adr, page | `structural` | recursive split inside heading sections, heading path prefixed |

Available but not default, for benchmarking or specific corpora: `fixed`, `recursive`, `sentence_window`, `parent_document`, `semantic`, `contextual`.

`chunker_for_doc(doc, override)` is the entry point: explicit override wins, else `DOC_TYPE_STRATEGY.get(doc.doc_type, "structural")`.

---

## Why chunking is a stage at all

Imagine you had to email someone the one paragraph of a textbook that answers their question, by physically tearing pages out and scanning just that scrap. Tear out too little — a single sentence — and you've handed them something with no idea what it's even about ("Set this to false to skip validation" — *what* setting, *where*?). Tear out too much — three surrounding pages — and the answer is buried in text they have to read anyway, and worse, a computer trying to match this scrap against a search query now has to represent "three pages about five different things" as a single point in space, which resembles nothing in particular. Chunking is the tearing, done automatically, thousands of times, and it turns out you cannot tear once and make both a search engine and a human reader happy with the result — which is the entire reason this stage exists as a hard problem rather than a five-line function.

Two consumers read the same text and want opposite things from it:

- The **retriever** wants small, single-topic units. A vector that averages five topics sits near nothing in embedding space — it is findable by no query and by every query equally.
- The **generator** wants enough surrounding context to answer: the definition, the preceding setup, the table header, the antecedent of "it".

The resolution is the one design decision the whole package is built around: **`Chunk` carries two text fields.**

```
Chunk
├─ embed_text     what gets vectorised — small, specific, prefixed with its heading path
├─ context_text   what is handed to the LLM — larger, self-contained, readable
├─ chunk_id       {doc_id}::{seq:04d}::{sha256(text)[:16]}  — positional + content
├─ parent_id      {doc_id}::{heading_path}  — the dedupe key at retrieval time
├─ heading_path, page, doc_type, tenant_id, source_uri, effective_date
├─ strategy, token_count, content_hash   — stamped by _finalise
└─ meta           strategy-specific: block_type, sentence_index, step_index, threshold, ...
```

`embed_text` and `context_text` are usually identical and deliberately need not be. `sentence_window`, `parent_document` and `contextual` are only *expressible* because of that split — a single-text-field chunk model cannot represent "embed this one sentence, return these seven" at all.

Everything is registered by name (`@register("...")`) so `benchmarks/chunking_sweep.py` enumerates strategies and produces a leaderboard rather than needing a code change per experiment.

---

## The shared machinery (`base.py`)

### `ntokens(text)` — the ruler

Every size decision in this stage is measured in **tokens**, because that is the unit every embedding model and LLM bills and truncates on — not characters, not words.

```python
def ntokens(text: str) -> int:
    encoder = _encoder()
    counter = getattr(encoder, "count", None)
    return counter(text) if counter else len(encoder.encode(text))
```

`_encoder()` is `@lru_cache`'d — built once per process — and loads tiktoken's `cl100k_base`.

**Assumption 1 — one ruler for every provider.** `cl100k_base` is OpenAI's tokenizer. It is *wrong* for Claude and Titan, whose tokenizers segment differently. It is used anyway: a chunking benchmark compares strategies against each other, so it needs a *consistent* ruler far more than an *exact* one. Measuring `structural` with one tokenizer and `recursive` with another would make the `chunks/doc` column of the leaderboard meaningless. Consistency beats accuracy here.

**Assumption 2 — degrade, never crash.** tiktoken downloads its encoding file on first use; in an air-gapped or egress-restricted environment that download fails. Rather than crash the pipeline over a tokenizer, `_ApproximateEncoder` takes over at `CHARS_PER_TOKEN = 4` — the standard rule of thumb for English technical prose (a token averages ~4 characters). A warning is logged **once**: `tokenizer_fallback`, `impact="chunk sizes are approximate and not comparable across runs"`. The honest cost: numbers from a fallback run cannot be compared with numbers from a real-encoder run.

The fallback's `encode` returns UTF-8 byte values as stand-in token ids (`errors="replace"`); `decode` is `bytes(...).decode(errors="ignore")`. Callers only need two things from an encoder — a **count**, and the ability to **round-trip a prefix** of the ids back to text — and bytes satisfy both. A prefix cut mid-multibyte-character produces slightly mangled text at the boundary rather than an exception.

### `truncate_tokens(text, limit)` — the safety net

```python
def truncate_tokens(text: str, limit: int) -> str:
    encoder = _encoder()
    if ntokens(text) <= limit:
        return text
    ids = encoder.encode(text)
    budget = limit * 4 if isinstance(encoder, _ApproximateEncoder) else limit
    return encoder.decode(ids[:budget])
```

- Early return if already under budget — no allocation, exact text preserved.
- Otherwise encode, keep the first `limit` ids, decode.
- **The `budget` line is the fallback correction.** With the real encoder, `ids` are tokens, so `ids[:limit]` is `limit` tokens. With the approximate encoder, `ids` are *bytes*, and `limit` tokens ≈ `limit × 4` bytes — so the slice is scaled by `CHARS_PER_TOKEN` to keep the same effective budget.

**Why it exists:** every embedding model *silently* truncates input past its max sequence length. A 2000-token chunk sent to a 512-token model loses three quarters of its content with no error anywhere in the stack. Truncating deliberately — and stamping `token_count` in `_finalise` — makes it visible: a rising truncation count downstream means chunks are sized wrong for the model.

### `_finalise(doc, chunks)` — the metadata gate

Every `split()` ends with `return self._finalise(doc, chunks)`. It:

1. **Drops chunks whose `embed_text` is blank** after stripping — the one universal filter.
2. **Stamps document-level fields onto every chunk**: `doc_id`, `tenant_id`, `source_uri`, `effective_date`, `strategy`, `token_count`, `content_hash` (`sha256(embed_text)[:16]`).
3. `doc_type` and `effective_date` use `chunk.X or doc.X`: a strategy *may* pin a value (`runbook` pins `doc_type="runbook"`, `glossary` pins `"glossary"`), otherwise the chunk inherits the document's. This is why `Chunk.doc_type` defaults to `""`, not `"page"` — the empty string lets `_finalise` tell "not set, inherit" apart from "deliberately generic".

**Why it is centralised:** a strategy that forgets `tenant_id` produces chunks that are invisible to a filtered (ACL-scoped) search, and the symptom looks like *poor recall*, not a missing field. Centralising the stamp makes that class of bug unwritable.

### Two ids, two purposes

| id | formula | purpose |
|---|---|---|
| `chunk_id` (`Chunk.make_id`) | `{doc_id}::{seq:04d}::{sha256(text)[:16]}` | stable identity. Position alone would renumber every later chunk when a paragraph is inserted mid-document, forcing a full re-embed; content alone would collide on two identical boilerplate paragraphs. The pair is stable under edits elsewhere, so re-ingesting an unchanged document is a no-op. |
| `content_hash` (`_finalise`) | `sha256(embed_text)[:16]` | dedupe key for near-identical chunks across documents. |

---

## The regexes

### Sentence splitting

```python
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
```

| fragment | role |
|---|---|
| `(?<=[.!?])` | lookbehind (zero-width): the char before the split must be `.`, `!` or `?`. The punctuation stays attached to the left sentence. |
| `\s+` | the only thing consumed — the whitespace gap between sentences. |
| `(?=[A-Z(])` | lookahead (zero-width): the next sentence must start with a capital or `(`. |

**What the pattern assumes a sentence boundary is:** terminal punctuation + space + capital. This deliberately rejects `"e.g. foo"` (lowercase follows) and `"3.14 is pi"` (digit follows) — the two commonest false positives. It still mis-splits `"Dr. Smith"`. The docstring is explicit: "occasionally split wrongly; if that shows up as a measurable recall problem, swap in pysbd or spaCy — but measure first, because the failure is usually harmless." A slightly mis-split sentence window still contains the fact.

### Runbook step splitting

```python
STEP_SPLIT = re.compile(r"\n(?=\s*(?:\d+[.)]|Step\s+\d+|[-*]\s))", re.IGNORECASE)
```

Splits **at a newline** (consumed) that is **followed by** (lookahead, not consumed) a step marker:

| alternative | matches |
|---|---|
| `\s*\d+[.)]` | optional indent, digits, then `.` or `)` → `1.`, `2)`, `  10.` |
| `Step\s+\d+` | `Step 3`, `step  12` (case-insensitive) |
| `[-*]\s` | a bullet: `- ` or `* ` |

**Assumption:** a runbook's steps are an ordered or bulleted list, each item starting its own line. The lookahead keeps the marker attached to its step. A runbook written as prose paragraphs yields `< 2` matches and the strategy falls back (see [`runbook`](#runbook--embed-steps-always-return-the-whole-procedure)).

### Glossary term extraction

```python
TERM = re.compile(
    r"^(?:#{2,4}\s+|\*\*)(?P<term>[^\n*#]{2,80})(?:\*\*)?\s*\n(?P<body>.*?)"
    r"(?=\n#{2,4}\s|\n\*\*[^\n*]{2,80}\*\*|\Z)",
    re.S | re.M,
)
```

Flags: `re.S` (DOTALL — `.` matches newlines, so `body` can span paragraphs) and `re.M` (MULTILINE — `^` matches at every line start).

| part | role |
|---|---|
| `^` | start of a line |
| `(?:#{2,4}\s+\|\*\*)` | the term is introduced *either* by a level 2–4 ATX heading (`##`–`####` + space) *or* by bold (`**`) — the two ways glossaries are commonly written |
| `(?P<term>[^\n*#]{2,80})` | the term: 2–80 chars, no newline / `*` / `#`. The 80-char cap is a sanity limit — a longer "term" is a misparse |
| `(?:\*\*)?` | optional closing `**` (present in the bold form, absent in the heading form) |
| `\s*\n` | end of the term's line |
| `(?P<body>.*?)` | the definition — **non-greedy**, so it stops as early as the lookahead permits |
| `(?=\n#{2,4}\s \| \n\*\*…\*\* \| \Z)` | stop when the next line is another heading, another bold term, or end of string. This is what bounds one entry without swallowing the next entry's header |

**Assumption:** every entry is a heading or a bold lead-in, and its definition runs until the next one. No matches → not a glossary in this format → fall back to `structural`.

---

## The nine strategies, in build order

They are written to be read top-to-bottom. Each answers a shortcoming of the one before it, and the progression *is* the argument for why the later ones exist.

### `fixed` — the control group

Encode the whole document to token ids, slide a window of `size=550` forward by `step = size - overlap = 490`. `embed_text == context_text` (no decoupling). Ignores every structural signal in the document — which is the point.

- **Why it exists:** the baseline. Any strategy that cannot beat `fixed` on your corpus is not earning its complexity.
- **Why overlap:** a fact sitting on a window boundary would be split across two chunks and coherent in neither. `overlap=60` (~11%) fixes that at the cost of ~11% more vectors and near-duplicate results crowding the top-k.
- **The `550 / 60` defaults** recur across the file: roughly a healthy paragraph, comfortably inside every embedding model's sequence limit, small enough to stay near-single-topic.

### `recursive` — respect boundaries when you can

```python
SEPARATORS = ["\n\n", "\n", ". ", "; ", ", ", " "]
```

Ordered **strongest semantic boundary first**: paragraph break, line break, sentence, clause, phrase, word. The pragmatic default of most RAG systems — it respects paragraph and sentence boundaries when it can and only falls back to an arbitrary token cut for text that has no boundaries at all.

#### `_recursive_split(text, separators)`

The core algorithm. Five of the nine strategies call it directly.

```
if ntokens(text) <= size:          → return [text]                     # base case: it fits
if no separators left:             → hard-cut every `size` token ids   # last resort
separator = separators[0]
parts = text.split(separator)
buffer = ""
for part in parts:
    candidate = buffer + separator + part
    if ntokens(candidate) <= size:  → buffer = candidate; continue      # keep packing
    if buffer:                      → emit buffer                       # flush
    if ntokens(part) <= size:       → buffer = part                     # start fresh
    else:                           → out.extend(_recursive_split(part, weaker_separators))
```

The shape: **greedily pack `parts` back together up to `size`; whenever a single `part` is itself over budget, drop to the next-weaker separator and split *that part* recursively.** So an unbroken wall of text is tried on `\n`, then `. `, then `; `, ... and only a paragraph that is one enormous string with no punctuation at all reaches the "no separators left" hard cut. Nothing is ever dropped — the `else` branch guarantees an oversized part is subdivided, not discarded.

#### `_apply_overlap(pieces)`

Recursive splitting produces *disjoint* pieces (unlike `fixed`'s sliding window), so overlap is added afterward: prepend the **last `overlap` tokens of piece `i-1`** to piece `i`.

```python
tail = truncate_tokens(pieces[i - 1][-2000:], self.overlap)
out.append(f"{tail} {pieces[i]}")
```

The `[-2000:]` is a **character** slice — a cheap pre-trim so `truncate_tokens` is not tokenising a 5000-word piece just to keep its last 60 tokens. 2000 chars is safely more than 60 tokens (~240 chars) with margin. Skipped entirely when `overlap <= 0` or there is only one piece.

### `structural` — the recommended default

Split along the document's **own heading hierarchy**, run `recursive` *inside* each section, and **prefix the full heading path onto every chunk's `embed_text`**.

#### `_sections(doc)`

Walks `doc.blocks` in reading order, maintaining `stack: dict[level → heading text]`:

- On a `HEADING` block: flush the accumulated non-heading blocks as a `(path, blocks)` section, then
  `stack = {k: v for k, v in stack.items() if k < level}` — **drop every heading at this level or deeper** (a fresh `##` closes the previous `##` and any `###` beneath it) — then `stack[level] = block.text`. `current_path = " > ".join(stack[k] for k in sorted(stack))` rebuilds the breadcrumb, e.g. `"Configuration > Validation"`.
- Any other block: append to the current section.
- `level = block.level or 2` — a heading whose depth the parser did not report is assumed to be `##`.
- `return sections or [("", doc.blocks)]` — a document with no headings still yields one section covering everything.

#### `split()`

- `header = f"{doc.title} > {heading_path}"` (or just `doc.title` at root) — **the title is always in the prefix.**
- `parent_id = f"{doc.doc_id}::{heading_path or 'root'}"` — one parent per section, for retrieval-time dedupe.
- **Tables** (`BlockType.TABLE`, `keep_tables_whole=True`, the default): emitted as a single chunk, never fed to `_recursive_split`. `embed_text = "{header}\nTable: {caption}\n{body}"`, truncated to `size * 2` (**1100 tokens — tables get double budget** because a table split across a boundary loses its header and becomes orphaned numbers). `context_text = "[{header}]\n{body}"`, `meta={"block_type": "table"}`.
- **Everything else**: `self.inner._recursive_split(block.text, SEPARATORS)` per block; each piece becomes `embed_text = f"{header}\n\n{piece}"`, `context_text = f"[{header}]\n{piece}"`, `meta={"block_type": block.type.value}`.

**Why the heading prefix is the entire argument for this being the default:**

> A chunk reading *"Set this to false to skip validation"* is unretrievable — no realistic query lands near it in embedding space.
>
> The same chunk embedded as *"Checkout Configuration > Validation > strict_mode — Set this to false to skip validation"* is findable by dozens of queries.

The prefix costs a handful of tokens and typically buys more recall than upgrading the embedding model — because it turns unretrievable fragments into findable ones.

### `sentence_window` — the purest decoupling

Per section: join the block text, `split_sentences`, then for each sentence `i`:

- `embed_text = f"{header}\n{sentence}"` — **one sentence** vectorised. Maximum precision: one claim per embedding.
- `context_text` = sentences `[max(0, i-window) : i+window+1]` joined — with `window=3`, **up to 7 sentences** returned to the generator.

**Costs:** 3–10× more vectors than paragraph chunking; adjacent sentences produce heavily overlapping windows, so the retriever **must** deduplicate before assembling a prompt or the context budget fills with repeated text. **Best on** dense factual documents — methodology, policy, specifications — where a single sentence is a complete fact.

### `parent_document` — embed children, return the parent section

- `child = RecursiveChunker(child_size=250, overlap=0)` — small children, **no overlap** (children are throwaway retrieval keys; overlap would only bloat the index).
- Per section: `context` = the whole section text, truncated to `parent_size=1800`, computed **once**. Every child of that section carries the *identical* `context_text`.

**The gotcha, and the reason it is in "what breaks":** you **must** deduplicate by `parent_id` at retrieval time. Five matching children of one parent each pull in the identical 1800-token parent text and consume the entire context budget on one section. `retrieval/fusion.dedupe_by_parent` handles it; a pipeline that forgets is quietly wasting most of its prompt.

### `runbook` — embed steps, always return the whole procedure

- `full = truncate_tokens(doc.markdown, max_context_tokens=3000)` — the complete procedure. This is the `context_text` for **every** chunk.
- `steps = STEP_SPLIT.split(doc.markdown)` — numbered / bulleted items become the retrieval units.
- **Fallback 1** — `< 2` steps found: the runbook is prose, so each **heading section** (`_sections`) becomes a "step". A long procedure is then retrievable by section rather than only by its title.
- **Fallback 2** — still nothing: `[doc.markdown[:2000]]`, one chunk, so the document does not vanish from the index.
- `doc_type="runbook"` pinned; `meta={"step_index": i, "is_parent_doc": True}`.

**Why this is a whole strategy:** a retrieved half-runbook is *worse* than no runbook, because someone under incident pressure will execute the half. This is the clearest case for why chunking strategy is chosen per document type rather than globally.

### `glossary` — one term per chunk

`TERM.finditer` over the markdown (regex above). No matches → fall back to `structural`.

Each entry: `embed_text = f"Glossary term: {term}\n{body}"`, `context_text = f"**{term}** — {body}"`, `heading_path = term`, `doc_type="glossary"` pinned. Entries with an empty body are skipped.

**Assumption:** glossary entries are atomic. Merging "burn rate" and "error budget" into one 500-token chunk makes *both* harder to find, because the vector now represents an average of two unrelated concepts.

### `semantic` — cut where meaning shifts, not where the token count runs out

The one strategy that embeds at chunk-decision time.

1. `split_sentences`; if `< 5` sentences → fall back to `structural`.
2. `windows` — each sentence joined with its `±buffer=1` neighbours, so one short sentence ("Yes.") does not register as a topic shift on its own.
3. Embed all windows via `get_provider("embedder")`. **L2-normalise** the vectors, with `np.where(norms == 0, 1, norms)` guarding a zero vector.
4. `distances = 1.0 - sum(v[:-1] * v[1:], axis=1)` — cosine *distance* between each consecutive pair (dot product of unit vectors is cosine similarity; `1 −` it is distance).
5. `threshold = percentile(distances, 92)` — **the largest 8% of jumps become breakpoints.** A percentile is adaptive: a uniform document produces few splits, a topic-hopping one produces many. `92` is the tuning knob (surfaced in the name as `semantic_p92`).
6. Walk the sentence ranges between breakpoints; any run still over `max_size=900` is handed to `_recursive_split`.

**The honest assessment, verbatim from the docstring:** it is **expensive** — you embed the whole corpus once at ingest just to decide boundaries, then embed the chunks again — and in published comparisons it frequently fails to beat recursive splitting by enough to justify that. Benchmark it on your corpus before adopting it; the interesting result is usually the cost column, not the recall column.

### `contextual` — not registered; wraps any other strategy

Anthropic's contextual-retrieval technique. `split()` (synchronous) returns the inner strategy's chunks un-enriched; **`asplit()`** does the real work: for each chunk a cheap LLM writes one sentence situating it within the document, prepended to `embed_text`. It recovers most of what splitting destroys — pronouns, implicit subjects, "as described above".

```python
PROMPT = (
    "<document>\n{doc}\n</document>\n\n"
    "Here is a chunk from that document:\n<chunk>\n{chunk}\n</chunk>\n\n"
    "Give one short sentence situating this chunk within the document ..."
)
```

- **The document goes first, deliberately.** That makes it the static prefix of the prompt, so **prompt caching** charges roughly a tenth for those tokens after the first chunk. Without caching, one LLM call per chunk across a whole document is impractical at corpus scale.
- `asyncio.Semaphore(max_concurrent=8)` bounds concurrency; `doc_char_limit=40_000` caps the prefix; `chunk.embed_text[:2000]` caps the chunk in the prompt.
- Per-chunk failure is swallowed (`except Exception: pass`) — a chunk that failed to contextualise is still usable, just less retrievable, and must not fail ingestion.
- Not in the registry (it costs one LLM call per chunk). Enable via `settings.contextual_chunking` and wrap the inner strategy explicitly.

---

## The routing table

```python
DOC_TYPE_STRATEGY = {
    "runbook": "runbook", "glossary": "glossary",
    "methodology": "structural", "incident": "structural",
    "factsheet": "structural", "adr": "structural", "page": "structural",
}
```

`chunker_for_doc(doc, override)` → explicit `override` wins; else `DOC_TYPE_STRATEGY.get(doc.doc_type, "structural")`.

**This mapping is the practical answer to "how do you chunk?"** — not one strategy, but a per-content-type decision, each row justified by `benchmarks/chunking_sweep.py`. The cost (from [`../design-decisions.md`](../design-decisions.md)): a routing table to maintain, and per-type benchmarking rather than a single number.

---

## Common questions

**`embed_text` and `context_text` are usually identical — why carry two fields instead of one that's reused twice?** Because the moment a strategy needs them to diverge — `sentence_window` embedding one sentence but returning seven, `parent_document` embedding a small child but returning its whole parent section — a single-field model literally cannot represent that. It would need a second, differently-shaped chunk type just for those strategies. Carrying two fields always, even when `fixed` and `recursive` leave them equal, means every strategy shares one `Chunk` shape, so nothing downstream (retrieval, generation, dedup) ever has to special-case "does this chunk type have one field or two."

**Why is `structural` the default instead of `semantic`, when "semantic" sounds like the smarter choice — doesn't it *understand* the text?** It's a cost-and-payoff question, not an intelligence one. `semantic` embeds the whole document once just to decide where the boundaries should go, then embeds the resulting chunks again for real — double the embedding cost at ingest — and per its own docstring, "in published comparisons it frequently fails to beat recursive splitting by enough to justify that." `structural` gets most of the payoff semantic promises (topic-coherent chunks) for free, by using boundaries a human author already placed — headings — and adds the heading-path prefix on top, which is a bigger, cheaper recall lever than smarter boundary detection.

**What happens if `structural` chunking runs on a document with no headings at all?** `_sections(doc)` falls back to `sections or [("", doc.blocks)]` — one section covering the entire document. The `header` becomes just `doc.title` (no `" > "` path to append), and everything else proceeds through `_recursive_split` exactly as normal. Nothing breaks; the document doesn't vanish or error out. You simply lose the extra recall the heading-path prefix buys — functionally, you get `recursive` chunking with the document's title stapled onto every chunk.

**Why does the tokenizer choice (`cl100k_base`) matter if the embedding model isn't from OpenAI at all?** Because `ntokens`'s job here is *consistency for comparison*, not *exactness for billing*. The chunking benchmark needs every strategy measured against the same ruler so the `chunks/doc` column in the leaderboard means something — measuring `structural` with one tokenizer and `semantic` with another would make that column garbage. `cl100k_base` genuinely is the right tokenizer when a chunk is actually sent to an OpenAI embedding model near its limit; for BGE or Claude it's a stand-in, not the model's real tokenizer — a deliberate, documented approximation, not an oversight.

**Why does `parent_document` need explicit deduplication at retrieval time instead of chunking just never returning duplicate parents?** Because deduplication is fundamentally a retrieval-time concern — chunking has no idea what else will end up in the same result set. Only once retrieval sees its top-k results together does it know it just pulled five different children of the identical parent section. That's why `retrieval/fusion.dedupe_by_parent` exists where it does, not inside the chunker.

**Why does `recursive` add overlap *after* splitting (`_apply_overlap`), while `fixed` bakes it into the sliding window as it goes?** `fixed`'s window walks arbitrary token positions, so overlap is just a smaller stride — there's always a well-defined "next window start" to offset backward. `recursive`'s pieces are the *disjoint* result of respecting real boundaries (a paragraph break, a sentence end); there's no stride to shrink, because the cut points aren't evenly spaced. Overlap has to be stitched on afterward, by copying the tail of piece `i-1` onto the front of piece `i`.

**`runbook` has two separate fallback layers before it gives up on step-splitting — why two, not one?** Because the two failure modes are genuinely different problems. Finding `< 2` steps means the runbook is written as prose rather than a numbered/bulleted list, so falling back to heading sections still keeps the document retrievable section by section. Finding nothing at all (no headings either) means the document has almost no exploitable structure, so the final fallback is a flat slice of the first 2000 characters — worse than either real option, but it keeps the document from silently vanishing out of the index.

**`contextual` chunking clearly helps retrieval — why isn't it in the registry, or on by default?** Because it costs one LLM call per chunk at ingest time, and is only affordable at all via prompt caching (see the document-first prompt design above). "Does this help" and "can this corpus afford it at its size" are two different questions, and a global default can't answer the second one for every user — so it's opt-in via `settings.contextual_chunking`, wrapping whichever inner strategy you'd otherwise use.

---

## Result _(illustrative — run `make bench-chunking`)_

| strategy | recall@5 | recall@50 | mrr | chunks/doc |
|---|---|---|---|---|
| structural_550_60 | 0.84 | 0.96 | 0.71 | 18 |
| parent_1800_250 | 0.82 | 0.95 | 0.69 | 41 |
| recursive_550_60 | 0.76 | 0.93 | 0.62 | 17 |
| sentence_window_3 | 0.79 | 0.97 | 0.65 | 96 |
| semantic_p92 | 0.77 | 0.94 | 0.63 | 21 |
| fixed_550_60 | 0.68 | 0.89 | 0.54 | 16 |

Read the `chunks/doc` column *alongside* recall. More chunks is not better: it inflates index size, raises embedding cost, and crowds the top-k with near-duplicates. `sentence_window` buys the best recall@50 for five times the vectors — worth it only if you are recall-starved.

Per `CLAUDE.md`: any change that affects retrieval quality is backed by `make eval` before and after, with the bootstrap confidence interval reported. On a ~40-example golden set a two-point move is usually noise.

---

## What breaks in production

- **Tables split across boundaries** lose their header and become orphaned numbers. `structural` emits each table as one chunk with double the token budget.
- **Code fences split in half** retrieve on their identifiers and actively mislead anyone acting on them. The markdown parser captures fences as single blocks so a chunker never sees a half fence.
- **`parent_document` without deduplication.** Five matching children of one parent each pull in the identical parent text and consume the whole context budget on one section. `retrieval/fusion.dedupe_by_parent` handles it; a pipeline that forgets is quietly wasting most of its prompt.
- **Silent truncation at embed time.** A 2000-token chunk sent to a 512-token model loses three quarters of its content with nothing in the logs. `truncate_tokens` cuts deliberately and `_finalise` stamps `token_count`; a rising truncation count downstream means chunks are sized wrong for the model.
- **A strategy that forgets to stamp `tenant_id`** produces chunks invisible to a filtered search — the symptom reads as poor recall, not a missing field. `_finalise` is the single place that stamp happens, so no strategy can omit it.
- **The tokenizer fallback firing unnoticed.** If tiktoken's encoding file cannot be fetched, chunk sizes become approximate and are not comparable across runs. It logs `tokenizer_fallback` once — grep for it before trusting a benchmark.
- **`split_sentences` mis-splitting on abbreviations** (`"Dr. Smith"`, `"Fig. 3"`). Usually harmless — the fact still lands in the window. If it shows up as measurable recall loss, swap in pysbd or spaCy at `split_sentences`, but measure first.
