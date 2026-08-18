"""Azure OpenAI chat models."""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sentinel.core.config import settings
from sentinel.generation.base import BaseLLM


class AzureLLM(BaseLLM):
    name = "azure"

    def __init__(self, use_managed_identity: bool = True, timeout: int = 60):
        self.timeout = timeout
        self.use_managed_identity = use_managed_identity and not settings.azure_openai_api_key
        self._log_ready(chat=settings.azure_chat_deployment, cheap=settings.azure_chat_cheap_deployment)

    def _build(self, deployment: str, **kwargs: Any) -> Any:
        from langchain_openai import AzureChatOpenAI

        common: dict[str, Any] = {
            "azure_deployment": deployment,
            "azure_endpoint": settings.azure_openai_endpoint,
            "api_version": settings.azure_openai_api_version,
            "temperature": 0,
            "timeout": self.timeout,
            "max_retries": 0,
            **kwargs,
        }
        if self.use_managed_identity:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            common["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
        else:
            common["api_key"] = settings.azure_openai_api_key
        return AzureChatOpenAI(**common)

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.azure_chat_deployment, max_tokens=1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.azure_chat_cheap_deployment, max_tokens=800)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "chat": settings.azure_chat_deployment,
            "cheap": settings.azure_chat_cheap_deployment,
            "auth": "managed_identity" if self.use_managed_identity else "api_key",
        }
