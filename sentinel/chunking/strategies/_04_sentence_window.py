"""`sentence_window` — the purest decoupling.

Fourth in build order: embed one sentence, return that sentence plus its neighbours. Only
expressible because `Chunk` carries two text fields.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, register
from sentinel.chunking.strategies._00_shared import split_sentences
from sentinel.chunking.strategies._03_structural import StructuralChunker
from sentinel.core.types import Chunk, ParsedDocument


@register("sentence_window")
class SentenceWindowChunker(BaseChunker):
    """Embeds one sentence, returns that sentence plus its neighbours.

    The purest expression of decoupling retrieval from generation: maximum precision in the vector
    (one claim per embedding) while the generator still receives readable surrounding context.

    Costs: 3-10x more vectors than paragraph chunking, and adjacent sentences produce overlapping
    windows, so the retriever must deduplicate before assembling a prompt or the context budget
    fills with repeated text.

    Best on dense factual documents — methodology, policy, specifications — where a single sentence
    is a complete fact.
    """

    def __init__(self, window: int = 3):
        self.window = window
        self.name = f"sentence_window_{window}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        seq = 0

        for heading_path, blocks in StructuralChunker._sections(doc):
            header = f"{doc.title} > {heading_path}" if heading_path else doc.title
            text = "\n".join(b.text for b in blocks if b.text.strip())
            sentences = split_sentences(text)

            for i, sentence in enumerate(sentences):
                lo = max(0, i - self.window)
                hi = min(len(sentences), i + self.window + 1)
                window_text = " ".join(sentences[lo:hi])
                chunks.append(
                    Chunk(
                        chunk_id=Chunk.make_id(doc.doc_id, seq, sentence),
                        doc_id=doc.doc_id,
                        embed_text=f"{header}\n{sentence}",
                        context_text=f"[{header}]\n{window_text}",
                        parent_id=f"{doc.doc_id}::{heading_path or 'root'}",
                        heading_path=heading_path,
                        meta={"sentence_index": i, "window": self.window},
                    )
                )
                seq += 1

        return self._finalise(doc, chunks)
