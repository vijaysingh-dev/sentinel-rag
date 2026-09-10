"""`fixed` — the control group.

First in build order: it ignores every structural signal in the document, which is exactly what
makes it the baseline. Any strategy that cannot beat this on your corpus is not earning its
complexity.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, register
from sentinel.core.types import Chunk, ParsedDocument


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
