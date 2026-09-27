"""Reranker interface.

The distinction that justifies this whole stage, and the one interviewers probe:

* A **bi-encoder** (your embedding model) encodes query and document *separately* into vectors and
  compares them. Documents can be embedded offline, so it scales to millions — but the model never
  sees the query and the document together.
* A **cross-encoder** (the reranker) takes the pair as one concatenated input and outputs a single
  relevance score. Attention runs across both, so it catches negation, qualifiers and subtle
  mismatch that cosine similarity cannot. It cannot be precomputed, so it only runs over a small
  candidate set.

Hence two-stage retrieval: a cheap wide arm for recall, an expensive narrow arm for precision.

The corollary worth stating: **a reranker cannot recover a recall miss.** If the answer is not in
the candidate pool, no amount of reordering will surface it. Measure recall@50 and recall@5
separately — the gap tells you which stage to invest in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sentinel.core.observability import stage_timer
from sentinel.core.types import RetrievedChunk


class BaseReranker(ABC):
    name: str = "base"
    model: str = ""
    # Rerankers disagree wildly on score scale: local cross-encoders emit unbounded logits, Cohere
    # emits 0-1, Azure's semantic ranker emits roughly 0-4. A relevance floor must therefore be
    # calibrated per reranker, never copied between them.
    default_floor: float = 0.0

    @abstractmethod
    def _score(self, query: str, texts: list[str]) -> list[float]:
        """Returns one relevance score per candidate, in input order."""

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        top_n: int = 5,
        floor: float | None = None,
    ) -> list[RetrievedChunk]:
        """Scores, filters below the floor, and returns the best `top_n`.

        The floor is what lets the system say "I don't have anything relevant" instead of
        answering from the five least-bad chunks it found. In an operations assistant that
        distinction is the difference between a useful refusal and someone running the wrong
        runbook.
        """
        if not candidates:
            return []

        floor = self.default_floor if floor is None else floor

        with stage_timer("rerank", self.name, candidates=len(candidates)) as extra:
            # Reranking scores the text the generator will see, not the embedding text: that is
            # what the user's question will actually be answered from.
            scores = self._score(query, [c.chunk.context_text for c in candidates])
            extra["scored"] = len(scores)

        ranked = sorted(zip(candidates, scores, strict=True), key=lambda pair: -pair[1])
        out: list[RetrievedChunk] = []
        for rank, (item, score) in enumerate(ranked[:top_n]):
            if score < floor:
                continue
            item.rerank_score = float(score)
            item.score = float(score)
            item.rank = rank
            item.retriever = f"{item.retriever}+{self.name}"
            out.append(item)

        return out

    async def arerank(
        self, query: str, candidates: list[RetrievedChunk], top_n: int = 5, floor: float | None = None
    ) -> list[RetrievedChunk]:
        import asyncio

        return await asyncio.to_thread(self.rerank, query, candidates, top_n, floor)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "default_floor": self.default_floor}
