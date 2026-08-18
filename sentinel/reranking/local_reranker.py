"""Local cross-encoder reranking via sentence-transformers.

Free, private, and fast enough: a MiniLM cross-encoder scores roughly 50 pairs in 50-150ms on CPU
and single-digit milliseconds on GPU. For most systems this is the highest quality-per-cost
addition available — it is the change to make first if you can only make one.

Model notes:
* `cross-encoder/ms-marco-MiniLM-L-6-v2` — the standard baseline. Emits unbounded logits, so a
  relevance floor must be calibrated rather than assumed; roughly 0 is the usual decision boundary.
* `BAAI/bge-reranker-base` / `-large` — stronger, slower, and outputs are sigmoid-friendly.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.reranking.base import BaseReranker


class CrossEncoderReranker(BaseReranker):
    name = "local"
    default_floor = -2.0  # ms-marco logits; calibrate on your own golden set

    def __init__(
        self, model: str | None = None, device: str | None = None, batch_size: int = 32, max_length: int = 512
    ):
        from sentence_transformers import CrossEncoder

        self.model = model or settings.local_rerank_model
        self.batch_size = batch_size
        self._encoder = CrossEncoder(self.model, device=device, max_length=max_length)
        log.info("reranker_ready", provider=self.name, model=self.model)

    def _score(self, query: str, texts: list[str]) -> list[float]:
        pairs = [(query, text) for text in texts]
        return [float(s) for s in self._encoder.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)]

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "device": str(self._encoder.model.device)}
