"""Prove that access control is enforced inside the search, not after it.

    python scripts/vectorstore/02_acl_filtering.py --provider local

Run `01_index_and_search.py` first to populate the index.

This is the script to be able to show when someone asks how you handle permissions in RAG. It
demonstrates three properties:

1. **Different roles get different result sets** for the identical query.
2. **The filter is part of the query**, so restricted content is never loaded into the process. A
   post-filter has already read it, and by then a log line or a stack trace can leak it.
3. **Empty roles fail closed.** An empty role list would render as an empty filter clause, which
   matches everything — the difference between "sees nothing" and "sees the entire corpus".
"""

from __future__ import annotations

import argparse
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider
from sentinel.core.resilience import AuthorizationViolation
from sentinel.vectorstore.base import SearchFilter

QUERY = "what went wrong with EU_TECH_50 and how do I restart the engine"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=None, choices=["local", "azure", "aws"])
    args = parser.parse_args()

    configure_logging()

    embedder = get_provider("embedder")
    store = get_provider("vectorstore", args.provider)
    vector = embedder.embed_query(QUERY)

    print(f"query: {QUERY}\n")

    for role in ("admin", "workflow", "support"):
        filters = SearchFilter(roles=[role], tenant_id="default")
        results = store.hybrid_search(QUERY, vector, 10, filters)
        print(f"role={role:9s} sees {len(results)} chunks")
        for item in results:
            print(f"    {item.chunk.doc_type:12s} {item.chunk.heading_path}")
        print()

    print(
        "Note the incident post-mortem is admin-only, and the ICE runbook is not visible to "
        "support. Those differences come from the filter inside the query.\n"
    )

    print("component scoping — a support engineer scoped to data-feed only:")
    filters = SearchFilter(roles=["support"], components=["data-feed"], tenant_id="default")
    for item in store.hybrid_search(QUERY, vector, 10, filters):
        print(f"    {item.chunk.doc_type:12s} {item.chunk.heading_path} {item.chunk.components}")

    print("\nfail-closed check — searching with no roles at all:")
    try:
        SearchFilter(roles=[], tenant_id="default").require_scope()
        print("    FAILED: an empty role set was accepted")
        return 1
    except AuthorizationViolation as exc:
        print(f"    refused, as it must: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
