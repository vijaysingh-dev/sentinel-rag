"""Amazon Bedrock chat models via the Converse API.

`ChatBedrockConverse` rather than the older `ChatBedrock` because the Converse API normalises
tool-calling and multimodal input across model families — without it, Anthropic, Meta and Amazon
models each want a different request shape, and the agent code would need to know which is which.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sentinel.core.config import settings
from sentinel.generation.base import BaseLLM


class BedrockLLM(BaseLLM):
    name = "aws"

    def __init__(self, region: str | None = None, timeout: int = 60):
        self.region = region or settings.aws_region
        self.timeout = timeout
        self._log_ready(chat=settings.bedrock_chat_model, region=self.region)

    def _build(self, model_id: str, max_tokens: int) -> Any:
        from langchain_aws import ChatBedrockConverse

        return ChatBedrockConverse(
            model=model_id,
            region_name=self.region,
            credentials_profile_name=settings.aws_profile or None,
            temperature=0,
            max_tokens=max_tokens,
        )

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.bedrock_chat_model, 1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.bedrock_chat_cheap_model, 800)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "chat": settings.bedrock_chat_model,
            "cheap": settings.bedrock_chat_cheap_model,
            "region": self.region,
        }
