"""Rank fusion and diversity.

Two independent rankers cannot be combined by adding their scores: a cosine similarity lives in
[0,1] and a BM25 score is unbounded and corpus-dependent. Normalising per query is unstable because
the score distribution shifts with the query. The standard answer is to discard the scores and use
the *ranks*, which is what reciprocal rank fusion does.
"""

from __future__ import annotations

from sentinel.core.config import settings
from sentinel.core.types import RetrievedChunk


def reciprocal_rank_fusion(
    rankings: list[list[RetrievedChunk]],
    k: int | None = None,
    weights: list[float] | None = None,
) -> list[RetrievedChunk]:
    """Fuses several ranked lists into one.

    Each list contributes `weight / (k + rank)` to every document it contains. The constant `k`
    (conventionally 60) damps the influence of the very top positions so that one ranker's
    confident-but-wrong first result cannot dominate.

    RRF is the default because it is scale-free, needs no tuning, works with any number of
    rankers, and is robust to one of them producing garbage scores. Weighted score fusion can beat
    it *if* you tune the weight on an evaluation set — and the right weight typically differs
    between natural-language and identifier-style queries, which is why untuned RRF usually wins
    in practice.
    """
    k = k if k is not None else settings.rrf_k
    weights = weights or [1.0] * len(rankings)

    scores: dict[str, float] = {}
    best: dict[str, RetrievedChunk] = {}

    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, item in enumerate(ranking, start=1):
            cid = item.chunk.chunk_id
            scores[cid] = scores.get(cid, 0.0) + weight / (k + rank)

            if cid not in best:
                best[cid] = item
            else:
                # Preserve whichever arm-specific scores each ranker contributed, so downstream
                # diagnostics can still tell dense hits from lexical ones.
                existing = best[cid]
                existing.dense_score = existing.dense_score or item.dense_score
                existing.sparse_score = existing.sparse_score or item.sparse_score
                existing.rerank_score = existing.rerank_score or item.rerank_score

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    out: list[RetrievedChunk] = []
    for rank, (cid, score) in enumerate(ordered):
        item = best[cid]
        item.score = score
        item.rank = rank
        item.retriever = "rrf"
        out.append(item)
    return out


def weighted_fusion(
    dense: list[RetrievedChunk],
    sparse: list[RetrievedChunk],
    alpha: float | None = None,
) -> list[RetrievedChunk]:
    """Min-max normalises both arms then blends them.

    Included for comparison against RRF in `benchmarks/retrieval_sweep.py`. It gives you an
    explicit dial (`alpha`) at the cost of needing an evaluation set to set it — and of being
    sensitive to outliers, since one extreme score compresses everything else after normalisation.
    """
    alpha = settings.hybrid_alpha if alpha is None else alpha

    def normalise(items: list[RetrievedChunk]) -> dict[str, float]:
        if not items:
            return {}
        values = [i.score for i in items]
        lo, hi = min(values), max(values)
        span = hi - lo
        if span == 0:
            return {i.chunk.chunk_id: 1.0 for i in items}
        return {i.chunk.chunk_id: (i.score - lo) / span for i in items}

    dense_scores = normalise(dense)
    sparse_scores = normalise(sparse)
    lookup = {i.chunk.chunk_id: i for i in [*dense, *sparse]}

    combined = {cid: alpha * dense_scores.get(cid, 0.0) + (1 - alpha) * sparse_scores.get(cid, 0.0) for cid in lookup}

    out = []
    for rank, (cid, score) in enumerate(sorted(combined.items(), key=lambda kv: -kv[1])):
        item = lookup[cid]
        item.score = score
        item.rank = rank
        item.retriever = f"weighted_a{alpha}"
        out.append(item)
    return out


def maximal_marginal_relevance(
    results: list[RetrievedChunk],
    query_vector: list[float],
    vectors: dict[str, list[float]],
    k: int = 5,
    lam: float | None = None,
) -> list[RetrievedChunk]:
    """Trades relevance against novelty so results are not near-duplicates.

    Matters most with overlapping chunks (sentence windows) or corpora with repeated boilerplate,
    where the naive top-5 can be five phrasings of one fact. Useful for questions that need
    evidence from several documents; counterproductive for narrow factual lookups, where diversity
    only dilutes.
    """
    import numpy as np

    lam = settings.mmr_lambda if lam is None else lam
    if not results:
        return []

    query = np.asarray(query_vector, dtype="float32")
    query /= np.linalg.norm(query) or 1.0

    candidates = [r for r in results if r.chunk.chunk_id in vectors]
    if not candidates:
        return results[:k]

    matrix = np.asarray([vectors[r.chunk.chunk_id] for r in candidates], dtype="float32")
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) or 1.0
    relevance = matrix @ query

    selected: list[int] = []
    remaining = list(range(len(candidates)))

    while remaining and len(selected) < k:
        if not selected:
            pick = max(remaining, key=lambda i: relevance[i])
        else:
            redundancy = (matrix[remaining] @ matrix[selected].T).max(axis=1)
            pick = remaining[int(np.argmax(lam * relevance[remaining] - (1 - lam) * redundancy))]
        selected.append(pick)
        remaining.remove(pick)

    return [candidates[i] for i in selected]


def dedupe_by_parent(results: list[RetrievedChunk], keep: int = 1) -> list[RetrievedChunk]:
    """Collapses multiple hits from the same parent section.

    Essential for parent-document and runbook strategies, where every child carries the full parent
    text: without this, five matching steps of one runbook return that runbook five times and
    consume the entire context budget on one document.
    """
    seen: dict[str, int] = {}
    out: list[RetrievedChunk] = []
    for item in results:
        parent = item.chunk.parent_id or item.chunk.chunk_id
        if seen.get(parent, 0) >= keep:
            continue
        seen[parent] = seen.get(parent, 0) + 1
        out.append(item)
    return out


def dedupe_by_content(results: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Drops chunks with identical content hashes, which overlap windows routinely produce."""
    seen: set[str] = set()
    out: list[RetrievedChunk] = []
    for item in results:
        key = item.chunk.content_hash or item.chunk.chunk_id
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
