"""`recursive` — respect boundaries when you can.

Second in build order. The pragmatic default of most RAG systems, and the shared engine of the
package: `_recursive_split` is called directly by `structural`, `parent_document` and `semantic`,
so this module has no siblings above it in the import graph.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, ntokens, register, truncate_tokens
from sentinel.core.types import Chunk, ParsedDocument


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
