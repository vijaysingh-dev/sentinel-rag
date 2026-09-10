"""`glossary` — one term per chunk.

Seventh in build order. Glossary entries are atomic; merging two into one chunk makes both harder
to find, because the vector becomes an average of two unrelated concepts.
"""

from __future__ import annotations

import re

from sentinel.chunking.base import BaseChunker, register
from sentinel.chunking.strategies._03_structural import StructuralChunker
from sentinel.core.types import Chunk, ParsedDocument


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
