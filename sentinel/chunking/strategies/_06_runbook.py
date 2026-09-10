"""`runbook` — embed steps, always return the whole procedure.

Sixth in build order, and the first doc-type-specific strategy: a retrieved half-runbook is worse
than no runbook, because someone under incident pressure will execute the half.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, register, truncate_tokens
from sentinel.chunking.strategies._00_shared import STEP_SPLIT
from sentinel.chunking.strategies._03_structural import StructuralChunker
from sentinel.core.types import Chunk, ParsedDocument


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
