"""Show what a cross-encoder reranker actually fixes.

    python scripts/reranking/01_rerank_compare.py --provider local

Run `scripts/vectorstore/01_index_and_search.py` first.

The queries here are chosen to expose the specific weakness of bi-encoder retrieval: a query and a
passage can be close in embedding space while meaning opposite things, because the embedding was
produced without ever seeing the query. A cross-encoder reads both together, so negation and
qualifiers register.

Watch the position changes, not the scores. A reranker that reorders nothing is not earning its
latency; a reranker that moves the right answer from rank 4 to rank 1 is the cheapest quality win
available.
"""

from __future__ import annotations

import argparse
import sys
import time

from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider
from sentinel.vectorstore.base import SearchFilter

QUERIES = [
    "which fault flags do NOT require a rolling restart to clear",
    "how do I safely bring checkout back online",
    "how long before a stuck queue triggers escalation",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=None, choices=["local", "cohere"])
    parser.add_argument("--k", type=int, default=10, help="candidate pool size")
    parser.add_argument("--top-n", type=int, default=3)
    args = parser.parse_args()

    configure_logging()

    embedder = get_provider("embedder")
    store = get_provider("vectorstore")
    reranker = get_provider("reranker", args.provider)

    print(f"reranker  {reranker.name} / {reranker.model}")
    print(f"floor     {reranker.default_floor}  (score scales differ per reranker — never copy a floor between them)")

    for query in QUERIES:
        print(f"\n{'=' * 70}")
        print(f"query: {query}")

        vector = embedder.embed_query(query)
        candidates = store.hybrid_search(query, vector, args.k, SearchFilter(roles=["admin"], tenant_id="default"))
        if not candidates:
            print("  no candidates — populate the index first")
            continue

        before = {c.chunk.chunk_id: i for i, c in enumerate(candidates)}

        started = time.perf_counter()
        reranked = reranker.rerank(query, candidates, top_n=args.top_n)
        elapsed = (time.perf_counter() - started) * 1000

        print(f"\n  retrieval order ({len(candidates)} candidates)")
        for i, item in enumerate(candidates[: args.top_n + 2]):
            print(f"    {i + 1}. {item.score:7.4f}  {item.chunk.heading_path}")

        print(f"\n  after reranking ({elapsed:.0f}ms)")
        for i, item in enumerate(reranked):
            moved = before.get(item.chunk.chunk_id, 99) - i
            arrow = f"  (up {moved})" if moved > 0 else f"  (down {-moved})" if moved < 0 else ""
            print(f"    {i + 1}. {item.score:7.4f}  {item.chunk.heading_path}{arrow}")

        dropped = len(candidates) - len(reranked)
        if dropped and len(reranked) < args.top_n:
            print(
                f"\n    {args.top_n - len(reranked)} results fell below the relevance floor — "
                f"this is how the system learns to say 'not documented'"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
