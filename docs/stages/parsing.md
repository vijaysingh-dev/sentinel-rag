# Parsing

> Stage 1 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#1-parsing); this file is the deep version.
>
> Code: `sentinel/parsing/` · Scripts: `scripts/parsing/` · Benchmark: `benchmarks/parser_bakeoff.py` · Offline eval: `sentinel/evaluation/parsing_eval.py`

---

## What we used

A three-provider adapter behind one interface (`BaseParser` in `parsing/base.py`), with a hybrid routing policy on top (`parsing/hybrid.py`).

| Provider | Library / service | Handles | Cost |
|---|---|---|---|
| local | pdfplumber, pypdf, python-docx, python-pptx, openpyxl, BeautifulSoup | native PDF, DOCX, PPTX, XLSX, CSV, MD, HTML, TXT, JSON | free |
| azure | AI Document Intelligence, `prebuilt-layout` | scanned PDF, complex/multi-column layout, borderless tables | ~$0.010/page |
| aws | Textract, `TABLES` + `LAYOUT` features | scanned PDF, images, ruled tables | ~$0.015/page |

Routing is decided per document by `hybrid.py`, using the reference-free quality score from `quality.py`. Walk one document through all three with `scripts/parsing/` — the scripts write a full trace to `outputs/parsing/<stem>/` (see [Tracing a parse](#tracing-a-parse)).

---

## Why parsing is stage one and caps everything downstream

Think of a document the way you'd think of a house: it has a shape — rooms, hallways, which door leads where — and that shape is what makes it navigable. A photocopier captures what a page *looks like*, pixel for pixel, but it does not capture that shape; it has no idea a certain box of text is "the kitchen" (a table) versus "the hallway" (a paragraph) versus "the front door" (a heading). Parsing is the step that looks at the photocopy and rebuilds a floor plan a computer can actually walk through — which blocks are headings, which are tables with real rows and columns, what order a reader's eye moves through them in. Every stage built after this one (chunking, search, the answer a person eventually reads) works from that floor plan and never looks at the original page again. Get the floor plan wrong here and nothing downstream can notice, let alone fix it — there's no do-over once the "kitchen" has been mislabeled as "hallway."

No chunking strategy, embedding model or reranker recovers a table that was flattened into a column of orphaned numbers, or body text interleaved with a running header on every page. Before tuning anything else in a RAG pipeline, print the raw extracted text for five documents and read it — which is exactly what the trace tooling below automates.

The economics force the hybrid design. On a mixed corpus, most documents are handled perfectly by a free library and a minority are handled badly. Paying per page for all of them wastes money; using only the local parser silently loses the minority. So: parse locally, score the result, escalate only what fails.

### The four strategies

| strategy | behaviour | when |
|---|---|---|
| `hybrid_escalate` (default) | local first, cloud only when the score is below `parse_quality_threshold` (0.72). Costs one wasted local parse (milliseconds) per document that escalates. | general corpus of mostly-native PDFs |
| `hybrid_merge` | parse with both, then take prose from whichever scored higher on text quality, tables from whichever produced sounder grids, union the figures. Always pays cloud cost. | small high-value corpus (methodology docs queried for years) |
| `race` | run both concurrently, take whichever finishes first with an acceptable score. Optimises latency, not cost — the cloud call still bills even when cancelled. | interactive upload path where a user is waiting |
| `single` | one parser, no fallback. | the baseline you benchmark the others against |

### Format routing, before any parser runs

`detect_format` (`base.py`) maps the extension, then for PDFs **probes the text layer**: it extracts text from the first 5 pages and, if that averages `≥ 100` chars/page, calls it `PDF_NATIVE`; below that, `PDF_SCANNED`. This is the only format where the same suffix covers two operationally different things — a native PDF a free library handles for free, and an image that needs cloud OCR.

`hybrid.parse()` then routes on the format:

- `LOCAL_ONLY_FORMATS` (md, txt, json, csv, html, xlsx) → local always. The structure is already in the file; a layout model has nothing to recover and would only cost money and latency.
- `CLOUD_PREFERRED_FORMATS` (scanned PDF, image) → cloud first. Running pdfplumber on a scan is a guaranteed miss, so there is no point scoring it first.
- everything else → the configured strategy above.

---

## The output contract

Every parser adapter returns a `ParsedDocument` (`core/types.py`), regardless of provider or file format. That uniformity is what makes the hybrid strategies and the cross-provider bake-off possible — you cannot merge or compare two parsers whose outputs have different shapes.

```
ParsedDocument
├─ doc_id            sha256(source_uri)[:16] — path-derived, so an edited document keeps its
│                    identity and its chunks can be superseded rather than appearing as new
├─ source_uri, title, doc_type, tenant_id, effective_date
├─ blocks: list[Block]              ← structure preserved, in reading order
│    ├─ type   heading | paragraph | table | figure | list | code | key_value | header | footer
│    ├─ text, page, level, confidence, bbox
│    ├─ table  Table(rows, cols, cells[][], markdown, caption, confidence)
│    └─ figure Figure(page, bbox, image_uri, caption, description)
├─ metrics: ParseMetrics(provider, duration_s, pages, characters, tables, figures,
│                        usd_cost, quality_score, warnings)
└─ meta: {format, model / features, parse_route, page_range, quality_score, ...}

.markdown   flatten blocks → markdown (what the structural chunker consumes)
.text       plain concatenation (what quality scoring reads)
```

Two modelling decisions carry weight:

- **`Block` keeps the document's structure** rather than flattening to a string at parse time. Chunkers need headings, table boundaries and figure positions to avoid splitting a table from its header or a procedure from its title, and that is impossible to recover once discarded.
- **`BlockType` is deliberately coarse.** Finer taxonomies (footnote, caption, list-item) look appealing but every parser reports them differently; a coarse shared vocabulary is the only one pypdf, Document Intelligence and Textract can all produce faithfully.

---

## Per-provider mechanics

### local (`local_parser.py`)

- **pdfplumber** for PDFs (not pypdf alone) because it exposes word positions and ruling lines, which is what makes table extraction and column detection possible. pypdf is used only for the cheap text-layer probe in `base.py`.
- Tables are extracted **first**, their bounding boxes recorded, and the text pass then excludes any word whose midpoint falls inside a captured table — otherwise every table's numbers appear twice, once as a grid and once as loose paragraph noise.
- Headings in formats without markup (PDF, plain text) are detected by cautious heuristics in `_blocks_from_text`: short line, no terminal punctuation, and either title-case (≤12 words) or a numbered-section pattern. Intentionally conservative — misclassifying a paragraph as a heading corrupts the heading path every chunk inherits, which is worse than missing a heading.
- **python-docx / python-pptx** read OOXML directly; body elements are walked in document order so tables stay interleaved with the prose that introduces them. PPTX keeps speaker notes — in design decks the rationale is in the notes while the slide is a diagram with three words on it.
- **openpyxl** via pandas: each sheet becomes its own table block, large sheets truncated with an explicit note rather than silently.

### azure (`azure_parser.py`)

- `output_content_format=MARKDOWN` on `prebuilt-layout` is the whole reason to prefer this service for RAG: headings arrive as `##`, tables as grids, section hierarchy preserved. The structural chunker consumes that directly.
- Prose is read from the markdown `content`; **tables and figures are read from the structured response** instead, because their markdown rendering loses cell boundaries and figure positions. `_table_spans` tracks where tables sit in the markdown so their text is not also emitted as loose paragraphs.
- Auth prefers `DefaultAzureCredential` (managed identity — no secret in config) over an API key; the key path remains for local development.
- Model choice drives cost by ~7×: `prebuilt-read` (OCR only, no structure) ≪ `prebuilt-layout` (the RAG one) ≪ `prebuilt-document` (layout + key-value).

### aws (`aws_parser.py`)

- **Sync vs async is a hard API split.** `analyze_document` takes inline bytes but handles one page. Anything multi-page goes through `start_document_analysis`, which reads from **S3** and returns a JobId you poll, with `NextToken` pagination over the results. An S3 bucket is a hard requirement — `TEXTRACT_S3_BUCKET`, enforced by config validation (see below).
- **The response is a flat block graph, not a tree.** Textract returns every LINE, WORD, CELL and LAYOUT element as a node with `Relationships` pointing at children by Id (3611 nodes for the 11-page Brezza brochure). Reconstructing reading order and table grids is the adapter's job — most of the file is that assembly.
- Reconstruction **prefers LAYOUT elements** when present (they encode reading order and heading roles: `LAYOUT_TITLE`, `LAYOUT_SECTION_HEADER`), and falls back to grouping LINE blocks into paragraphs by vertical gap when LAYOUT was not requested or returned nothing. The trace records which path was taken.
- Feature selection drives both cost and capability: no features = OCR only (cheapest); `TABLES` = cell coordinates; `FORMS` = key-value pairs; `LAYOUT` = the one that makes it usable for RAG.

---

## Why the quality score is heuristic, and where its numbers come from

At ingest time there is no ground truth to compare against, so the routing gate must be reference-free. `quality.py` scores five components and combines them:

```
score = 0.25·text_density + 0.30·word_validity + 0.20·structure + 0.15·encoding + 0.10·table
```

The weights are ordered by **how catastrophic the failure is**. Density and word-validity (0.55 together) catch the parses that make a document *useless* — a scan returned blank, encoding damage returned as mojibake. Structure and tables (0.30) catch parses that are merely *degraded* — every character correct but the shape lost, so retrieval limps rather than dies. Encoding (0.15) is a narrow, high-signal check for one specific failure mode.

**None of the constants are fitted to a dataset.** They are calibrated priors derived from the physics of a page, then biased conservative. A false "good" puts silent garbage in the index forever; a false "bad" costs one cloud call — so the constants and the 0.72 threshold are set to make the second error the common one. Retune them with `parsing_eval.py` against hand-labelled truth, not by intuition.

| constant | value | derivation |
|---|---|---|
| full page of prose | `1500` chars | ~500 words/page × ~6 chars/word ≈ 3000 for dense prose (a novel); technical docs are ~half that after headings, whitespace, figures. `min(chars_per_page / 1500, 1)` so a normal page scores maximum and the gradient below it penalises decks and scans. |
| native-vs-scanned probe | `100` chars/page | a text-bearing page yields hundreds–thousands of chars; a scanned page yields only stray artefacts (a watermark, a stamp) — low double digits. 100 sits in the empty valley between the two populations, so the exact value barely matters. |
| density warning bands | `< 50` / `< 300` | 50 is below the scanned-page noise floor → image-only. 300 is ~1/5 of a page → a slide deck or a half-scanned document. Informational; they do not move the score. |
| common-word floor | `0.15` | `COMMON_WORDS` is 21 function words that appear in any English prose. Fewer than 15% (≈3 of 21) present in a page of "prose" means it is garbled, non-English, or column-interleaved. |
| fused-word detector | `25` lowercase chars | the longest ordinary English words run ~20 chars; 25+ with no break is almost always two words fused by a lost space (`calculationengine`), a classic pdfplumber artefact. A capped *penalty*, not a hard fail, because chemical names and URLs trip it. |
| giant-paragraph warning | `4000` chars | ~1.3 pages unbroken in one block; past that, layout segmentation has almost certainly failed and the chunker has no seams. |
| healthy heading ratio | `5–25%` of blocks | below ~1 heading per 20 blocks the document is under-segmented; above 25% every other block is a heading, meaning the heuristic is misfiring and promoting short lines. The score peaks in the band and falls off both sides. |
| absent-tables score | `0.75` (neutral) | a document with no tables is not faulty; a neutral score keeps `table_score` from dragging the total up or down when there is nothing to judge. |

### The offline counterpart

`evaluation/parsing_eval.py` *does* compare against hand-labelled truth and is what chooses providers. Three dimensions, because parsers fail in three ways:

- **text fidelity** — normalised edit similarity. Did the characters survive?
- **table accuracy** — cell-level F1, a simplified TEDS. Did the grid survive?
- **reading order** — Kendall's tau on block positions. Did the sequence survive? This is the one that silently destroys multi-column PDFs: every character is correct and the text is unreadable because two columns were interleaved line by line.

`quality.py` is the *online* gate that routes individual documents; `parsing_eval.py` is the *offline* measurement that picks which providers to offer. Don't confuse them.

---

## The failure mode the score does not catch

The `suzuki-brezza.pdf` walkthrough (`scripts/parsing/`, traces in `outputs/suzuki-brezza/`) makes it concrete. A car brochure: eight marketing spreads with stylised ALL-CAPS display text, then one dense spec-sheet page with four tables.

| provider | quality | headings | tables | pages | usd |
|---|---|---|---|---|---|
| local | **0.87** | 0 | 3 (mangled) | 11 | 0.0000 |
| azure `prebuilt-layout` | 0.73 | 2 | 0 | 2 (dev cap) | 0.0200 |
| aws Textract `TABLES`+`LAYOUT` | 0.98 | 7 | 6 | 11 | 0.1650 |

Local scores **0.87 — above threshold — so `hybrid_escalate` accepts it and never calls a cloud parser.** Yet it lost every heading (the brochure's "headings" are display text the layout heuristic does not recognise) and flattened the spec grids into a 127-row blob. The score is fooled because word-validity, density and encoding are all perfect on brochure prose, and the only real failure — structure — is 20% of the weight.

Everything downstream inherits this: heading-aware chunking has nothing to work with, and the spec tables (the part of a brochure people actually query — "what's the boot space of the ZXi+?") are unusable.

If brochures or spec-sheets were a real corpus, the fix is one of: lower the threshold, up-weight `structure_score`, or add a doc-type routing rule that sends them straight to cloud. Per `CLAUDE.md`, any such change is backed by `make eval` before and after, with the bootstrap CI reported.

---

## Tracing a parse

`quality.py` and the adapters emit structured `log` events at every decision point — format detection, the text-layer probe, handler dispatch, per-page extraction counts, the Textract layout-vs-lines reconstruction choice, the quality components, the hybrid route. They are `DEBUG` level, so `settings.debug=true` (or `LOG_LEVEL=DEBUG`) turns them on.

`scripts/parsing/_trace.py` folds those events into a per-run trace using `structlog.testing.capture_logs`, interleaved with explicit `tracer.step(...)` / `tracer.note(...)` / `tracer.obj(...)` calls from the script. Each numbered script writes to `outputs/parsing/<stem>/`:

| file | content |
|---|---|
| `<provider>.trace.log` | the step-by-step narrative — format probe → handler → per-page counts → reconstruction → quality breakdown → route decision. Read this first. |
| `<provider>.parsed.json` | `dataclasses.asdict(ParsedDocument)` — every block, table grid, figure, bbox, metric, meta key. |
| `<provider>.content.md` | `doc.markdown` — what the chunker will see. Diff it across providers to see what escalating buys. |

`04_hybrid_compare.py` additionally writes `compare.trace.log`: every provider on one document, scored on the same heuristics, with the hybrid route and its reasoning. This is the artefact that answers "how did you decide which parser to use?" with evidence rather than an opinion.

`outputs/` is gitignored — these are artefacts of a run, not fixtures.

### Example: the Textract reconstruction step in a trace

```
log:textract_route          route=async_s3 pages=11 size_mb=1.25 features=["TABLES","LAYOUT"]
log:textract_uploaded       bucket=… key=sentinel/textract/f87a57e957adb550/suzuki-brezza.pdf
log:textract_response       raw_blocks=3611  block_kinds={PAGE:11, LINE:599, WORD:2064,
                            TABLE:6, CELL:809, LAYOUT_TITLE:6, LAYOUT_FIGURE:32, ...}
log:textract_reconstruction strategy=layout  layout_elements=99
log:textract_assembled      prose_blocks=71  table_blocks=6
```

If you wanted to debug why 32 `LAYOUT_FIGURE` nodes collapsed to 8 figure blocks, this tells you where to look.

---

## Azure vs AWS: the differences that matter

Common misconception: "AWS for structure and tables, Azure for OCR." It is the other way round — **Azure is the stronger OCR and layout engine**; Textract's advantages are ecosystem fit and forms.

| dimension | winner | why |
|---|---|---|
| scanned pages, hard OCR | **Azure** | `azure_parser.py` docstring: strongest of the three exactly on the cases where parsing fails — scans, multi-column, borderless tables. |
| multi-column / complex layout | **Azure** | its layout model reconstructs cross-column reading order more reliably. |
| tables | **even; Azure edge on borderless** | both rebuild grids from cell coordinates. Azure wins when structure is implied by alignment; Textract `TABLES` is solid on ruled tables (Brezza page 10 has 172 ruling lines → 6 clean grids). |
| structure / headings for RAG | **even, with the right mode** | Azure `prebuilt-layout` → markdown `##`; Textract `LAYOUT` → `LAYOUT_TITLE` / `LAYOUT_SECTION_HEADER`. Both recovered the Brezza display headings; local recovered zero. |
| integration effort | **Azure, decisively** | Azure: send bytes, get markdown. Textract multi-page: stage to S3, start a job, poll a JobId, paginate a flat block graph and rebuild the tree yourself. |
| cost per page (see `observability.PAGE_PRICING`) | **Textract for OCR-only; Azure for layout** | OCR: `textract-detect` and `azure:prebuilt-read` both ≈ $0.0015. Layout: `azure:prebuilt-layout` $0.010 < `textract-analyze` $0.015. The mode you want for RAG is cheaper on Azure. |
| free-tier gotcha | **Textract** (no silent cap) | Azure F0 silently caps analysis at 2 pages and drops the rest — a "half my index is missing" incident. See the dev page cap below. |
| forms / key-value | **Textract** `FORMS` | purpose-built. Irrelevant for a prose knowledge corpus. |
| multi-page latency | **Azure** | no S3 upload + job-polling round trip. |

**One-line version:** default to Azure `prebuilt-layout` for a knowledge corpus — better on the hard cases, cheaper for layout mode, dramatically simpler to integrate. Reach for Textract when you are already all-in on AWS, need `FORMS`, or need OCR-only cost at scale without layout. This is why `hybrid.py` makes **Azure the default escalation target**: its layout model returns markdown directly, which needs no reconstruction.

---

## Dev vs production: the `debug` switch

`settings.debug` (env `DEBUG`) is the development switch. Two effects:

1. **Logging** is forced to `DEBUG` regardless of `LOG_LEVEL` (via `settings.effective_log_level`), so the trace events above are emitted without a second knob.
2. **Azure Document Intelligence analysis is capped** at `settings.azure_di_dev_page_limit` pages (default 2), passed as the SDK's `pages="1-2"` parameter. Per-page billing means an accidental full-corpus parse during development is a real bill, and a free-tier resource caps at 2 pages anyway — so the cap makes that explicit and logged (`azure_page_limited`, WARNING) rather than a silent truncation. Set the limit to `0` to disable it even in debug mode; pass `pages=` to `parse()` to override per call.

With `DEBUG=false` (production), the whole document is sent in one call and `LOG_LEVEL` applies as set. Nothing else in the pipeline changes behaviour on this flag today; it is the hook for future cost/quota-sensitive dev paths.

---

## Common questions

**Why not just always use the cloud parser and skip the local step entirely?** Cost and latency, multiplied by every document you will ever ingest, forever. Local parsing is free and takes ~0.4s/doc; Azure and AWS cost real money per page and take 3–5s/doc. On a corpus where most documents parse perfectly locally (78% in the illustrative bake-off below), routing everyone through the cloud parser means paying and waiting for work that was already done for free. `hybrid_escalate` lands within a few points of the best single provider's quality for roughly a quarter of the cost, precisely because it only pays for the documents that actually need it.

**What exactly does the quality score miss, and why?** Structure is only 20% of its weight (`0.20 · structure_score` in the formula above). The other 80% — text density, word validity, encoding — can be perfect on a page that lost every heading and mangled every table, because those checks only ask "are the characters plausible English," not "is the shape of the document intact." The `suzuki-brezza.pdf` walkthrough is the concrete proof: local scores 0.87 (comfortably above the 0.72 threshold) while losing all structure. See [the failure mode the score does not catch](#the-failure-mode-the-score-does-not-catch).

**Why is Azure the default escalation target instead of AWS, if both handle scans and complex layouts?** Per [Azure vs AWS](#azure-vs-aws-the-differences-that-matter): Azure is the stronger OCR/layout engine, cheaper for the layout mode you actually want for RAG ($0.010 vs $0.015/page), and returns ready-to-chunk markdown directly. Textract returns a flat block graph you must reconstruct into a tree yourself. Textract's genuine advantages — AWS-ecosystem fit, the `FORMS` feature — aren't relevant to a prose knowledge corpus.

**What happens if `DEBUG` is left on in production?** Two things, and only one is obviously dangerous. Forced `DEBUG` logging is just noisy. But Azure Document Intelligence gets capped at `azure_di_dev_page_limit` (default 2) pages *per document* — meaning a "production" run accidentally left with `DEBUG=true` silently analyzes only the first two pages of every PDF sent to Azure. It's logged as `azure_page_limited` at `WARNING`, but a warning nobody is watching for is indistinguishable from silence. This is the same failure shape as the Azure free-tier gotcha in the table above, self-inflicted instead of imposed by the vendor.

**Why does `hybrid_merge` always pay cloud cost, even when local turned out fine?** Because merge's whole premise is comparing local and cloud output and keeping the better half of each — text from whichever scored higher, tables from whichever produced sounder grids. It cannot know which half is better without having both results in hand, unlike `hybrid_escalate`, which never runs the cloud parser once local already clears the threshold.

**Why use a coarse `BlockType` (heading/paragraph/table/figure/list/code/key_value/header/footer) instead of something richer, like footnote or caption or list-item?** Because every parser reports fine-grained structure differently, if it reports it at all. A shared vocabulary only works if all three providers — pdfplumber, Document Intelligence, Textract — can faithfully populate every category. A taxonomy only one provider can actually produce isn't shared, it's aspirational, and the hybrid strategies (merge, compare) would break the moment they tried to line up a `footnote` from one provider against nothing from another.

**Why does the local parser exclude table regions from the text pass instead of just running one text extraction over the whole page?** Because tables are extracted *first*, with their bounding boxes recorded, specifically so the text pass can skip anything inside them. Skip that step and every table's numbers appear twice — once as a structured grid, once as loose paragraph noise — which corrupts both the text-density quality score and, later, retrieval (the same number shows up as two different "chunks" with two different meanings attached).

**If a document type is found to reliably score well but lose structure (like brochures), what's the actual fix?** Per `CLAUDE.md`, back any change with `make eval` before and after. The concrete levers, in the order the `suzuki-brezza.pdf` section names them: lower `parse_quality_threshold`, up-weight `structure_score` in the formula, or add a doc-type routing rule that sends that document type straight to cloud regardless of what the heuristic says. All three are deliberate policy changes, not code fixes — which is why none of them should happen without the eval gate.

---

## Result _(illustrative — run `make bench-parsers`)_

| provider | overall | text | tables | reading order | secs/doc | usd/doc |
|---|---|---|---|---|---|---|
| local | 0.71 | 0.94 | 0.52 | 0.88 | 0.4 | 0.0000 |
| azure | 0.89 | 0.96 | 0.91 | 0.95 | 3.1 | 0.0800 |
| aws | 0.85 | 0.95 | 0.86 | 0.91 | 4.7 | 0.1200 |
| hybrid_escalate | 0.86 | 0.95 | 0.85 | 0.93 | 1.2 | 0.0180 |

The interesting row is the last one: within a few points of the best single provider at roughly a quarter of the cost, because 78% of the corpus never escalated. The interesting column is cost-per-quality-point, not quality — a parser that scores 0.02 higher for eight times the price has not won.

Per-format breakdown matters as much as the average. Local matches or beats both clouds on DOCX, PPTX and XLSX (the structure is already in the file — a layout model has nothing to recover) and loses badly on scans. A single average hides that entirely, which is why the bake-off reports by format.

---

## What breaks in production

- **Scanned pages with no text layer.** Detected by `_pdf_has_text_layer` probing the first five pages; routed straight to cloud OCR by `CLOUD_PREFERRED_FORMATS`.
- **A parse that scores "good enough" but lost its structure.** The quality heuristic weights structure at 20%, so a document with perfect characters and no headings can clear the threshold and never escalate — see [the failure mode the score does not catch](#the-failure-mode-the-score-does-not-catch). The trace (`outputs/parsing/<stem>/*.trace.log`) is where you see this; the heuristic alone will not tell you.
- **Azure free-tier silently capping at 2 pages.** F0 resources analyse only the first two pages and return no error. `settings.debug` makes this an explicit, logged `pages="1-2"` cap; production (`DEBUG=false`) sends the whole document.
- **`parse_provider=aws` with no S3 bucket.** Textract's multi-page API has no inline-bytes option. Config validation refuses this combination at startup rather than failing 20 minutes into ingestion.
- **Tables extracted twice.** Both the local and Azure parsers exclude table regions from the text pass; Textract needs `_ids_inside_tables` to drop LINE/WORD blocks already consumed by a CELL. Without it, every table's numbers appear as a grid *and* as loose paragraph noise.
- **Running headers on every page** diluting every chunk. Flagged by `_encoding_score`.
- **Speaker notes dropped from decks.** In architecture and design decks the rationale lives in the notes while the slide is a diagram with three words on it. `python-pptx` reads them and `local_parser` keeps them.
- **Diagrams invisible to a text index.** `parsing/figures.py` describes them with a vision model at ingest, cached by image content hash, with decorative images (logos, rules) dropped rather than becoming empty chunks.

---

## Files

| path | what |
|---|---|
| `sentinel/parsing/base.py` | `BaseParser`, `DocFormat`, `detect_format`, the text-layer probe |
| `sentinel/parsing/local_parser.py` | pdfplumber / python-docx / python-pptx / openpyxl adapter |
| `sentinel/parsing/azure_parser.py` | Document Intelligence adapter |
| `sentinel/parsing/aws_parser.py` | Textract adapter + block-graph reconstruction |
| `sentinel/parsing/quality.py` | reference-free quality score (the routing gate) |
| `sentinel/parsing/hybrid.py` | the four routing strategies |
| `sentinel/parsing/figures.py` | vision-model figure enrichment |
| `sentinel/core/types.py` | `ParsedDocument`, `Block`, `Table`, `Figure`, `ParseMetrics` |
| `scripts/parsing/0{1..4}_*.py` | per-provider + comparison demos, each writing a trace |
| `scripts/parsing/_trace.py` | the tracer |
| `sentinel/evaluation/parsing_eval.py` | offline accuracy measurement against ground truth |
| `benchmarks/parser_bakeoff.py` | the bake-off that tunes the escalation threshold |
