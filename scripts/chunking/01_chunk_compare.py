"""Chunk a handful of real documents with every non-LLM, non-vector strategy and dump the result.

    uv run python scripts/chunking/01_chunk_compare.py                # the curated 5-doc set
    uv run python scripts/chunking/01_chunk_compare.py knowledge/adr/adr-001-flagd-for-fault-injection.md
    uv run python scripts/chunking/01_chunk_compare.py --strategies structural,recursive knowledge/...
    uv run python scripts/chunking/01_chunk_compare.py --size 200 --overlap 30 --strategies fixed knowledge/...

This is the chunking-stage analogue of ``scripts/parsing/01_parse_local.py``: pick one real
document, run it through the stage, and read the full output rather than a summary. The
question it answers is *"what does each strategy actually do to this text"* — how many chunks,
where the cuts land, what ``embed_text`` gets vs. what ``context_text`` returns, and how the
heading-path prefix turns an otherwise-unretrievable fragment into a findable one.

Artefacts are written under ``outputs/chunking/<doc-stem>/``, **numbered in build order** so the
files sort the way the strategy modules read (``_01_fixed`` → ``_02_recursive`` → …). Read
``00_document.log`` first, then walk ``01_``, ``02_``, … top to bottom and watch each strategy
answer the shortcoming of the one before it — its header carries a ``vs <n> (<prev>)`` line so
the comparison has a number to anchor on.

``00_document.log``
    The ``ParsedDocument`` the strategies worked from — title, doc_type, metrics, every block
    with its token count — on its own, so it can be read before any strategy.

``NN_<strategy>.log``
    Human-readable, read top to bottom, one file per strategy. Repeats the ``00_document.log``
    contents, then that strategy's chunk count (and its delta from the previous strategy), token
    distribution, and every chunk rendered readable (both text fields, heading path, meta).
    Self-contained: you can read one strategy without the others. This is a dev artefact on a
    non-restricted corpus, so it prints text in full; it is not a production log and does not
    obey the ids-and-hashes-only rule.

``NN_<strategy>.chunks.jsonl``
    One ``dataclasses.asdict(Chunk)`` per line, nothing else. ``Chunk(**json.loads(line))``
    round-trips it straight into ``embedder.embed_chunks(...)``. This is raw chunker output:
    ``_finalise`` has stamped ``chunk_id``, ``token_count``, ``content_hash``, ``strategy`` and
    the doc-level fields (``doc_id``, ``tenant_id``, ``source_uri``, ``effective_date``), but
    **not** ``index_version`` or the ACL fields — the real ingest pipeline adds those at embed
    time, and so should you. Pick the strategy you want when you embed; this writes all of them.

The ``NN`` is the strategy's position in ``--strategies`` (default order below is build order).

``outputs/chunking/leaderboard.md``
    The ``chunks/doc`` table across the whole run. Read it next to the recall column of
    ``benchmarks/chunking_sweep.py`` — more chunks is not better, it inflates the index and
    crowds the top-k with near-duplicates.

Note on ``fixed``/``recursive`` and the short documents: ``size`` is measured in **tokens**,
not characters (``ntokens`` uses tiktoken ``cl100k_base``, ~4 chars/token for English prose).
The ADRs are ~450 tokens, under the 550 default, so they come out as a single chunk — correct,
and the ``markdown NNN tokens`` line in every log makes it visible. Pass ``--size`` to force
smaller windows.

Strategies covered: ``fixed``, ``recursive``, ``structural``, ``sentence_window``,
``parent_document``. Deliberately excluded — ``runbook`` and ``glossary`` (doc-type specific,
walk those through on a runbook / the glossary), ``semantic`` (needs a live embedder) and
``contextual`` (needs a live LLM). Set both up and add them here afterwards.

``outputs/`` is gitignored — this is the artefact of a run, not a fixture.
"""

from __future__ import annotations

import argparse
import inspect
import json
import statistics
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sentinel.chunking.base import get_chunker, ntokens
from sentinel.core.observability import configure_logging
from sentinel.core.types import Chunk, ParsedDocument
from sentinel.parsing.local_parser import LocalParser

OUTPUT_ROOT = Path("outputs/chunking")

# The strategies this script compares: everything that needs neither a live embedder (semantic)
# nor a live LLM (contextual), minus the two chosen by document type rather than benchmarked
# (runbook, glossary).
DEFAULT_STRATEGIES = ["fixed", "recursive", "structural", "sentence_window", "parent_document"]

# A curated spread, chosen so the strategies have visibly different material to work on:
#
#   adr-006  — short, flat `##` sections, pure prose. `fixed` barely splits it; `structural`
#              gives one chunk per section. The floor case.
#   adr-001  — same shape, a little longer. A second ADR so the per-section behaviour is not a
#              one-document accident.
#   inc-2614 — headings + a bulleted timeline + a fenced code block (the detection signature).
#              Tests that a code fence is never cut in half and that list items survive.
#   inc-2691 — another incident, different service and wording — checks the incident shape
#              generalises.
#   security-review-2026h1 — nested `### SEC-n` findings under `## Findings`: the deepest
#              heading hierarchy in the set, so the `structural` heading-path prefix and the
#              `parent_document` section grouping have something real to show.
CURATED = [
    ("knowledge/adr/adr-006-two-field-chunks.md", "adr"),
    ("knowledge/adr/adr-001-flagd-for-fault-injection.md", "adr"),
    ("knowledge/incident/inc-2614-ad-adhighcpu.md", "incident"),
    ("knowledge/incident/inc-2691-shipping-intlshippingslowdown.md", "incident"),
    ("knowledge/restricted/security-review-2026h1.md", "page"),
]

_PARSER = LocalParser()
_RULE = "=" * 100


def parse(path: str, doc_type: str) -> ParsedDocument:
    return _PARSER.parse(path, doc_type=doc_type)


def build_chunker(strategy: str, size: int | None, overlap: int | None):
    """Instantiate a strategy, passing only the size/overlap kwargs its constructor accepts.

    `fixed`/`recursive`/`structural` take (size, overlap); `sentence_window` takes (window) and
    `parent_document` takes (parent_size, child_size), so a blanket `size=` would be a TypeError.
    """
    from sentinel.chunking.base import REGISTRY

    params = inspect.signature(REGISTRY[strategy].__init__).parameters
    kwargs: dict[str, int] = {}
    if size is not None and "size" in params:
        kwargs["size"] = size
    if overlap is not None and "overlap" in params:
        kwargs["overlap"] = overlap
    return get_chunker(strategy, **kwargs)


# --------------------------------------------------------------------------------- .log rendering
def render_parsed_document(doc: ParsedDocument) -> str:
    m = doc.metrics
    lines = [
        _RULE,
        "PARSED DOCUMENT",
        _RULE,
        f"doc_id          {doc.doc_id}",
        f"title           {doc.title}",
        f"source_uri      {doc.source_uri}",
        f"doc_type        {doc.doc_type}",
        f"tenant_id       {doc.tenant_id}",
        f"effective_date  {doc.effective_date}",
        f"markdown        {ntokens(doc.markdown)} tokens, {len(doc.markdown)} chars",
        "",
        f"metrics         provider={m.provider} pages={m.pages} blocks={len(doc.blocks)} "
        f"tables={m.tables} figures={m.figures} chars={m.characters} "
        f"quality={m.quality_score} duration_s={m.duration_s:.3f}",
        f"warnings        {m.warnings or '—'}",
        "",
        f"BLOCKS ({len(doc.blocks)}) — in reading order",
        "-" * 100,
    ]
    for i, b in enumerate(doc.blocks):
        lines.append(f"[{i:>3}] {b.type.value:<10} level={b.level} page={b.page} tokens={ntokens(b.text)}")
        for row in (b.text or "").splitlines() or [""]:
            lines.append(f"      {row}")
        lines.append("")
    return "\n".join(lines)


def render_chunk(i: int, c: Chunk) -> str:
    same = c.embed_text == c.context_text
    lines = [
        f"  --- chunk {i} " + "-" * 84,
        f"  chunk_id       {c.chunk_id}",
        f"  parent_id      {c.parent_id}",
        f"  heading_path   {c.heading_path or '—'}",
        f"  doc_type       {c.doc_type}   strategy={c.strategy}   token_count={c.token_count}   "
        f"content_hash={c.content_hash}",
        f"  page           {c.page}   block_type={c.meta.get('block_type', '—')}",
        f"  meta           {json.dumps({k: v for k, v in c.meta.items() if k != 'block_type'})}",
        f"  embed==context {same}",
        "",
        "  embed_text (vectorised):",
    ]
    lines += [f"  | {row}" for row in c.embed_text.splitlines() or [""]]
    if not same:
        lines += ["", "  context_text (returned to the generator):"]
        lines += [f"  | {row}" for row in c.context_text.splitlines() or [""]]
    return "\n".join(lines)


def render_strategy_header(
    ordinal: int, strategy: str, chunker_name: str, chunks: list[Chunk], prev: tuple[str, int] | None
) -> str:
    tokens = [c.token_count for c in chunks] or [0]
    diverge = sum(1 for c in chunks if c.embed_text != c.context_text)
    delta = f"vs {prev[1]} ({prev[0]})" if prev else "first strategy — the baseline"
    return "\n".join(
        [
            "",
            _RULE,
            f"STRATEGY  {ordinal:02d}  {strategy}   (chunker: {chunker_name})",
            _RULE,
            f"chunks              {len(chunks)}   [{delta}]",
            f"tokens/chunk        min {min(tokens)} · median {int(statistics.median(tokens))} · "
            f"mean {int(statistics.fmean(tokens))} · max {max(tokens)}",
            f"distinct parent_ids {len({c.parent_id for c in chunks})}",
            f"embed ≠ context     {diverge} / {len(chunks)}",
            "",
        ]
    )


# --------------------------------------------------------------------------------- run
def process_doc(
    path: str, doc_type: str, strategies: list[str], size: int | None, overlap: int | None
) -> dict[str, int]:
    doc = parse(path, doc_type)
    out_dir = OUTPUT_ROOT / Path(path).stem
    out_dir.mkdir(parents=True, exist_ok=True)

    preamble = [
        f"# chunking trace — {path}",
        f"# generated {datetime.now(UTC).isoformat()}",
        f"# size override: {size}   overlap override: {overlap}",
        "",
        render_parsed_document(doc),
    ]

    # The ParsedDocument on its own, so it sorts first (00_) and can be read before any strategy.
    (out_dir / "00_document.log").write_text("\n".join(preamble))

    # Numbered so the output files sort into the same build order as the strategy modules
    # (_01_fixed → _02_recursive → …): read 00_document.log, then walk 01_, 02_, … top to bottom
    # and watch each strategy answer the shortcoming of the one before it.
    counts: dict[str, int] = {}
    prev: tuple[str, int] | None = None
    for ordinal, strategy in enumerate(strategies, start=1):
        chunker = build_chunker(strategy, size, overlap)
        chunks = chunker.split(doc)
        counts[strategy] = len(chunks)
        stem = f"{ordinal:02d}_{strategy}"

        with (out_dir / f"{stem}.chunks.jsonl").open("w") as fh:
            for c in chunks:
                fh.write(json.dumps(asdict(c), ensure_ascii=False, default=str) + "\n")

        log = [
            *preamble,
            render_strategy_header(ordinal, strategy, chunker.name, chunks, prev),
            *[render_chunk(i, c) + "\n" for i, c in enumerate(chunks)],
        ]
        (out_dir / f"{stem}.log").write_text("\n".join(log))
        prev = (strategy, len(chunks))

    print(f"  {Path(path).stem}")
    for ordinal, strategy in enumerate(strategies, start=1):
        stem = f"{ordinal:02d}_{strategy}"
        print(f"    {stem:<24} {counts[strategy]:>3} chunks  ->  {stem}.log / {stem}.chunks.jsonl")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="documents to chunk (default: the curated 5-doc set)")
    ap.add_argument("--doc-type", help="override doc_type for supplied paths (default: parent dir name)")
    ap.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES), help="comma-separated strategy names")
    ap.add_argument("--size", type=int, help="token size override (fixed/recursive/structural only)")
    ap.add_argument("--overlap", type=int, help="token overlap override (fixed/recursive/structural only)")
    args = ap.parse_args()

    configure_logging(json_output=False)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    docs = [(p, args.doc_type or Path(p).parent.name) for p in args.paths] if args.paths else CURATED

    print(f"chunking {len(docs)} document(s) × {len(strategies)} strategies\n")
    leaderboard: dict[str, list[int]] = {s: [] for s in strategies}
    for path, doc_type in docs:
        counts = process_doc(path, doc_type, strategies, args.size, args.overlap)
        for strategy, n in counts.items():
            leaderboard[strategy].append(n)

    lines = [
        "# Chunking leaderboard",
        "",
        f"{len(docs)} documents. `chunks/doc` is the number that matters here — read it next to a "
        "recall column, not on its own.",
        "",
        "| strategy | total chunks | chunks/doc (mean) | min | max |",
        "|---|---|---|---|---|",
    ]
    for strategy in strategies:
        vals = leaderboard[strategy]
        lines.append(f"| `{strategy}` | {sum(vals)} | {statistics.fmean(vals):.1f} | {min(vals)} | {max(vals)} |")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "leaderboard.md").write_text("\n".join(lines) + "\n")

    print(
        f"\nwrote {OUTPUT_ROOT}/  —  <doc>/00_document.log  +  <doc>/NN_<strategy>.log"
        f"  +  <doc>/NN_<strategy>.chunks.jsonl  +  leaderboard.md"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
