"""`structural` — the recommended default.

Third in build order. Splits along the document's own heading hierarchy, runs `recursive` inside
each section, and prefixes the heading path onto every chunk. `_sections` is reused by every
strategy below it, so — with `recursive` — this is the shared core of the package.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, register, truncate_tokens
from sentinel.chunking.strategies._02_recursive import RecursiveChunker
from sentinel.core.types import Block, BlockType, Chunk, ParsedDocument


@register("structural")
class StructuralChunker(BaseChunker):
    """Splits along the document's own heading hierarchy and prefixes the heading path.

    This is the recommended default, and the heading prefix is the reason.

    A chunk reading *"Set this to false to skip validation"* is unretrievable — no realistic query
    lands near it in embedding space. The same chunk embedded as *"Checkout Configuration >
    Validation > strict_mode — Set this to false to skip validation"* is findable by dozens of
    queries. The
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
