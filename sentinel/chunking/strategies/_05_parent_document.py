"""`parent_document` — embed children, return the parent section.

Fifth in build order. Requires deduplication by `parent_id` at retrieval time — five matching
children of one parent otherwise pull in the identical parent text five times.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, register, truncate_tokens
from sentinel.chunking.strategies._02_recursive import RecursiveChunker
from sentinel.chunking.strategies._03_structural import StructuralChunker
from sentinel.core.types import Chunk, ParsedDocument


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
