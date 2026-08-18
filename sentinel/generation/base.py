"""LLM adapter interface.

Every adapter exposes the same three handles, because a production pipeline never uses one model:

* ``chat``     — the strong model, for final answers and verification.
* ``cheap``    — the small fast model, for routing, query rewriting, SQL/KQL generation, chunk
                 contextualisation. Typically 60-70% of all calls, and the main cost lever.
* ``vision``   — multimodal, for figure description at ingest.

They are LangChain `BaseChatModel` instances so the rest of the codebase (LangGraph agents,
structured output, tool binding) works identically whichever cloud is selected.

Two settings applied uniformly and deliberately:

* ``temperature=0`` — everything here is extraction, routing or grounded answering. None of it is
  creative writing.
* ``max_retries=0`` — retry policy lives in `core/resilience.py`. Leaving the SDK's own retries on
  as well produces compounding backoff and, under load, a retry storm.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sentinel.core.observability import log


class BaseLLM(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def chat(self) -> Any: ...

    @property
    @abstractmethod
    def cheap(self) -> Any: ...

    @property
    def vision(self) -> Any:
        """Defaults to the strong model, which is multimodal on all three providers."""
        return self.chat

    def structured(self, schema: type, cheap: bool = False) -> Any:
        """Binds a Pydantic schema for validated output.

        Uses the provider's constrained decoding where available: the model is prevented from
        emitting tokens that would make the output schema-invalid, rather than being asked nicely
        and parsed afterwards.
        """
        model = self.cheap if cheap else self.chat
        return model.with_structured_output(schema)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}

    def _log_ready(self, **fields: Any) -> None:
        log.info("llm_ready", provider=self.name, **fields)
