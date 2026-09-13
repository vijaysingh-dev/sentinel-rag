"""Create an index, upsert chunks and run all three search modes against pgvector.

    python scripts/vectorstore/01_index_and_search.py --provider local

pgvector is the only maintained vectorstore provider (ADR-029) — Azure AI Search and OpenSearch
adapters are archived at `.archive/providers/vectorstore/`. `--provider` stays as a flag for
symmetry with the other stage scripts and because the archived adapters can still be pointed at
directly if revived, but `local` is the only registered choice today.

Watch the three result lists: the lexical arm wins on exact identifiers, the dense arm wins on
paraphrase, and hybrid gets both. That is the argument for hybrid retrieval in one screen, and it
is more convincing than any benchmark table.
"""

from __future__ import annotations

import argparse
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider
from sentinel.core.types import Chunk
from sentinel.vectorstore.base import SearchFilter

DOCS = [
    (
        "Checkout Restart Procedure",
        "runbook",
        ["admin", "workflow"],
        ["checkout"],
        "To restart checkout, drain in-flight requests from the load balancer first, wait for open "
        "orders to finish processing, then perform a rolling restart one replica at a time. Never "
        "restart all replicas simultaneously during peak traffic.",
    ),
    (
        "Burn-Rate Alerting Methodology",
        "methodology",
        ["admin", "workflow", "support"],
        [],
        "A fast-burn alert fires when the error budget is consumed at a rate that would exhaust a "
        "30-day budget in under a day; a slow-burn alert fires on a rate that would exhaust it over "
        "several days. Both windows are needed because a single threshold trades off latency against "
        "false positives in only one direction.",
    ),
    (
        "Feature Flag Fault Injection",
        "runbook",
        ["admin", "workflow", "support"],
        ["flagd"],
        "flagd serves the flags that drive fault injection in this environment. A flag left on its "
        "fault variant for more than 90 seconds after the page fires should be treated as the "
        "leading suspect; beyond 300 seconds, escalate even if the root cause is not yet confirmed.",
    ),
    (
        "INC-2621 Post-mortem",
        "incident",
        ["admin"],
        ["kafka"],
        "On 2026-06-01 the kafka consumer group fell behind for 14 minutes, backing up the "
        "accounting and fraud-detection queues. Root cause was the kafkaQueueProblems fault flag "
        "left on its active variant; kafka itself was healthy throughout.",
    ),
    (
        "Checkout and Payment SLO",
        "methodology",
        ["admin", "workflow"],
        ["checkout", "payment"],
        "Checkout's latency SLO is 250ms at p99, measured from cart submission to order "
        "confirmation, most of which is the synchronous call to payment. Kafka publishes to the "
        "accounting and fraud-detection topics with consumer-group offset tracking on reconnect.",
    ),
]

QUERIES = [
    ("INC-2621", "exact identifier — lexical search should dominate"),
    ("what happens when a queue falls behind", "paraphrase — dense search should dominate"),
    ("bringing checkout back online safely", "vocabulary mismatch — hybrid should win"),
]


def build_chunks() -> list[Chunk]:
    chunks = []
    for i, (title, doc_type, roles, components, body) in enumerate(DOCS):
        text = f"{title}\n\n{body}"
        chunks.append(
            Chunk(
                chunk_id=Chunk.make_id(f"demo{i}", 0, text),
                doc_id=f"demo{i}",
                embed_text=text,
                context_text=text,
                parent_id=f"demo{i}",
                heading_path=title,
                doc_type=doc_type,
                source_uri=f"demo://{title}",
                allowed_roles=roles,
                components=components,
                strategy="demo",
                token_count=len(text.split()),
            )
        )
    return chunks


def show(label: str, results: list, limit: int = 3) -> None:
    print(f"\n  {label}")
    if not results:
        print("    (nothing)")
        return
    for item in results[:limit]:
        detail = []
        if item.dense_score is not None:
            detail.append(f"d={item.dense_score:.3f}")
        if item.sparse_score is not None:
            detail.append(f"s={item.sparse_score:.3f}")
        print(f"    {item.score:7.4f}  {item.chunk.heading_path:35s} {' '.join(detail)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=None, choices=["local"])
    parser.add_argument("--recreate", action="store_true", help="drop and rebuild the index")
    parser.add_argument("--role", default="admin", help="role to search as")
    args = parser.parse_args()

    configure_logging()

    embedder = get_provider("embedder")
    store = get_provider("vectorstore", args.provider)

    print(f"store      {store.name}")
    print(f"embedder   {embedder.name} / {embedder.model} ({embedder.dims}d)")
    print(f"native hybrid: {store.supports_native_hybrid}")

    store.ensure_index(embedder.dims, recreate=args.recreate)

    chunks = build_chunks()
    embedded = embedder.embed_chunks(chunks)
    written = store.upsert(embedded)
    print(f"\nupserted {written} chunks")

    # Re-running is a no-op: chunk ids are content-derived, so identical text upserts in place.
    again = store.upsert(embedded)
    print(f"re-upserted {again} chunks (idempotent — no duplicates created)")

    filters = SearchFilter(roles=[args.role], tenant_id="default")

    for query, why in QUERIES:
        print(f"\n{'=' * 66}")
        print(f"query: {query}")
        print(f"       ({why})")

        vector = embedder.embed_query(query)
        show("dense only", store.dense_search(vector, 5, filters))
        show("lexical only", store.sparse_search(query, 5, filters))
        show("hybrid", store.hybrid_search(query, vector, 5, filters))

    print(f"\n{'=' * 66}")
    print(store.health())
    return 0


if __name__ == "__main__":
    sys.exit(main())
