"""Caching: embeddings, retrieval results and answers.

The rule that governs every decision in this module: **cache knowledge, never telemetry.**

This codebase serves an operations assistant where one conversation mixes both. "What does a stale
feed mean?" is stable for months. "Is the SGX feed healthy?" is stable for seconds. Caching the
second turns the assistant into an incident: an engineer asks whether a feed recovered, receives a
ten-minute-old "healthy", and stops investigating. So cacheability is decided per *tool*, not per
request — see `is_cacheable`.

Every key includes the index version and the caller's role scope. Omitting the index version
serves stale chunks after a re-index; omitting the role scope means one caller receives another
caller's answer, which is not a performance bug but a data leak.

A cache failure must never fail the request. Every method swallows backend errors, counts them,
and behaves as a miss.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import CACHE_EVENTS, log

# Tools whose output is live state. A turn touching any of these is never cached.
#
# These names must match the registered tool names in `rca/tools/` exactly — a typo here silently
# makes a live tool cacheable, which is the failure this whole module exists to prevent. There is a
# regression test for it in `tests/test_pipeline.py`.
#
# `recent_deployments`, `flag_changes` and `incident_history` look like history and are still
# listed, because the window they query ends at "now": a cached answer to "did anything ship this
# morning" is wrong the moment something ships.
LIVE_TOOLS = {
    # implemented in rca/tools/
    "query_prometheus",
    "find_traces",
    "current_flags",
    "flag_changes",
    "recent_deployments",
    "incident_history",
    # platform monitoring tools this agent also fronts
    "system_monitor_health",
    "service_health_rollup",
    "kafka_lag",
    "query_logs",
}


def is_cacheable(tools_called: Iterable[str]) -> bool:
    """True only when the turn was answered purely from documentation."""
    called = set(tools_called)
    return bool(called) and not (called & LIVE_TOOLS)


def _normalise(text: str) -> str:
    """Collapses trivial variation so 'How  do I RESET?' and 'how do i reset' share a key."""
    return " ".join(text.lower().split()).strip(" ?.!")


def _hash(text: str) -> str:
    return hashlib.sha256(_normalise(text).encode()).hexdigest()[:24]


def role_scope(roles: Iterable[str]) -> str:
    """Stable fingerprint of an effective permission set, used to namespace every cache."""
    return hashlib.sha256(",".join(sorted(roles)).encode()).hexdigest()[:12]


class CacheBackend:
    """Minimal interface, so tests and local runs can use a dict instead of Redis."""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    def mget(self, keys: list[str]) -> list[str | None]: ...
    def mset(self, items: dict[str, str], ttl: int | None = None) -> None: ...
    def hvals(self, key: str) -> list[str]: ...
    def hset(self, key: str, field: str, value: str) -> None: ...
    def expire(self, key: str, ttl: int) -> None: ...


class MemoryCache(CacheBackend):
    """Process-local fallback. Not shared between workers; fine for tests and single-process runs."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._data[key] = value

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self._data.get(k) for k in keys]

    def mset(self, items: dict[str, str], ttl: int | None = None) -> None:
        self._data.update(items)

    def hvals(self, key: str) -> list[str]:
        return list(self._hashes.get(key, {}).values())

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def expire(self, key: str, ttl: int) -> None: ...


class RedisCache(CacheBackend):
    def __init__(self, url: str):
        import redis

        self._r = redis.from_url(url, decode_responses=True, socket_timeout=2)

    def get(self, key: str) -> str | None:
        return self._r.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._r.set(key, value, ex=ttl)

    def mget(self, keys: list[str]) -> list[str | None]:
        return self._r.mget(keys) if keys else []

    def mset(self, items: dict[str, str], ttl: int | None = None) -> None:
        pipe = self._r.pipeline()
        for k, v in items.items():
            pipe.set(k, v, ex=ttl)
        pipe.execute()

    def hvals(self, key: str) -> list[str]:
        return self._r.hvals(key)

    def hset(self, key: str, field: str, value: str) -> None:
        self._r.hset(key, field, value)

    def expire(self, key: str, ttl: int) -> None:
        self._r.expire(key, ttl)


_backend: CacheBackend | None = None


def backend() -> CacheBackend:
    """Returns Redis when configured and reachable, otherwise an in-process dict.

    Degrading to memory rather than raising keeps the pipeline runnable on a laptop with no
    infrastructure, which is the difference between a codebase people experiment with and one
    they only read.
    """
    global _backend
    if _backend is not None:
        return _backend
    if not settings.cache_enabled:
        _backend = MemoryCache()
        return _backend
    try:
        cache = RedisCache(settings.redis_url)
        cache.get("healthcheck")
        _backend = cache
        log.info("cache_backend", kind="redis")
    except Exception as exc:
        log.warning("cache_backend_fallback", kind="memory", reason=str(exc)[:120])
        _backend = MemoryCache()
    return _backend


class EmbeddingCache:
    """Keyed by model plus content hash, so it survives re-chunking and re-indexing.

    The main consumer is ingestion: a crawl that re-processes four thousand unchanged pages should
    cost nothing. Because the key is content-derived rather than position-derived, a chunker change
    that leaves most chunk text byte-identical still hits.
    """

    def __init__(self, model: str, ttl: int | None = None):
        self.model = model
        self.ttl = ttl if ttl is not None else settings.embedding_cache_ttl

    def _key(self, text: str) -> str:
        return f"{settings.redis_key_prefix}:emb:{self.model}:{hashlib.sha256(text.encode()).hexdigest()}"

    def get_many(self, texts: list[str]) -> list[list[float] | None]:
        try:
            raw = backend().mget([self._key(t) for t in texts])
        except Exception:
            CACHE_EVENTS.labels("embedding", "error").inc()
            return [None] * len(texts)

        out: list[list[float] | None] = []
        for value in raw:
            if value is None:
                CACHE_EVENTS.labels("embedding", "miss").inc()
                out.append(None)
            else:
                CACHE_EVENTS.labels("embedding", "hit").inc()
                out.append(json.loads(value))
        return out

    def put_many(self, texts: list[str], vectors: list[list[float]]) -> None:
        try:
            backend().mset({self._key(t): json.dumps(v) for t, v in zip(texts, vectors, strict=True)}, ttl=self.ttl)
        except Exception:
            CACHE_EVENTS.labels("embedding", "error").inc()


class RetrievalCache:
    """Caches the chunk ids returned for a (query, filter) pair.

    Short TTL because the index changes underneath it; the index version in the key handles
    re-indexing, but the TTL handles incremental document updates that do not bump the version.
    """

    def __init__(self, ttl: int | None = None):
        self.ttl = ttl or settings.retrieval_cache_ttl

    def _key(self, query: str, scope: str, extra: dict[str, Any]) -> str:
        fingerprint = hashlib.sha256(json.dumps(extra, sort_keys=True, default=str).encode()).hexdigest()[:12]
        return f"{settings.redis_key_prefix}:ret:{settings.index_version}:{scope}:{fingerprint}:{_hash(query)}"

    def get(self, query: str, scope: str, **extra: Any) -> list[dict] | None:
        try:
            raw = backend().get(self._key(query, scope, extra))
        except Exception:
            CACHE_EVENTS.labels("retrieval", "error").inc()
            return None
        CACHE_EVENTS.labels("retrieval", "hit" if raw else "miss").inc()
        return json.loads(raw) if raw else None

    def put(self, query: str, scope: str, payload: list[dict], **extra: Any) -> None:
        try:
            backend().set(self._key(query, scope, extra), json.dumps(payload), ttl=self.ttl)
        except Exception:
            CACHE_EVENTS.labels("retrieval", "error").inc()


class SemanticAnswerCache:
    """Returns a prior answer when a new question is near-identical in meaning.

    Namespaced by role scope and index version so a cross-role hit is structurally impossible
    rather than merely checked for.

    The threshold deserves calibration on your own data before you trust it. "How do I enable
    delivery" and "how do I disable delivery" are close neighbours in embedding space and are
    opposite questions; a threshold tuned on paraphrase pairs alone will happily conflate them.
    """

    def __init__(self, embedder: Any, threshold: float | None = None, ttl: int | None = None):
        self.embedder = embedder
        self.threshold = threshold or settings.answer_cache_threshold
        self.ttl = ttl or settings.answer_cache_ttl

    def _namespace(self, scope: str) -> str:
        return f"{settings.redis_key_prefix}:ans:{settings.index_version}:{scope}"

    def get(self, query: str, scope: str) -> dict | None:
        try:
            entries = backend().hvals(self._namespace(scope))
        except Exception:
            CACHE_EVENTS.labels("answer", "error").inc()
            return None
        if not entries:
            CACHE_EVENTS.labels("answer", "miss").inc()
            return None

        import numpy as np

        vector = np.asarray(self.embedder.embed_query(query), dtype="float32")
        norm = np.linalg.norm(vector) or 1.0

        best, best_score = None, 0.0
        for raw in entries:
            entry = json.loads(raw)
            candidate = np.asarray(entry["vector"], dtype="float32")
            score = float(vector @ candidate / (norm * (np.linalg.norm(candidate) or 1.0)))
            if score > best_score:
                best, best_score = entry, score

        if best and best_score >= self.threshold:
            CACHE_EVENTS.labels("answer", "hit").inc()
            log.info("answer_cache_hit", similarity=round(best_score, 4), scope=scope)
            return {**best, "cached": True, "similarity": best_score}

        CACHE_EVENTS.labels("answer", "miss").inc()
        return None

    def put(self, query: str, scope: str, answer: str, citations: list[str]) -> None:
        try:
            payload = json.dumps(
                {
                    "query": query,
                    "answer": answer,
                    "citations": citations,
                    "vector": self.embedder.embed_query(query),
                }
            )
            namespace = self._namespace(scope)
            backend().hset(namespace, _hash(query), payload)
            backend().expire(namespace, self.ttl)
        except Exception:
            CACHE_EVENTS.labels("answer", "error").inc()
