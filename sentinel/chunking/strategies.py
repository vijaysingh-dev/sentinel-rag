"""Chunking strategies.

Each strategy is registered by name and takes a `ParsedDocument`, so they are directly comparable
in a benchmark sweep. Read them in order — they build on one another, and the progression is the
argument for why the later ones exist.

Choosing between them is an empirical question, and `benchmarks/chunking_sweep.py` answers it for
a given corpus. The defensible default is `structural`: recursive splitting inside heading
sections, with the heading path prefixed onto every chunk. That prefix is usually worth more than
switching embedding models, because it turns unretrievable fragments into findable ones.
"""

from __future__ import annotations

import re
from typing import Any

from sentinel.chunking.base import BaseChunker, ntokens, register, truncate_tokens
from sentinel.core.types import Block, BlockType, Chunk, ParsedDocument

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
STEP_SPLIT = re.compile(r"\n(?=\s*(?:\d+[.)]|Step\s+\d+|[-*]\s))", re.IGNORECASE)


def split_sentences(text: str) -> list[str]:
    """Rule-based sentence splitting.

    Good enough for technical prose. Abbreviations such as "e.g." and "Fig. 3" will occasionally
    split wrongly; if that shows up as a measurable recall problem, swap in pysbd or spaCy here —
    but measure first, because the failure is usually harmless.
    """
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s.strip()]


# --------------------------------------------------------------------------------- fixed
@register("fixed")
class FixedTokenChunker(BaseChunker):
    """Fixed-size token windows with overlap.

    The control group. It ignores every structural signal in the document, which is exactly what
    makes it useful as a baseline: any strategy that cannot beat this on your corpus is not
    earning its complexity.

    Overlap exists because a fact sitting on a boundary would otherwise be split across two chunks
    and appear coherently in neither. The cost is duplication — 15% overlap means ~15% more
    vectors and near-duplicate results crowding the top-k.
    """

    def __init__(self, size: int = 550, overlap: int = 60):
        self.size = size
        self.overlap = overlap
        self.name = f"fixed_{size}_{overlap}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        from sentinel.chunking.base import _encoder

        encoder = _encoder()
        ids = encoder.encode(doc.markdown)
        step = max(self.size - self.overlap, 1)

        chunks: list[Chunk] = []
        for seq, start in enumerate(range(0, len(ids), step)):
            window = ids[start : start + self.size]
            if not window:
                break
            text = encoder.decode(window)
            chunks.append(
                Chunk(
                    chunk_id=Chunk.make_id(doc.doc_id, seq, text),
                    doc_id=doc.doc_id,
                    embed_text=text,
                    context_text=text,
                    parent_id=doc.doc_id,
                    meta={"start_token": start},
                )
            )
        return self._finalise(doc, chunks)


# ----------------------------------------------------------------------------- recursive
@register("recursive")
class RecursiveChunker(BaseChunker):
    """Splits on the most semantic separator available, recursing to weaker ones as needed.

    The pragmatic default in most RAG systems. It respects paragraph and sentence boundaries when
    it can, and only falls back to arbitrary token cuts for text that has no boundaries at all.
    """

    SEPARATORS = ["\n\n", "\n", ". ", "; ", ", ", " "]

    def __init__(self, size: int = 550, overlap: int = 60):
        self.size = size
        self.overlap = overlap
        self.name = f"recursive_{size}_{overlap}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        pieces = self._recursive_split(doc.markdown, self.SEPARATORS)
        pieces = self._apply_overlap(pieces)

        chunks = [
            Chunk(
                chunk_id=Chunk.make_id(doc.doc_id, seq, text),
                doc_id=doc.doc_id,
                embed_text=text,
                context_text=text,
                parent_id=doc.doc_id,
            )
            for seq, text in enumerate(pieces)
        ]
        return self._finalise(doc, chunks)

    def _recursive_split(self, text: str, separators: list[str]) -> list[str]:
        if ntokens(text) <= self.size:
            return [text] if text.strip() else []

        if not separators:
            # No boundaries left; cut on tokens as a last resort.
            from sentinel.chunking.base import _encoder

            encoder = _encoder()
            ids = encoder.encode(text)
            return [encoder.decode(ids[i : i + self.size]) for i in range(0, len(ids), self.size)]

        separator, rest = separators[0], separators[1:]
        parts = text.split(separator)

        out: list[str] = []
        buffer = ""
        for part in parts:
            candidate = f"{buffer}{separator}{part}" if buffer else part
            if ntokens(candidate) <= self.size:
                buffer = candidate
                continue
            if buffer:
                out.append(buffer)
            # An oversized single part recurses to a weaker separator rather than being dropped.
            if ntokens(part) <= self.size:
                buffer = part
            else:
                buffer = ""
                out.extend(self._recursive_split(part, rest))
        if buffer:
            out.append(buffer)
        return [o for o in out if o.strip()]

    def _apply_overlap(self, pieces: list[str]) -> list[str]:
        if self.overlap <= 0 or len(pieces) < 2:
            return pieces
        out = [pieces[0]]
        for i in range(1, len(pieces)):
            tail = truncate_tokens(pieces[i - 1][-2000:], self.overlap)
            out.append(f"{tail} {pieces[i]}")
        return out


# ---------------------------------------------------------------------------- structural
@register("structural")
class StructuralChunker(BaseChunker):
    """Splits along the document's own heading hierarchy and prefixes the heading path.

    This is the recommended default, and the heading prefix is the reason.

    A chunk reading *"Set this to false to skip validation"* is unretrievable — no realistic query
    lands near it in embedding space. The same chunk embedded as *"ICE Configuration > Validation >
    strict_mode — Set this to false to skip validation"* is findable by dozens of queries. The
    prefix costs a handful of tokens and typically buys more recall than upgrading the embedding
    model.

    Tables are emitted as their own chunks with their caption and heading path attached, because a
    table split across chunk boundaries loses its header and becomes orphaned numbers.
    """

    def __init__(self, size: int = 550, overlap: int = 60, keep_tables_whole: bool = True):
        self.size = size
        self.overlap = overlap
        self.keep_tables_whole = keep_tables_whole
        self.inner = RecursiveChunker(size, overlap)
        self.name = f"structural_{size}_{overlap}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        sections = self._sections(doc)
        chunks: list[Chunk] = []
        seq = 0

        for heading_path, blocks in sections:
            header = f"{doc.title} > {heading_path}" if heading_path else doc.title
            parent_id = f"{doc.doc_id}::{heading_path or 'root'}"

            for block in blocks:
                if block.type is BlockType.TABLE and self.keep_tables_whole:
                    body = block.table.markdown if block.table else block.text
                    caption = (block.table.caption if block.table else None) or ""
                    embed = f"{header}\nTable: {caption}\n{body}".strip()
                    chunks.append(
                        Chunk(
                            chunk_id=Chunk.make_id(doc.doc_id, seq, embed),
                            doc_id=doc.doc_id,
                            embed_text=truncate_tokens(embed, self.size * 2),
                            context_text=f"[{header}]\n{body}",
                            parent_id=parent_id,
                            heading_path=heading_path,
                            page=block.page,
                            meta={"block_type": "table"},
                        )
                    )
                    seq += 1
                    continue

                if not block.text.strip():
                    continue

                for piece in self.inner._recursive_split(block.text, RecursiveChunker.SEPARATORS):
                    chunks.append(
                        Chunk(
                            chunk_id=Chunk.make_id(doc.doc_id, seq, piece),
                            doc_id=doc.doc_id,
                            embed_text=f"{header}\n\n{piece}",
                            context_text=f"[{header}]\n{piece}",
                            parent_id=parent_id,
                            heading_path=heading_path,
                            page=block.page,
                            meta={"block_type": block.type.value},
                        )
                    )
                    seq += 1

        return self._finalise(doc, chunks)

    @staticmethod
    def _sections(doc: ParsedDocument) -> list[tuple[str, list[Block]]]:
        """Groups blocks under their heading path, maintaining a stack of open headings."""
        sections: list[tuple[str, list[Block]]] = []
        stack: dict[int, str] = {}
        current: list[Block] = []
        current_path = ""

        for block in doc.blocks:
            if block.type is BlockType.HEADING:
                if current:
                    sections.append((current_path, current))
                    current = []
                level = block.level or 2
                stack = {k: v for k, v in stack.items() if k < level}
                stack[level] = block.text
                current_path = " > ".join(stack[k] for k in sorted(stack))
            else:
                current.append(block)

        if current:
            sections.append((current_path, current))
        return sections or [("", doc.blocks)]


# ----------------------------------------------------------------------- sentence window
@register("sentence_window")
class SentenceWindowChunker(BaseChunker):
    """Embeds one sentence, returns that sentence plus its neighbours.

    The purest expression of decoupling retrieval from generation: maximum precision in the vector
    (one claim per embedding) while the generator still receives readable surrounding context.

    Costs: 3-10x more vectors than paragraph chunking, and adjacent sentences produce overlapping
    windows, so the retriever must deduplicate before assembling a prompt or the context budget
    fills with repeated text.

    Best on dense factual documents — methodology, policy, specifications — where a single sentence
    is a complete fact.
    """

    def __init__(self, window: int = 3):
        self.window = window
        self.name = f"sentence_window_{window}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        seq = 0

        for heading_path, blocks in StructuralChunker._sections(doc):
            header = f"{doc.title} > {heading_path}" if heading_path else doc.title
            text = "\n".join(b.text for b in blocks if b.text.strip())
            sentences = split_sentences(text)

            for i, sentence in enumerate(sentences):
                lo = max(0, i - self.window)
                hi = min(len(sentences), i + self.window + 1)
                window_text = " ".join(sentences[lo:hi])
                chunks.append(
                    Chunk(
                        chunk_id=Chunk.make_id(doc.doc_id, seq, sentence),
                        doc_id=doc.doc_id,
                        embed_text=f"{header}\n{sentence}",
                        context_text=f"[{header}]\n{window_text}",
                        parent_id=f"{doc.doc_id}::{heading_path or 'root'}",
                        heading_path=heading_path,
                        meta={"sentence_index": i, "window": self.window},
                    )
                )
                seq += 1

        return self._finalise(doc, chunks)


# ----------------------------------------------------------------------- parent document
@register("parent_document")
class ParentDocumentChunker(BaseChunker):
    """Embeds small children, returns the whole parent section.

    The gotcha, and the detail worth knowing: **you must deduplicate by `parent_id` at retrieval
    time.** Five matching children of one parent will each pull in the identical parent text and
    consume the entire context budget on one section. `retrieval/dedupe.py` handles it, but a
    pipeline that forgets is quietly wasting most of its prompt.
    """

    def __init__(self, parent_size: int = 1800, child_size: int = 250):
        self.parent_size = parent_size
        self.child_size = child_size
        self.child = RecursiveChunker(child_size, overlap=0)
        self.name = f"parent_{parent_size}_{child_size}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        seq = 0

        for heading_path, blocks in StructuralChunker._sections(doc):
            header = f"{doc.title} > {heading_path}" if heading_path else doc.title
            parent_text = "\n\n".join(b.text for b in blocks if b.text.strip())
            if not parent_text.strip():
                continue

            parent_id = f"{doc.doc_id}::{heading_path or 'root'}"
            context = truncate_tokens(f"[{header}]\n{parent_text}", self.parent_size)

            for child_text in self.child._recursive_split(parent_text, RecursiveChunker.SEPARATORS):
                chunks.append(
                    Chunk(
                        chunk_id=Chunk.make_id(doc.doc_id, seq, child_text),
                        doc_id=doc.doc_id,
                        embed_text=f"{header}\n{child_text}",
                        context_text=context,
                        parent_id=parent_id,
                        heading_path=heading_path,
                        meta={"is_child": True},
                    )
                )
                seq += 1

        return self._finalise(doc, chunks)


# ------------------------------------------------------------------------------- runbook
@register("runbook")
class RunbookChunker(BaseChunker):
    """Embeds individual steps, always returns the complete procedure.

    A retrieved half-runbook is worse than no runbook, because someone under incident pressure
    will execute the half. This is the specialisation that matters most in an operations corpus,
    and it is why chunking strategy is chosen per document type rather than globally.
    """

    def __init__(self, max_context_tokens: int = 3000):
        self.max_context_tokens = max_context_tokens
        self.name = "runbook"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        full = truncate_tokens(doc.markdown, self.max_context_tokens)
        steps = [s.strip() for s in STEP_SPLIT.split(doc.markdown) if s.strip()]

        # Not every runbook is numbered — plenty are written as prose under headings. Falling back
        # to one chunk for the whole document would make a long procedure retrievable only by its
        # title, so section headings become the retrieval units instead. The full procedure is
        # still what gets returned either way.
        if len(steps) < 2:
            steps = [
                f"{heading}\n" + "\n".join(b.text for b in blocks if b.text).strip()
                for heading, blocks in StructuralChunker._sections(doc)
            ]
            steps = [s.strip() for s in steps if s.strip()]

        # A document with no usable structure at all still needs one chunk, or it vanishes.
        if not steps:
            steps = [doc.markdown[:2000]]

        chunks = [
            Chunk(
                chunk_id=Chunk.make_id(doc.doc_id, i, step),
                doc_id=doc.doc_id,
                embed_text=f"{doc.title} (runbook)\n{step}",
                context_text=full,
                parent_id=doc.doc_id,
                doc_type="runbook",
                meta={"step_index": i, "is_parent_doc": True},
            )
            for i, step in enumerate(steps)
        ]
        return self._finalise(doc, chunks)


# ------------------------------------------------------------------------------ glossary
@register("glossary")
class GlossaryChunker(BaseChunker):
    """One term per chunk.

    Glossary entries are atomic. Merging "divisor" and "rebalance" into one 500-token chunk makes
    both harder to find, because the vector now represents an average of two unrelated concepts.
    """

    TERM = re.compile(
        r"^(?:#{2,4}\s+|\*\*)(?P<term>[^\n*#]{2,80})(?:\*\*)?\s*\n(?P<body>.*?)"
        r"(?=\n#{2,4}\s|\n\*\*[^\n*]{2,80}\*\*|\Z)",
        re.S | re.M,
    )

    def __init__(self) -> None:
        self.name = "glossary"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        matches = list(self.TERM.finditer(doc.markdown))
        if not matches:
            return StructuralChunker().split(doc)

        chunks = []
        for i, match in enumerate(matches):
            term = match.group("term").strip()
            body = match.group("body").strip()
            if not body:
                continue
            chunks.append(
                Chunk(
                    chunk_id=Chunk.make_id(doc.doc_id, i, term),
                    doc_id=doc.doc_id,
                    embed_text=f"Glossary term: {term}\n{body}",
                    context_text=f"**{term}** — {body}",
                    parent_id=doc.doc_id,
                    heading_path=term,
                    doc_type="glossary",
                )
            )
        return self._finalise(doc, chunks)


# ------------------------------------------------------------------------------ semantic
@register("semantic")
class SemanticChunker(BaseChunker):
    """Cuts where meaning shifts rather than where the token count runs out.

    Embeds every sentence, measures the cosine distance between consecutive sentences, and splits
    at distance peaks above a percentile threshold.

    Honest assessment, which is the useful thing to be able to say about it: it is **expensive** —
    you embed the whole corpus once at ingest just to decide boundaries, then embed the chunks
    again — and in published comparisons it frequently fails to beat recursive splitting by enough
    to justify that. Benchmark it on your corpus before adopting it; the interesting result is
    usually the cost column, not the recall column.
    """

    def __init__(self, percentile: int = 92, buffer: int = 1, max_size: int = 900):
        self.percentile = percentile
        self.buffer = buffer
        self.max_size = max_size
        self.name = f"semantic_p{percentile}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        import numpy as np

        from sentinel.core.registry import get_provider

        sentences = split_sentences(doc.markdown)
        if len(sentences) < 5:
            return StructuralChunker().split(doc)

        # Each sentence is embedded with its neighbours so one short sentence does not register as
        # a topic shift on its own.
        windows = [" ".join(sentences[max(0, i - self.buffer) : i + self.buffer + 1]) for i in range(len(sentences))]

        embedder = get_provider("embedder")
        vectors = np.asarray(embedder.embed_documents(windows), dtype="float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)

        distances = 1.0 - np.sum(vectors[:-1] * vectors[1:], axis=1)
        threshold = float(np.percentile(distances, self.percentile))
        breakpoints = [i + 1 for i, d in enumerate(distances) if d > threshold]

        chunks: list[Chunk] = []
        start, seq = 0, 0
        for end in [*breakpoints, len(sentences)]:
            body = " ".join(sentences[start:end]).strip()
            start = end
            if not body:
                continue
            # A semantically coherent run can still exceed the budget; fall back to recursive.
            pieces = (
                [body]
                if ntokens(body) <= self.max_size
                else RecursiveChunker(self.max_size, 0)._recursive_split(body, RecursiveChunker.SEPARATORS)
            )
            for piece in pieces:
                chunks.append(
                    Chunk(
                        chunk_id=Chunk.make_id(doc.doc_id, seq, piece),
                        doc_id=doc.doc_id,
                        embed_text=f"{doc.title}\n{piece}",
                        context_text=piece,
                        parent_id=doc.doc_id,
                        meta={"threshold": round(threshold, 4)},
                    )
                )
                seq += 1

        return self._finalise(doc, chunks)


# ---------------------------------------------------------------------------- contextual
class ContextualChunker(BaseChunker):
    """Wraps any strategy and prepends an LLM-written sentence situating each chunk.

    Anthropic's contextual-retrieval technique. For each chunk, a cheap model writes one or two
    sentences explaining where it sits in the document, and that is prepended before embedding.
    It recovers most of the context that splitting destroys — pronouns, implicit subjects,
    "as described above".

    What makes it affordable is prompt caching: the document occupies the static prefix of the
    prompt, so after the first chunk the provider charges roughly a tenth for those tokens. Without
    caching this is expensive enough to be impractical on a large corpus.

    Not registered by default because it costs one LLM call per chunk; enable via
    `settings.contextual_chunking` and wrap explicitly.
    """

    PROMPT = (
        "<document>\n{doc}\n</document>\n\n"
        "Here is a chunk from that document:\n<chunk>\n{chunk}\n</chunk>\n\n"
        "Give one short sentence situating this chunk within the document so it can be understood "
        "on its own in a search result. Output only that sentence."
    )

    def __init__(self, inner: BaseChunker, llm: Any = None, doc_char_limit: int = 40_000):
        self.inner = inner
        self.llm = llm
        self.doc_char_limit = doc_char_limit
        self.name = f"contextual_{inner.name}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        """Synchronous path returns un-enriched chunks; use `asplit` to actually contextualise."""
        return self.inner.split(doc)

    async def asplit(self, doc: ParsedDocument, max_concurrent: int = 8) -> list[Chunk]:
        import asyncio

        from sentinel.core.registry import get_provider

        chunks = self.inner.split(doc)
        llm = self.llm or get_provider("llm").cheap
        document = doc.markdown[: self.doc_char_limit]
        semaphore = asyncio.Semaphore(max_concurrent)

        async def enrich(chunk: Chunk) -> None:
            async with semaphore:
                try:
                    response = await llm.ainvoke(self.PROMPT.format(doc=document, chunk=chunk.embed_text[:2000]))
                    situating = (response.content or "").strip()
                    if situating:
                        chunk.embed_text = f"{situating}\n\n{chunk.embed_text}"
                        chunk.meta["situating_context"] = situating
                except Exception:
                    # Failing to contextualise one chunk must not fail ingestion; the chunk is
                    # still usable, just less retrievable.
                    pass

        await asyncio.gather(*(enrich(c) for c in chunks))
        return self._finalise(doc, chunks)


# Document type drives strategy. This mapping is the practical answer to "how do you chunk?" —
# not one strategy, but a routing table justified per content type.
DOC_TYPE_STRATEGY = {
    "runbook": "runbook",
    "glossary": "glossary",
    "methodology": "structural",
    "incident": "structural",
    "factsheet": "structural",
    "adr": "structural",
    "page": "structural",
}


def chunker_for_doc(doc: ParsedDocument, override: str | None = None) -> BaseChunker:
    from sentinel.chunking.base import get_chunker

    if override:
        return get_chunker(override)
    return get_chunker(DOC_TYPE_STRATEGY.get(doc.doc_type, "structural"))
