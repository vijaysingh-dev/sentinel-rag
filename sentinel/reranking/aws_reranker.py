"""Amazon Bedrock reranking.

Bedrock exposes reranking through the `bedrock-agent-runtime` client's `rerank` API — a separate
service surface from `bedrock-runtime`, which is a common source of "method not found" confusion.

Two models at time of writing:
* `amazon.rerank-v1:0` — Amazon's own.
* `cohere.rerank-v3-5:0` — Cohere's, served through Bedrock.

Both return normalised relevance scores in [0,1], which makes the floor directly interpretable and
portable between them — unlike the local cross-encoder's raw logits.

The API caps sources per call (100 for Amazon's model), so long candidate lists are chunked and the
partial results merged. That detail matters if you retrieve wide: a 200-candidate pool is three
calls, not one.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log
from sentinel.core.resilience import RateLimited, with_retry
from sentinel.reranking.base import BaseReranker

MAX_SOURCES_PER_CALL = 100


class BedrockReranker(BaseReranker):
    name = "aws"
    default_floor = 0.02  # normalised [0,1]; well below this is noise

    def __init__(self, model: str | None = None, region: str | None = None):
        import boto3

        self.model = model or settings.bedrock_rerank_model
        self.region = region or settings.aws_region
        session = boto3.Session(profile_name=settings.aws_profile or None, region_name=self.region)
        # Note the client name: rerank lives on bedrock-agent-runtime, not bedrock-runtime.
        self._client = session.client("bedrock-agent-runtime")
        self._model_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{self.model}"
        log.info("reranker_ready", provider=self.name, model=self.model, region=self.region)

    def _score(self, query: str, texts: list[str]) -> list[float]:
        """Scores every candidate, chunking around the per-call source limit."""
        scores = [0.0] * len(texts)

        for start in range(0, len(texts), MAX_SOURCES_PER_CALL):
            window = texts[start : start + MAX_SOURCES_PER_CALL]
            for offset, score in self._rerank_call(query, window).items():
                scores[start + offset] = score

        return scores

    def _rerank_call(self, query: str, texts: list[str]) -> dict[int, float]:
        from botocore.exceptions import ClientError

        try:
            response = self._client.rerank(
                queries=[{"type": "TEXT", "textQuery": {"text": query}}],
                sources=[
                    {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": t}}}
                    for t in texts
                ],
                rerankingConfiguration={
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        # Ask for every candidate back; we apply our own top_n and floor.
                        "numberOfResults": len(texts),
                        "modelConfiguration": {"modelArn": self._model_arn},
                    },
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ThrottlingException":
                raise RateLimited(str(exc)) from exc
            raise

        # Results come back sorted by relevance with an `index` pointing at the input position.
        return {r["index"]: float(r["relevanceScore"]) for r in response.get("results", [])}

    async def arerank(self, query, candidates, top_n: int = 5, floor: float | None = None):
        import asyncio

        async def call():
            return await asyncio.to_thread(self.rerank, query, candidates, top_n, floor)

        return await with_retry(call, attempts=3, operation="bedrock_rerank")

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "region": self.region, "max_sources_per_call": MAX_SOURCES_PER_CALL}
