"""Provider registry.

Every stage of the pipeline has one abstract interface and three implementations (local, Azure,
AWS). This module is the lookup table that turns a config string into an instance.

Why a registry rather than plain imports at the call site:

* **Lazy imports.** `boto3` and the Azure SDKs are heavy and mutually irrelevant. Importing an
  adapter only when it is selected keeps startup fast and, more usefully, lets someone run the
  whole local pipeline without having either cloud SDK installed.
* **Benchmarks.** The bake-off scripts iterate over every registered provider for a stage. That
  loop is only possible if providers are enumerable rather than hard-coded at import sites.
* **Failure isolation.** A broken optional dependency surfaces as "provider X unavailable: reason"
  at construction time instead of an ImportError three modules deep during ingestion.

The registry deliberately does *not* try to be a dependency-injection framework. It resolves a
name to a constructed object and caches it; anything more elaborate obscures control flow for no
practical gain at this size.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import log

# stage -> provider -> "module.path:ClassName"
_SPECS: dict[str, dict[str, str]] = {
    "parser": {
        "local": "sentinel.parsing.local_parser:LocalParser",
        "azure": "sentinel.parsing.azure_parser:AzureDocIntelligenceParser",
        "aws": "sentinel.parsing.aws_parser:TextractParser",
    },
    "embedder": {
        "local": "sentinel.embedding.local_embedder:LocalEmbedder",
        "azure": "sentinel.embedding.azure_embedder:AzureOpenAIEmbedder",
        "aws": "sentinel.embedding.aws_embedder:BedrockEmbedder",
    },
    "vectorstore": {
        "local": "sentinel.vectorstore.pgvector_store:PgVectorStore",
        "azure": "sentinel.vectorstore.azure_search_store:AzureSearchStore",
        "aws": "sentinel.vectorstore.opensearch_store:OpenSearchStore",
    },
    "reranker": {
        "local": "sentinel.reranking.local_reranker:CrossEncoderReranker",
        "azure": "sentinel.reranking.azure_reranker:AzureSemanticReranker",
        "aws": "sentinel.reranking.aws_reranker:BedrockReranker",
    },
    "llm": {
        "local": "sentinel.generation.local_llm:LocalLLM",
        "azure": "sentinel.generation.azure_llm:AzureLLM",
        "aws": "sentinel.generation.aws_llm:BedrockLLM",
    },
}

_INSTANCES: dict[tuple[str, str, int], Any] = {}


class ProviderUnavailable(RuntimeError):
    """Raised when a provider is selected but cannot be constructed.

    Carries the underlying cause so the message names the missing package or credential rather
    than only the symptom.
    """


@dataclass(frozen=True)
class ProviderInfo:
    stage: str
    name: str
    spec: str
    available: bool
    reason: str = ""


def _load(spec: str) -> type:
    module_path, class_name = spec.split(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def get_provider(stage: str, name: str | None = None, **kwargs: Any) -> Any:
    """Constructs (and caches) the adapter for `stage`.

    Args:
        stage: One of the keys in `_SPECS`.
        name: Provider name; falls back to the configured default for that stage.
        **kwargs: Passed to the adapter constructor. Included in the cache key so two differently
            configured instances of the same provider do not collide — which matters for
            benchmarks that sweep, say, three embedding dimensionalities.
    """
    if stage not in _SPECS:
        raise KeyError(f"unknown stage '{stage}'; known: {sorted(_SPECS)}")

    name = name or _default_provider(stage)
    if name not in _SPECS[stage]:
        raise KeyError(f"unknown provider '{name}' for stage '{stage}'; known: {sorted(_SPECS[stage])}")

    cache_key = (stage, name, hash(tuple(sorted(kwargs.items()))) if kwargs else 0)
    if cache_key in _INSTANCES:
        return _INSTANCES[cache_key]

    try:
        instance = _load(_SPECS[stage][name])(**kwargs)
    except ImportError as exc:
        raise ProviderUnavailable(
            f"{stage}/{name} needs an uninstalled dependency: {exc}. "
            f"Install the matching extra, e.g. `pip install -e '.[{name}]'`."
        ) from exc
    except Exception as exc:
        raise ProviderUnavailable(f"{stage}/{name} failed to initialise: {exc}") from exc

    _INSTANCES[cache_key] = instance
    log.debug("provider_constructed", stage=stage, provider=name)
    return instance


def _default_provider(stage: str) -> str:
    return {
        "parser": settings.parse_provider,
        "embedder": settings.embed_provider,
        "vectorstore": settings.vectorstore_provider,
        "reranker": settings.rerank_provider,
        "llm": settings.llm_provider,
    }[stage]


def available_providers(stage: str) -> list[ProviderInfo]:
    """Probes every provider for a stage without constructing side effects.

    Used by `scripts/doctor.py` and by benchmark runners to skip providers whose SDK or
    credentials are absent, rather than failing the whole sweep on the first missing one.
    """
    out: list[ProviderInfo] = []
    for name, spec in _SPECS[stage].items():
        try:
            _load(spec)
            out.append(ProviderInfo(stage, name, spec, True))
        except Exception as exc:
            out.append(ProviderInfo(stage, name, spec, False, f"{type(exc).__name__}: {exc}"))
    return out


def register(stage: str, name: str, spec: str) -> None:
    """Adds a provider at runtime, for experiments that should not need a code change."""
    _SPECS.setdefault(stage, {})[name] = spec


def clear_cache() -> None:
    """Drops cached instances. Tests use this between provider swaps."""
    _INSTANCES.clear()


def stages() -> list[str]:
    return sorted(_SPECS)


def provider_names(stage: str) -> list[str]:
    return sorted(_SPECS[stage])


def describe() -> dict[str, list[ProviderInfo]]:
    return {stage: available_providers(stage) for stage in stages()}


__all__ = [
    "get_provider",
    "available_providers",
    "register",
    "clear_cache",
    "stages",
    "provider_names",
    "describe",
    "ProviderUnavailable",
    "ProviderInfo",
]


def provider_factory(stage: str) -> Callable[..., Any]:
    """Returns a partial bound to a stage; convenience for call sites that resolve repeatedly."""

    def factory(name: str | None = None, **kwargs: Any) -> Any:
        return get_provider(stage, name, **kwargs)

    return factory
