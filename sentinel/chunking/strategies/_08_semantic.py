"""`semantic` — cut where meaning shifts, not where the token count runs out.

Eighth in build order, and the one strategy that embeds at chunk-decision time. Expensive: you
embed the whole corpus once just to place boundaries, then embed the chunks again. Benchmark it
before adopting it — the interesting column is usually cost, not recall.
"""

from __future__ import annotations

from sentinel.chunking.base import BaseChunker, ntokens, register
from sentinel.chunking.strategies._00_shared import split_sentences
from sentinel.chunking.strategies._02_recursive import RecursiveChunker
from sentinel.chunking.strategies._03_structural import StructuralChunker
from sentinel.core.types import Chunk, ParsedDocument


@register("semantic")
class SemanticChunker(BaseChunker):
    """Cuts where meaning shifts rather than where the token count runs out.

    Embeds every sentence, measures the cosine distance between consecutive sentences, and splits
    at distance peaks above a percentile threshold.

    Honest assessment, which is the useful thing to be able to say about it: it is **expensive** —
    you embed the whole corpus once at ingest just to decide boundaries, then embed the chunks
    again — and in published comparisons it frequently fails to beat recursive splitting by enough
    to justify that. Benchmark it on your corpus before adopting it; the interesting result is
    usually the cost column, not the recall column.
    """

    def __init__(self, percentile: int = 92, buffer: int = 1, max_size: int = 900):
        self.percentile = percentile
        self.buffer = buffer
        self.max_size = max_size
        self.name = f"semantic_p{percentile}"

    def split(self, doc: ParsedDocument) -> list[Chunk]:
        import numpy as np

        from sentinel.core.registry import get_provider

        sentences = split_sentences(doc.markdown)
        if len(sentences) < 5:
            return StructuralChunker().split(doc)

        # Each sentence is embedded with its neighbours so one short sentence does not register as
        # a topic shift on its own.
        windows = [" ".join(sentences[max(0, i - self.buffer) : i + self.buffer + 1]) for i in range(len(sentences))]

        embedder = get_provider("embedder")
        vectors = np.asarray(embedder.embed_documents(windows), dtype="float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)

        distances = 1.0 - np.sum(vectors[:-1] * vectors[1:], axis=1)
        threshold = float(np.percentile(distances, self.percentile))
        breakpoints = [i + 1 for i, d in enumerate(distances) if d > threshold]

        chunks: list[Chunk] = []
        start, seq = 0, 0
        for end in [*breakpoints, len(sentences)]:
            body = " ".join(sentences[start:end]).strip()
            start = end
            if not body:
                continue
            # A semantically coherent run can still exceed the budget; fall back to recursive.
            pieces = (
                [body]
                if ntokens(body) <= self.max_size
                else RecursiveChunker(self.max_size, 0)._recursive_split(body, RecursiveChunker.SEPARATORS)
            )
            for piece in pieces:
                chunks.append(
                    Chunk(
                        chunk_id=Chunk.make_id(doc.doc_id, seq, piece),
                        doc_id=doc.doc_id,
                        embed_text=f"{doc.title}\n{piece}",
                        context_text=piece,
                        parent_id=doc.doc_id,
                        meta={"threshold": round(threshold, 4)},
                    )
                )
                seq += 1

        return self._finalise(doc, chunks)
