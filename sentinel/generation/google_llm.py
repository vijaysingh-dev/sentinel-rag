"""Google chat models (Gemini, via the Gemini API key — not Vertex AI) — one of three inference
vendors.

Same two-tier shape as every other `BaseLLM` adapter via `langchain-google-genai`'s
`ChatGoogleGenerativeAI`. Deliberately the plain Gemini API (one `google_api_key`, no GCP project
or service account) rather than Vertex AI, which is a materially different auth and billing setup
— consistent with this repo no longer treating generation as a cloud-account decision (ADR-029).
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sentinel.core.config import settings
from sentinel.generation.base import BaseLLM


class GoogleLLM(BaseLLM):
    name = "google"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._log_ready(chat=settings.google_chat_model, cheap=settings.google_cheap_model)

    def _build(self, model: str, **kwargs: Any) -> Any:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.google_api_key,
            temperature=0,
            timeout=self.timeout,
            max_retries=0,
            **kwargs,
        )

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.google_chat_model, max_tokens=1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.google_cheap_model, max_tokens=800)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "chat": settings.google_chat_model, "cheap": settings.google_cheap_model}
