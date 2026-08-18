"""Create an index, upsert chunks and run all three search modes — on any provider.

    python scripts/vectorstore/01_index_and_search.py --provider local
    python scripts/vectorstore/01_index_and_search.py --provider azure
    python scripts/vectorstore/01_index_and_search.py --provider aws

The same code path runs against pgvector, Azure AI Search and OpenSearch. That is the whole point
of the adapter layer, and this script is the proof.

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
        "ICE Restart Procedure",
        "runbook",
        ["admin", "workflow"],
        ["ice"],
        "To restart the calculation engine, drain the input queue first, wait for in-flight "
        "calculations to finish, then perform a rolling restart one region at a time. Never restart "
        "all regions simultaneously during market hours.",
    ),
    (
        "Divisor Adjustment Methodology",
        "methodology",
        ["admin", "workflow", "support"],
        [],
        "The divisor is adjusted on the ex-date of a special dividend so the index level remains "
        "continuous across the cash distribution. Ordinary dividends are not adjusted for in a "
        "price-return index.",
    ),
    (
        "Feed Staleness Thresholds",
        "runbook",
        ["admin", "workflow", "support"],
        ["data-feed"],
        "A feed more than 90 seconds late raises a stale-data warning. Beyond 300 seconds the "
        "affected indices are suspended from publication until the feed recovers.",
    ),
    (
        "INCIDENT-4471 Post-mortem",
        "incident",
        ["admin"],
        ["ice"],
        "On 2026-02-11 index EU_TECH_50 published a value 1.2% below expectation for 14 minutes. "
        "Root cause was an unprocessed spin-off corporate action; the divisor was not adjusted.",
    ),
    (
        "Exchange Delivery SLO",
        "methodology",
        ["admin", "workflow"],
        ["exchange-delivery"],
        "Delivery latency SLO is 250ms at p99, measured from index publication to exchange gateway "
        "acknowledgement. FIX sessions use sequence-number gap-fill on reconnect.",
    ),
]

QUERIES = [
    ("INCIDENT-4471", "exact identifier — lexical search should dominate"),
    ("what happens when market data arrives late", "paraphrase — dense search should dominate"),
    ("bringing the calc engine back up safely", "vocabulary mismatch — hybrid should win"),
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
    parser.add_argument("--provider", default=None, choices=["local", "azure", "aws"])
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
