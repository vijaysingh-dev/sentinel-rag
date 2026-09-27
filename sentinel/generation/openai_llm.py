"""Plain OpenAI chat models (api.openai.com) — one of three inference vendors.

No deployment indirection, no managed identity: a model id goes straight into `model=`. This is
the direct-API twin of `embedding/openai_embedder.py`; see that module's docstring for why plain
OpenAI is a minimal escape hatch here rather than a maintained "cloud" path the way Azure OpenAI
used to be (ADR-029, `.archive/providers/azure_llm.py`).
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sentinel.core.config import settings
from sentinel.generation.base import CHEAP_MAX_TOKENS, BaseLLM


class OpenAILLM(BaseLLM):
    name = "openai"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._log_ready(chat=settings.openai_chat_model, cheap=settings.openai_cheap_model)

    def _build(self, model: str, **kwargs: Any) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=settings.openai_api_key,
            temperature=0,
            timeout=self.timeout,
            max_retries=0,
            **kwargs,
        )

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.openai_chat_model, max_tokens=1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.openai_cheap_model, max_tokens=CHEAP_MAX_TOKENS)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "chat": settings.openai_chat_model, "cheap": settings.openai_cheap_model}
