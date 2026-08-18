"""Parsing and chunking behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.chunking.base import get_chunker
from sentinel.core.types import Block, BlockType, ParsedDocument, ParseMetrics, Table
from sentinel.parsing.quality import score_parse

MARKDOWN = """# ICE Operations

## Restart Procedure

Drain the input queue before restarting. Wait for in-flight calculations to complete.

### Rolling Restart

Restart one region at a time. Never restart all regions during market hours.

## Thresholds

| metric | warning | critical |
| --- | --- | --- |
| feed lag | 90s | 300s |
"""


@pytest.fixture
def markdown_doc(tmp_path: Path) -> ParsedDocument:
    from sentinel.parsing.local_parser import LocalParser

    path = tmp_path / "ice_operations.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    return LocalParser().parse(path, doc_type="runbook")


def test_markdown_headings_are_extracted(markdown_doc):
    headings = [b for b in markdown_doc.blocks if b.type is BlockType.HEADING]
    assert [h.text for h in headings] == [
        "ICE Operations",
        "Restart Procedure",
        "Rolling Restart",
        "Thresholds",
    ]
    assert headings[0].level == 1
    assert headings[2].level == 3


def test_structural_chunker_prefixes_heading_path(markdown_doc):
    chunks = get_chunker("structural").split(markdown_doc)
    nested = [c for c in chunks if "Rolling Restart" in c.heading_path]

    assert nested, "expected chunks under the nested heading"
    # The prefix is what turns an unretrievable fragment into a findable one.
    assert "Rolling Restart" in nested[0].embed_text
    assert "ICE Operations" in nested[0].embed_text


def test_runbook_chunker_returns_whole_procedure(markdown_doc):
    """Steps are embedded individually but the full procedure is returned.

    A retrieved half-runbook is worse than none, because someone under incident pressure will
    execute the half.
    """
    chunks = get_chunker("runbook").split(markdown_doc)
    assert len(chunks) > 1
    assert all(c.context_text == chunks[0].context_text for c in chunks)
    assert len({c.embed_text for c in chunks}) > 1


def test_quality_score_flags_empty_extraction():
    doc = ParsedDocument(
        doc_id="x",
        source_uri="x",
        title="x",
        blocks=[],
        metrics=ParseMetrics(provider="local", duration_s=0.1, pages=10),
    )
    report = score_parse(doc)
    assert report.score < 0.3
    assert report.needs_escalation


def test_quality_score_flags_garbled_text():
    """Text with no common English words scores badly on word validity."""
    doc = ParsedDocument(
        doc_id="x",
        source_uri="x",
        title="x",
        blocks=[Block(BlockType.PARAGRAPH, "xkqz mnbv qwrt zxcv " * 60)],
        metrics=ParseMetrics(provider="local", duration_s=0.1, pages=1),
    )
    report = score_parse(doc)
    assert report.word_validity < 0.5


def test_single_column_table_is_flagged():
    table = Table(rows=3, cols=1, cells=[["a"], ["b"], ["c"]], markdown="")
    doc = ParsedDocument(
        doc_id="x",
        source_uri="x",
        title="x",
        blocks=[Block(BlockType.TABLE, "a\nb\nc", table=table)],
        metrics=ParseMetrics(provider="local", duration_s=0.1, pages=1),
    )
    report = score_parse(doc)
    assert report.table_score == 0.0
    assert any("single column" in w for w in report.warnings)


def test_table_markdown_repeats_header():
    table = Table(rows=2, cols=2, cells=[["metric", "value"], ["lag", "90s"]], markdown="")
    rendered = table.to_markdown()
    assert rendered.splitlines()[0].startswith("| metric | value |")
    assert "---" in rendered.splitlines()[1]


def test_chunk_embed_and_context_can_differ(markdown_doc):
    """Sentence-window embeds one sentence and returns its neighbours."""
    chunks = get_chunker("sentence_window").split(markdown_doc)
    assert any(c.embed_text != c.context_text for c in chunks)
