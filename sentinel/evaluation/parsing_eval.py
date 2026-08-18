"""Parser evaluation against hand-labelled ground truth.

`parsing/quality.py` scores a parse without a reference, for online routing. This module is the
offline counterpart: it compares against ground truth to *choose* a provider, and it is what
produces the numbers in `docs/STAGE-OVERVIEW.md`.

Three dimensions, because parsers fail in three different ways:

* **Text fidelity** — did the characters survive? Measured by normalised edit similarity.
* **Table accuracy** — did the grid survive? Measured by cell-level F1, which is a simplified TEDS
  (Tree Edit Distance based Similarity, the standard table-recognition metric).
* **Reading order** — did the sequence survive? Measured by Kendall's tau on block positions.
  This is the dimension that silently destroys multi-column PDFs: every character is correct and
  the text is unreadable because two columns were interleaved line by line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sentinel.core.types import BlockType, ParsedDocument


@dataclass
class ParseScore:
    text_similarity: float = 0.0
    table_f1: float = 0.0
    reading_order_tau: float = 0.0
    heading_recall: float = 0.0
    overall: float = 0.0
    detail: dict = field(default_factory=dict)


def _normalise(text: str) -> str:
    """Collapses whitespace and case so scoring measures content, not formatting."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def text_similarity(extracted: str, expected: str) -> float:
    """Normalised similarity via difflib. 1.0 means identical after normalisation."""
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _normalise(extracted), _normalise(expected)).ratio()


def table_cell_f1(extracted: list[list[list[str]]], expected: list[list[list[str]]]) -> float:
    """Cell-level F1 across all tables in a document.

    Each cell is keyed by (table index, row, column, normalised text), so a cell that is correct
    but in the wrong position counts as both a false positive and a false negative — which is the
    behaviour you want, because a misplaced value is not a partial success.
    """

    def cells(tables: list[list[list[str]]]) -> set[tuple]:
        out = set()
        for t, table in enumerate(tables):
            for r, row in enumerate(table):
                for c, value in enumerate(row):
                    normalised = _normalise(value)
                    if normalised:
                        out.add((t, r, c, normalised))
        return out

    got, want = cells(extracted), cells(expected)
    if not want:
        return 1.0 if not got else 0.0
    if not got:
        return 0.0

    true_positives = len(got & want)
    precision = true_positives / len(got)
    recall = true_positives / len(want)
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def reading_order_tau(extracted_blocks: list[str], expected_blocks: list[str]) -> float:
    """Kendall's tau between extracted and expected block order.

    Blocks are matched by fuzzy text similarity first, then their positions correlated. A tau near
    1 means order preserved; near 0 or negative means scrambled — the classic multi-column failure.
    """
    from difflib import SequenceMatcher

    if len(extracted_blocks) < 2 or len(expected_blocks) < 2:
        return 1.0

    positions: list[tuple[int, int]] = []
    for i, extracted in enumerate(extracted_blocks):
        best_j, best_score = -1, 0.0
        norm_extracted = _normalise(extracted)[:200]
        for j, expected in enumerate(expected_blocks):
            score = SequenceMatcher(None, norm_extracted, _normalise(expected)[:200]).ratio()
            if score > best_score:
                best_j, best_score = j, score
        if best_score > 0.6:
            positions.append((i, best_j))

    if len(positions) < 2:
        return 0.0

    concordant = discordant = 0
    for a in range(len(positions)):
        for b in range(a + 1, len(positions)):
            (i1, j1), (i2, j2) = positions[a], positions[b]
            if (i2 - i1) * (j2 - j1) > 0:
                concordant += 1
            elif (i2 - i1) * (j2 - j1) < 0:
                discordant += 1

    total = concordant + discordant
    return (concordant - discordant) / total if total else 0.0


def score_against_truth(doc: ParsedDocument, truth: dict) -> ParseScore:
    """Scores one parse against a ground-truth record.

    The ground-truth JSON format (see `data/ground_truth/README.md`):
        {"text": "...", "tables": [[[cell, ...], ...], ...], "blocks": ["...", ...],
         "headings": ["...", ...]}
    """
    score = ParseScore()

    if truth.get("text"):
        score.text_similarity = text_similarity(doc.text, truth["text"])

    extracted_tables = [b.table.cells for b in doc.blocks if b.type is BlockType.TABLE and b.table]
    score.table_f1 = table_cell_f1(extracted_tables, truth.get("tables", []))

    if truth.get("blocks"):
        score.reading_order_tau = reading_order_tau([b.text for b in doc.blocks if b.text], truth["blocks"])

    expected_headings = {_normalise(h) for h in truth.get("headings", [])}
    if expected_headings:
        found = {_normalise(b.text) for b in doc.blocks if b.type is BlockType.HEADING}
        score.heading_recall = len(found & expected_headings) / len(expected_headings)

    # Weighted toward text and tables because those are the failures that make a document useless;
    # reading order and headings degrade retrieval quality rather than destroying content.
    score.overall = round(
        score.text_similarity * 0.40
        + score.table_f1 * 0.30
        + score.reading_order_tau * 0.20
        + score.heading_recall * 0.10,
        4,
    )
    score.detail = {
        "provider": doc.metrics.provider,
        "pages": doc.metrics.pages,
        "duration_s": doc.metrics.duration_s,
        "usd_cost": doc.metrics.usd_cost,
        "tables_found": len(extracted_tables),
        "tables_expected": len(truth.get("tables", [])),
    }
    return score


def load_ground_truth(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
