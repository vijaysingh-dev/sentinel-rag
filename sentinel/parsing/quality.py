"""Heuristic quality scoring for a parse.

This module answers one question: **did the cheap local parser do well enough, or should we pay a
cloud service to try?** Without an answer you have only two bad options — pay per page for every
document including the ones a free library handled perfectly, or accept silent garbage on the
documents it did not.

The score is deliberately heuristic and reference-free, because at ingestion time there is no
ground truth to compare against. It is calibrated to be *conservative*: it should rarely call a bad
parse good. Calling a good parse bad merely costs a cloud call.

For true accuracy measurement against hand-labelled ground truth, see `evaluation/parsing_eval.py`;
that is the offline counterpart used to choose providers, whereas this is the online gate used to
route individual documents.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from sentinel.core.types import BlockType, ParsedDocument

# Words that appear in essentially any English technical document. Their absence from a long
# extraction is strong evidence that what came out is not readable prose — encoding damage,
# ligature loss, or column interleaving.
COMMON_WORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "this",
    "from",
    "are",
    "was",
    "not",
    "but",
    "have",
    "has",
    "will",
    "can",
    "which",
    "when",
    "all",
    "been",
    "would",
    "there",
}

# Repeated ALL-CAPS or numeric lines at page boundaries — running headers and footers that carry
# no information but dilute every chunk they land in.
HEADER_FOOTER = re.compile(r"^\s*(page\s+\d+|\d+\s*/\s*\d+|confidential|internal use only)\s*$", re.IGNORECASE)

# Control characters and replacement markers left by failed encoding conversions.
GARBAGE_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]")

# Words fused by lost spaces, e.g. "calculationengine" — a classic PDF extraction artefact.
SUSPICIOUS_RUN = re.compile(r"\b[a-z]{25,}\b")


@dataclass
class QualityReport:
    """Component scores plus the overall verdict.

    Components are kept separately rather than collapsed immediately because the *reason* a parse
    scored badly determines the right remedy: low `text_density` means OCR is needed, while low
    `structure_score` means the layout model failed and a different service may do better.
    """

    score: float
    text_density: float
    word_validity: float
    structure_score: float
    encoding_score: float
    table_score: float
    warnings: list[str] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def needs_escalation(self) -> bool:
        from sentinel.core.config import settings

        return self.score < settings.parse_quality_threshold


def _text_density(doc: ParsedDocument) -> tuple[float, list[str]]:
    """Characters per page, normalised. Near zero means an image-only document."""
    warnings: list[str] = []
    pages = max(doc.metrics.pages, 1)
    chars = sum(len(b.text) for b in doc.blocks)
    per_page = chars / pages

    if per_page < 50:
        warnings.append("almost no extractable text; likely scanned or image-only")
    elif per_page < 300:
        warnings.append("sparse text; possibly a slide deck or a partially scanned document")

    # 1500 chars/page is a comfortably full page of prose; anything above scores maximum.
    return min(per_page / 1500.0, 1.0), warnings


def _word_validity(text: str) -> tuple[float, list[str]]:
    """Fraction of tokens that look like real words, plus a common-word presence check."""
    warnings: list[str] = []
    tokens = re.findall(r"[A-Za-z]{2,}", text.lower())
    if len(tokens) < 20:
        return 0.5, ["too little text to judge word validity"]

    alpha_ratio = len([t for t in tokens if t.isalpha()]) / len(tokens)
    common_hits = len(COMMON_WORDS & set(tokens)) / len(COMMON_WORDS)

    if common_hits < 0.15:
        warnings.append("few common English words; text may be garbled or non-English")

    fused = len(SUSPICIOUS_RUN.findall(text))
    if fused > len(tokens) * 0.01:
        warnings.append(f"{fused} suspiciously long tokens; word spacing may be lost")

    penalty = min(fused / max(len(tokens), 1) * 10, 0.3)
    return max(0.0, (alpha_ratio * 0.4 + min(common_hits * 2, 1.0) * 0.6) - penalty), warnings


def _structure_score(doc: ParsedDocument) -> tuple[float, list[str]]:
    """Rewards a document that has headings and a plausible block mix.

    A parse that returns one enormous paragraph block has lost the structure chunkers depend on,
    even if every character is correct.
    """
    warnings: list[str] = []
    if not doc.blocks:
        return 0.0, ["no blocks extracted"]

    kinds = [b.type for b in doc.blocks]
    headings = kinds.count(BlockType.HEADING)
    paragraphs = kinds.count(BlockType.PARAGRAPH)

    if headings == 0 and len(doc.blocks) > 5:
        warnings.append("no headings detected; heading-aware chunking will not be available")

    sizes = [len(b.text) for b in doc.blocks if b.type is BlockType.PARAGRAPH]
    if sizes and statistics.mean(sizes) > 4000:
        warnings.append("very large paragraph blocks; layout segmentation may have failed")

    heading_ratio = headings / max(len(doc.blocks), 1)
    # A healthy document is roughly 5-25% headings; score peaks in that band.
    heading_component = (
        min(heading_ratio / 0.10, 1.0)
        if heading_ratio < 0.10
        else max(0.4, 1.0 - (heading_ratio - 0.25) * 2)
        if heading_ratio > 0.25
        else 1.0
    )
    variety = min(len(set(kinds)) / 4.0, 1.0)
    balance = 1.0 if paragraphs else 0.5

    return heading_component * 0.5 + variety * 0.3 + balance * 0.2, warnings


def _encoding_score(text: str) -> tuple[float, list[str]]:
    warnings: list[str] = []
    if not text:
        return 0.0, ["empty text"]

    garbage = len(GARBAGE_CHARS.findall(text))
    ratio = garbage / len(text)
    if ratio > 0.001:
        warnings.append(f"{garbage} control or replacement characters; encoding damage likely")

    header_lines = sum(1 for line in text.splitlines() if HEADER_FOOTER.match(line))
    if header_lines > 5:
        warnings.append(f"{header_lines} running header/footer lines not stripped")

    return max(0.0, 1.0 - ratio * 500 - min(header_lines / 100, 0.2)), warnings


def _table_score(doc: ParsedDocument) -> tuple[float, list[str]]:
    """Judges whether detected tables are structurally plausible.

    A table extracted as a single column, or with wildly ragged rows, is worse than no table: it
    produces chunks of orphaned values that retrieve on the numbers and mean nothing.
    """
    warnings: list[str] = []
    tables = [b.table for b in doc.blocks if b.type is BlockType.TABLE and b.table]
    if not tables:
        # Absence of tables is not itself a fault; neutral score so it does not skew the total.
        return 0.75, warnings

    good = 0
    for t in tables:
        if t.cols < 2:
            warnings.append("table with a single column; structure likely lost")
            continue
        if not t.cells or not any(any(c.strip() for c in row) for row in t.cells):
            warnings.append("empty table cells")
            continue
        widths = {len(row) for row in t.cells}
        if len(widths) > max(2, len(t.cells) // 3):
            warnings.append("ragged table rows; cell alignment may be wrong")
            continue
        good += 1

    return good / len(tables), warnings


def score_parse(doc: ParsedDocument) -> QualityReport:
    """Computes the overall quality score for a parsed document.

    Weights favour text density and word validity because those catch the catastrophic failures
    (scanned document, encoding damage) that make a document useless. Structure and tables are
    quality-of-life: they degrade retrieval rather than destroying it.
    """
    text = doc.text

    density, w1 = _text_density(doc)
    validity, w2 = _word_validity(text)
    structure, w3 = _structure_score(doc)
    encoding, w4 = _encoding_score(text)
    tables, w5 = _table_score(doc)

    score = density * 0.25 + validity * 0.30 + structure * 0.20 + encoding * 0.15 + tables * 0.10

    report = QualityReport(
        score=round(score, 4),
        text_density=round(density, 4),
        word_validity=round(validity, 4),
        structure_score=round(structure, 4),
        encoding_score=round(encoding, 4),
        table_score=round(tables, 4),
        warnings=[*w1, *w2, *w3, *w4, *w5],
        signals={
            "chars": len(text),
            "blocks": len(doc.blocks),
            "pages": doc.metrics.pages,
            "tables": doc.metrics.tables,
            "figures": doc.metrics.figures,
        },
    )
    doc.metrics.quality_score = report.score
    doc.metrics.warnings.extend(report.warnings)
    return report


def compare_parses(candidates: dict[str, ParsedDocument]) -> dict[str, QualityReport]:
    """Scores several parses of the same document, for provider bake-offs.

    Returns a mapping keyed by provider name so the caller can pick a winner, log the margin, and
    record both in the benchmark table.
    """
    return {name: score_parse(doc) for name, doc in candidates.items()}
