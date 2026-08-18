"""Domain types that flow between pipeline stages.

These types are the contract between stages. Parsing produces a `ParsedDocument`, chunking turns
it into `Chunk`s, embedding attaches vectors, retrieval returns `RetrievedChunk`s. Every adapter
(local / Azure / AWS) produces the *same* types, which is what makes the providers swappable and
what makes a cross-provider benchmark meaningful — you are comparing like with like.

Two modelling decisions are load-bearing and worth understanding before reading further:

1. `Chunk` carries **two** text fields. `embed_text` is what gets vectorised; `context_text` is
   what is handed to the LLM. They are usually the same but deliberately need not be: sentence
   window and parent-document strategies embed something small and precise while returning
   something larger and self-contained. A single-text-field chunk model cannot express those
   strategies at all.

2. `Block` keeps the document's *structure* (headings, tables, figures) rather than flattening
   everything to a string at parse time. Chunkers need that structure to avoid splitting a table
   away from its header or a procedure away from its title, and it is impossible to recover once
   discarded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal


class BlockType(StrEnum):
    """Structural role of a span of extracted content.

    Kept coarse on purpose. Finer taxonomies (footnote, caption, list item) look appealing but
    every parser reports them differently, so a coarse shared vocabulary is the only one that can
    be produced faithfully by pypdf, Document Intelligence and Textract alike.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FIGURE = "figure"
    LIST = "list"
    CODE = "code"
    KEY_VALUE = "key_value"  # form fields; Textract FORMS and DI key-value pairs land here
    FOOTER = "footer"
    HEADER = "header"


@dataclass
class Table:
    """A table preserved as a grid plus a markdown rendering.

    Both representations are kept because they serve different consumers: `markdown` is what goes
    into an LLM prompt, while `cells` survives round-tripping and is what table-accuracy scoring
    compares against ground truth.
    """

    rows: int
    cols: int
    cells: list[list[str]]
    markdown: str
    page: int | None = None
    caption: str | None = None
    confidence: float | None = None

    def to_markdown(self) -> str:
        """Renders the grid, treating row 0 as the header.

        Header repetition matters downstream: when a large table is split across chunks, each
        fragment must carry the header or the rows become meaningless numbers.
        """
        if not self.cells:
            return ""
        header = self.cells[0]
        body = self.cells[1:]
        out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        out.extend("| " + " | ".join(r) + " |" for r in body)
        return "\n".join(out)


@dataclass
class Figure:
    """An image region plus, once enriched, a text description of it.

    Diagrams are invisible to a text index until something describes them. `description` is filled
    in by the figure-enrichment step using a vision model; `image_uri` is retained so an answer can
    link back to the actual picture rather than only paraphrasing it.
    """

    page: int | None
    bbox: tuple[float, float, float, float] | None = None
    image_uri: str | None = None
    caption: str | None = None
    description: str | None = None


@dataclass
class Block:
    """One structural unit of a parsed document, in reading order."""

    type: BlockType
    text: str
    page: int | None = None
    level: int | None = None  # heading depth, 1-6
    table: Table | None = None
    figure: Figure | None = None
    confidence: float | None = None  # parser-reported, when available
    bbox: tuple[float, float, float, float] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseMetrics:
    """Cost and quality telemetry for a single parse.

    Recorded per parse rather than aggregated later because the interesting comparisons are
    per-document: a parser that is excellent on native PDFs and useless on scans has a good mean
    and a terrible distribution, and only per-document records reveal that.
    """

    provider: str
    duration_s: float
    pages: int = 0
    characters: int = 0
    tables: int = 0
    figures: int = 0
    usd_cost: float = 0.0
    quality_score: float | None = None  # populated by parsing.quality
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    """The output of every parser adapter, regardless of provider or file format."""

    doc_id: str
    source_uri: str
    title: str
    blocks: list[Block]
    metrics: ParseMetrics
    doc_type: str = "page"  # runbook | methodology | incident | factsheet | page
    tenant_id: str = "default"
    effective_date: str | None = field(default_factory=lambda: date.today().isoformat())
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def markdown(self) -> str:
        """Flattens blocks back into markdown for chunkers that prefer text over structure."""
        out: list[str] = []
        for b in self.blocks:
            if b.type is BlockType.HEADING:
                out.append(f"{'#' * (b.level or 2)} {b.text}")
            elif b.type is BlockType.TABLE and b.table:
                if b.table.caption:
                    out.append(f"**{b.table.caption}**")
                out.append(b.table.markdown or b.table.to_markdown())
            elif b.type is BlockType.FIGURE and b.figure:
                desc = b.figure.description or b.figure.caption or "figure"
                out.append(f"![figure]({b.figure.image_uri or ''})\n\n_{desc}_")
            else:
                out.append(b.text)
        return "\n\n".join(x for x in out if x and x.strip())

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text)


@dataclass
class Chunk:
    """A retrievable unit.

    `embed_text` and `context_text` differ whenever the retrieval unit and the generation unit
    should differ — see the module docstring. `parent_id` supports deduplication so five matching
    steps of one runbook do not return that runbook five times.
    """

    chunk_id: str
    doc_id: str
    embed_text: str
    context_text: str
    parent_id: str
    heading_path: str = ""
    # Empty rather than "page" so `_finalise` can tell "not set" from "deliberately generic" and
    # inherit the document's type. A truthy default silently swallows the inheritance.
    doc_type: str = ""
    tenant_id: str = "default"
    source_uri: str = ""
    page: int | None = None
    effective_date: str | None = None
    allowed_roles: list[str] = field(default_factory=lambda: ["admin"])
    components: list[str] = field(default_factory=list)
    strategy: str = ""
    token_count: int = 0
    content_hash: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_id(doc_id: str, seq: int, text: str) -> str:
        """Content-derived id so re-ingesting an unchanged document is a no-op.

        Position alone is not enough (inserting a paragraph would shift every later id and force a
        full re-embed); content alone is not enough (two identical boilerplate paragraphs would
        collide). The pair is stable under edits elsewhere in the document.
        """
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        return f"{doc_id}::{seq:04d}::{digest}"


@dataclass
class EmbeddedChunk:
    chunk: Chunk
    vector: list[float]
    model: str
    dims: int


@dataclass
class RetrievedChunk:
    """A chunk returned by retrieval, carrying every score that touched it.

    All three score slots are kept rather than collapsed into one because they answer different
    diagnostic questions: a high dense score with a low rerank score means the embedding was
    fooled by surface similarity, while a BM25-only hit means the query was lexical and the dense
    arm contributed nothing.
    """

    chunk: Chunk
    score: float
    dense_score: float | None = None
    sparse_score: float | None = None
    rerank_score: float | None = None
    rank: int = 0
    retriever: str = ""

    @property
    def ref(self) -> str:
        """Citation handle used by the RCA agents; must round-trip to a real chunk id."""
        return f"doc:{self.chunk.chunk_id}"


@dataclass
class GoldenExample:
    """One evaluation case.

    `role` exists because in a role-scoped system the same question has different correct answers
    for different callers, and `expected_behaviour` allows "refuse" to be the correct answer —
    without which you cannot measure hallucination on out-of-scope questions at all.
    """

    id: str
    question: str
    category: Literal[
        "terminology",
        "howto",
        "design_rationale",
        "lookup",
        "live_status",
        "mixed",
        "unanswerable",
        "acl_denied",
        "multi_hop",
    ]
    role: str = "admin"
    expected_behaviour: Literal["answer", "refuse", "escalate"] = "answer"
    ground_truth: str = ""
    relevant_chunk_ids: list[str] = field(default_factory=list)
    answer_substrings: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    """Uniform benchmark row.

    Every stage benchmark emits these so the leaderboard renderer, the CSV schema and the CI gate
    are written once instead of per stage.
    """

    stage: str
    provider: str
    config: dict[str, Any]
    metrics: dict[str, float]
    duration_s: float
    usd_cost: float = 0.0
    n_items: int = 0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    notes: str = ""
