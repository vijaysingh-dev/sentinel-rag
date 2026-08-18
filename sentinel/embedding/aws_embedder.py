"""Amazon Bedrock embeddings.

Two model families with meaningfully different interfaces, which is the main thing to know when
comparing AWS to Azure here:

**Titan Embeddings V2** (`amazon.titan-embed-text-v2:0`) takes one text per call and supports
`dimensions` of 1024, 512 or 256 plus a `normalize` flag. There is **no batch endpoint** — you
issue one InvokeModel call per text, so throughput comes from client-side concurrency rather than
from batching. That is a real operational difference from Azure OpenAI, which accepts hundreds of
inputs per request.

**Cohere Embed V3** (`cohere.embed-english-v3`) does accept batches (up to 96 texts) and requires
an explicit `input_type`: `search_document` when indexing, `search_query` when querying. Omitting
it, or using the same value for both, is the asymmetry bug from the base-class docstring in its
most concrete form — the model was trained to place queries and passages differently and will
quietly underperform if you do not tell it which is which.

This adapter handles both, because which one you pick changes the shape of your ingestion code.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log, record_tokens
from sentinel.core.resilience import RateLimited, with_retry
from sentinel.embedding.base import BaseEmbedder

TITAN_DIMS = {1024, 512, 256}
COHERE_BATCH_LIMIT = 96


class BedrockEmbedder(BaseEmbedder):
    name = "aws"
    max_input_tokens = 8192

    def __init__(
        self,
        model: str | None = None,
        dims: int | None = None,
        region: str | None = None,
        use_cache: bool = True,
        batch_size: int = 32,
        max_workers: int = 8,
    ):
        """
        Args:
            max_workers: Client-side concurrency for Titan, which has no batch endpoint. Raising
                this is how you get throughput; raising it too far earns throttling exceptions,
                which the retry policy absorbs but at the cost of latency.
        """
        import boto3

        self.model = model or settings.bedrock_embedding_model
        self.region = region or settings.aws_region
        self.max_workers = max_workers
        self.is_cohere = self.model.startswith("cohere.")
        self.is_titan = self.model.startswith("amazon.titan")

        requested = dims or settings.bedrock_embedding_dims
        if self.is_titan:
            self.supports_dimension_reduction = True
            self.dims = requested if requested in TITAN_DIMS else 1024
        else:
            self.dims = 1024  # Cohere v3 English is fixed at 1024

        session = boto3.Session(profile_name=settings.aws_profile or None, region_name=self.region)
        self._client = session.client("bedrock-runtime")

        # Cohere batches; Titan cannot, so its effective batch size is one per call.
        effective_batch = min(batch_size, COHERE_BATCH_LIMIT) if self.is_cohere else batch_size
        super().__init__(use_cache=use_cache, batch_size=effective_batch)
        log.info(
            "embedder_ready",
            provider=self.name,
            model=self.model,
            dims=self.dims,
            region=self.region,
            family="cohere" if self.is_cohere else "titan",
        )

    def _embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        if self.is_cohere:
            return self._embed_cohere(texts, is_query)
        return self._embed_titan_concurrent(texts)

    # ------------------------------------------------------------------------- Cohere
    def _embed_cohere(self, texts: list[str], is_query: bool) -> list[list[float]]:
        """One call for the whole batch, with the input_type the model was trained to expect."""
        from botocore.exceptions import ClientError

        body = json.dumps(
            {
                "texts": texts,
                "input_type": "search_query" if is_query else "search_document",
                "truncate": "END",
            }
        )
        try:
            response = self._client.invoke_model(modelId=self.model, body=body)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ThrottlingException":
                raise RateLimited(str(exc)) from exc
            raise

        payload = json.loads(response["body"].read())
        return payload["embeddings"]

    # -------------------------------------------------------------------------- Titan
    def _embed_titan_one(self, text: str) -> list[float]:
        from botocore.exceptions import ClientError

        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self.dims,
                "normalize": True,  # unit vectors so cosine and dot product agree
            }
        )
        try:
            response = self._client.invoke_model(modelId=self.model, body=body)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"ThrottlingException", "TooManyRequestsException"}:
                raise RateLimited(str(exc)) from exc
            raise

        payload = json.loads(response["body"].read())
        if payload.get("inputTextTokenCount"):
            record_tokens(self.model, payload["inputTextTokenCount"], 0, stage="embedding")
        return payload["embedding"]

    def _embed_titan_concurrent(self, texts: list[str]) -> list[list[float]]:
        """Fans out single-text calls, preserving input order.

        `executor.map` is used rather than `as_completed` precisely because order must be preserved
        — the caller zips these vectors back against the chunks that produced them.
        """
        if len(texts) == 1:
            return [self._embed_titan_one(texts[0])]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(self._embed_titan_one, texts))

    async def _aembed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        import asyncio

        async def call() -> list[list[float]]:
            return await asyncio.to_thread(self._embed_batch, texts, is_query)

        return await with_retry(call, attempts=4, operation="bedrock_embed")

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "region": self.region,
            "family": "cohere" if self.is_cohere else "titan",
            "batch_capable": self.is_cohere,
        }
