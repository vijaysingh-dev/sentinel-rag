"""Information-retrieval metrics.

Deterministic, cheap, no LLM required — which is why these run on every pull request while the
generation metrics run nightly. That split is the practical answer to "how do you put a
non-deterministic system in CI".

The one diagnostic to internalise: **report recall@5 and recall@50 together.** The gap localises
the failure. High recall@50 with low recall@5 is a ranking problem, and a reranker fixes it. Low
recall@50 is a candidate-generation problem, and no reranker can help — you must fix retrieval,
chunking, or parsing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def hit_rate(retrieved: Sequence[str], relevant: set[str], k: int = 5) -> float:
    """Did any relevant chunk appear in the top k? Blunt but very interpretable."""
    return 1.0 if set(retrieved[:k]) & relevant else 0.0


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    if k == 0:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / k


def mrr(retrieved: Sequence[str], relevant: set[str]) -> float:
    """Reciprocal rank of the first relevant result.

    The right metric when there is essentially one correct answer, which describes most
    operational lookups: you care whether the runbook came first, not whether all four related
    documents appeared somewhere.
    """
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevance: dict[str, float], k: int) -> float:
    """Normalised discounted cumulative gain.

    The most complete ranking metric: it handles graded relevance (not just binary) and discounts
    by position, so a relevant result at rank 5 counts for less than the same result at rank 1.
    """
    gains = [relevance.get(doc_id, 0.0) for doc_id in retrieved[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))

    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def average_precision(retrieved: Sequence[str], relevant: set[str]) -> float:
    if not relevant:
        return 0.0
    hits, total = 0, 0.0
    for i, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            hits += 1
            total += hits / i
    return total / len(relevant)


def substring_hit(contexts: Iterable[str], substrings: Sequence[str]) -> float:
    """Fraction of expected answer substrings present in the retrieved context.

    A pragmatic fallback when chunk ids are not stable enough to label directly — useful early,
    when you have a golden set but the chunker is still changing under it.
    """
    if not substrings:
        return 0.0
    joined = " ".join(contexts).lower()
    return sum(1 for s in substrings if s.lower() in joined) / len(substrings)


def aggregate(rows: list[dict], keys: Sequence[str] | None = None) -> dict[str, float]:
    """Means over a list of per-example metric dicts, ignoring None values."""
    if not rows:
        return {}
    if keys is None:
        # Union across rows, not just rows[0]: categories like acl_denied/unanswerable contribute
        # different keys than answerable ones, and even among answerable rows, recall/mrr/ndcg are
        # only present when relevant_chunk_ids was populated for that example. Keying off rows[0]
        # alone would silently drop a metric whenever the first row happened to lack it.
        keys = []
        seen = set()
        for r in rows:
            for k, v in r.items():
                if isinstance(v, (int, float)) and k not in seen:
                    seen.add(k)
                    keys.append(k)
    out: dict[str, float] = {}
    for key in keys:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if values:
            out[key] = round(sum(values) / len(values), 4)
    return out


def bootstrap_ci(values: Sequence[float], iterations: int = 1000, confidence: float = 0.95) -> tuple[float, float]:
    """Bootstrap confidence interval for a metric mean.

    Included because with a 40-example golden set a two-point improvement is usually noise, and
    reporting a delta without an interval is how teams talk themselves into shipping regressions.
    """
    import random

    if not values:
        return (0.0, 0.0)
    means = []
    n = len(values)
    for _ in range(iterations):
        sample = [values[random.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((1 - confidence) / 2 * iterations)]
    hi = means[int((1 + confidence) / 2 * iterations) - 1]
    return (round(lo, 4), round(hi, 4))
