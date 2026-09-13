"""Local embeddings via sentence-transformers.

Zero marginal cost, no rate limits, no data leaving the machine — which is a genuine architectural
argument, not just a convenience: some corpora cannot be sent to a third-party API at all, and that
constraint alone can decide the embedding provider regardless of benchmark scores.

The trade is throughput. On CPU, expect roughly 50-200 chunks/second for a base-sized model; a
100k-chunk corpus is therefore tens of minutes rather than the few minutes an API would take.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.embedding.base import BaseEmbedder

# Models that were trained with an instruction prefix on the query side. Using them without the
# prefix costs several points of recall and produces no error, which makes it a genuinely nasty
# silent bug — hence the explicit table rather than a heuristic.
QUERY_PREFIXES = {
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "intfloat/e5-base-v2": "query: ",
    "intfloat/e5-large-v2": "query: ",
    "intfloat/multilingual-e5-base": "query: ",
}
DOC_PREFIXES = {
    "intfloat/e5-base-v2": "passage: ",
    "intfloat/e5-large-v2": "passage: ",
    "intfloat/multilingual-e5-base": "passage: ",
}

KNOWN_DIMS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "intfloat/e5-base-v2": 768,
    "intfloat/e5-large-v2": 1024,
}


class LocalEmbedder(BaseEmbedder):
    name = "local"

    def __init__(
        self,
        model: str | None = None,
        device: str | None = None,
        normalize: bool = True,
        use_cache: bool = True,
        batch_size: int = 64,
    ):
        """
        Args:
            normalize: Return unit-length vectors. Strongly recommended — with normalised vectors
                cosine, dot product and L2 all agree, and the length bias that makes long documents
                score higher under dot product disappears.
        """
        from sentence_transformers import SentenceTransformer

        self.model = model or settings.local_embedding_model
        self.normalize = normalize
        self._st = SentenceTransformer(self.model, device=device)

        dims = KNOWN_DIMS.get(self.model) or self._st.get_embedding_dimension()
        if dims is None:
            raise ValueError(f"Could not determine embedding dimension for model {self.model!r}")
        self.dims = dims
        self.max_input_tokens = min(self._st.max_seq_length or 512, 512)

        super().__init__(use_cache=use_cache, batch_size=batch_size)
        log.info(
            "embedder_ready",
            provider=self.name,
            model=self.model,
            dims=self.dims,
            max_tokens=self.max_input_tokens,
            device=str(self._st.device),
        )

    def _apply_prefix(self, text: str, is_query: bool) -> str:
        table = QUERY_PREFIXES if is_query else DOC_PREFIXES
        return table.get(self.model, "") + text

    def _embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        vectors = self._st.encode(
            texts,
            normalize_embeddings=self.normalize,
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vectors.tolist()

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "device": str(self._st.device), "normalized": self.normalize}
