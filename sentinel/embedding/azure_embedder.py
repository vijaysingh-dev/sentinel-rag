"""Azure OpenAI embeddings.

The `text-embedding-3-*` family supports **Matryoshka truncation**: the models are trained so the
first N dimensions are usable on their own. Passing `dimensions=512` to a 3072-dimension model
returns a shorter vector that retains most of the quality, which is a real lever — index size and
search latency both scale with dimensionality, and storage is often the binding constraint before
quality is.

The classic two-stage pattern this enables: search on truncated vectors for a wide candidate set,
then rescore the top candidates with full-precision vectors. `benchmarks/embedding_bakeoff.py`
measures whether that trade is worth it on a given corpus.

Note the deployment/model distinction, which trips people up: on Azure you call a *deployment name*
you chose, not the model id. Two deployments of the same model with different quota are ordinary,
and the deployment name is what goes in config.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log, record_tokens
from sentinel.core.resilience import RateLimited, with_retry
from sentinel.embedding.base import BaseEmbedder

MODEL_DIMS = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
}
MATRYOSHKA_CAPABLE = {"text-embedding-3-large", "text-embedding-3-small"}


class AzureOpenAIEmbedder(BaseEmbedder):
    name = "azure"
    max_input_tokens = 8191  # generous; chunk size is the binding constraint, not this

    def __init__(
        self,
        deployment: str | None = None,
        dims: int | None = None,
        use_managed_identity: bool = True,
        use_cache: bool = True,
        batch_size: int = 128,  # the API accepts large batches; fewer round trips
    ):
        from openai import AzureOpenAI

        self.deployment = deployment or settings.azure_embedding_deployment
        self.model = self.deployment
        base_dims = MODEL_DIMS.get(self.deployment, settings.azure_embedding_dims)

        # Only request dimension reduction when the model actually supports it; ada-002 rejects
        # the parameter outright rather than ignoring it.
        self.supports_dimension_reduction = any(m in self.deployment for m in MATRYOSHKA_CAPABLE)
        self.dims = dims if (dims and self.supports_dimension_reduction) else base_dims
        self._request_dims = dims if (dims and self.supports_dimension_reduction) else None

        if use_managed_identity and not settings.azure_openai_api_key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
            self._client = AzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                azure_ad_token_provider=token_provider,
            )
        else:
            self._client = AzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                api_version=settings.azure_openai_api_version,
            )

        super().__init__(use_cache=use_cache, batch_size=batch_size)
        log.info(
            "embedder_ready",
            provider=self.name,
            deployment=self.deployment,
            dims=self.dims,
            truncated=bool(self._request_dims),
        )

    def _embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        from openai import RateLimitError

        kwargs: dict[str, Any] = {"model": self.deployment, "input": texts}
        if self._request_dims:
            kwargs["dimensions"] = self._request_dims

        try:
            response = self._client.embeddings.create(**kwargs)
        except RateLimitError as exc:
            # Surfaced as our own type so the shared retry policy handles it uniformly across
            # providers rather than each adapter inventing its own backoff.
            raise RateLimited(str(exc)) from exc

        if response.usage:
            record_tokens(self.deployment, response.usage.prompt_tokens, 0, stage="embedding")

        # The API does not guarantee response order matches input order; sort by index.
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    async def _aembed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        import asyncio

        async def call() -> list[list[float]]:
            return await asyncio.to_thread(self._embed_batch, texts, is_query)

        return await with_retry(call, attempts=4, operation="azure_embed")

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "deployment": self.deployment, "matryoshka": bool(self._request_dims)}
