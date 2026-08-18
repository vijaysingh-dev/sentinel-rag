"""Local models through an OpenAI-compatible endpoint (Ollama, vLLM, LM Studio).

Any of these expose `/v1/chat/completions`, so `ChatOpenAI` pointed at a local base URL works
unchanged. That is the whole trick, and it is what lets the entire pipeline run with no cloud
account at all — which matters for development, for tests, and for corpora that cannot leave the
building.

Caveat worth knowing: small local models are noticeably weaker at *tool calling* and structured
output than the hosted frontier models. The agent graph will run, but expect more schema repair
rounds and worse routing accuracy. Benchmark before assuming parity.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sentinel.core.config import settings
from sentinel.generation.base import BaseLLM


class LocalLLM(BaseLLM):
    name = "local"

    def __init__(self, base_url: str | None = None, timeout: int = 120):
        self.base_url = base_url or settings.local_llm_base_url
        self.timeout = timeout  # local inference is slower; a tighter timeout just fails
        self._log_ready(chat=settings.local_llm_model, base_url=self.base_url)

    def _build(self, model: str, max_tokens: int) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            base_url=self.base_url,
            api_key="not-needed",  # local servers ignore it but the client requires a value
            temperature=0,
            max_tokens=max_tokens,
            timeout=self.timeout,
            max_retries=0,
        )

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.local_llm_model, 1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.local_llm_cheap_model, 800)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "chat": settings.local_llm_model,
            "cheap": settings.local_llm_cheap_model,
            "base_url": self.base_url,
        }
