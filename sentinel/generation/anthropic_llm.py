"""Anthropic chat models (api.anthropic.com) — one of three inference vendors.

Same two-tier shape as every other `BaseLLM` adapter (`chat` for final answers and verification,
`cheap` for routing/rewriting/high-volume calls) via `langchain-anthropic`'s `ChatAnthropic`, so
the rest of the codebase — LangGraph agents, `with_structured_output`, tool binding — works
identically regardless of which of the three inference vendors is selected.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sentinel.core.config import settings
from sentinel.generation.base import CHEAP_MAX_TOKENS, BaseLLM


class AnthropicLLM(BaseLLM):
    name = "anthropic"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._log_ready(chat=settings.anthropic_chat_model, cheap=settings.anthropic_cheap_model)

    def _build(self, model: str, **kwargs: Any) -> Any:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=0,
            timeout=self.timeout,
            max_retries=0,
            **kwargs,
        )

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.anthropic_chat_model, max_tokens=1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.anthropic_cheap_model, max_tokens=CHEAP_MAX_TOKENS)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "chat": settings.anthropic_chat_model, "cheap": settings.anthropic_cheap_model}
