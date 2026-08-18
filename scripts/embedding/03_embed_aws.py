"""Embed with Amazon Bedrock — Titan and Cohere, which behave differently.

    export AWS_REGION=us-east-1
    python scripts/embedding/03_embed_aws.py --model amazon.titan-embed-text-v2:0
    python scripts/embedding/03_embed_aws.py --model cohere.embed-english-v3

The two differences that change how you write ingestion code:

**Titan has no batch endpoint.** One InvokeModel call per text. Throughput comes from client-side
concurrency, not from batching — this script shows the effect of `--workers` on wall time for the
same corpus.

**Cohere requires `input_type`.** `search_document` when indexing, `search_query` when querying.
Using the same value for both is the query/document asymmetry bug in its most concrete form, and it
produces no error.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from sentinel.core.observability import configure_logging
from sentinel.embedding.aws_embedder import BedrockEmbedder

QUERY = "how long can a feed be late before the index is suspended"
CORPUS = [
    "A feed more than 90 seconds late triggers a stale-data warning; beyond 300 seconds the "
    "affected index is suspended from publication until the feed recovers.",
    "Feed arrival times are recorded per source and compared against a rolling expected envelope.",
    "The rebalance calendar is published quarterly and effective on the third Friday.",
] * 8


def cosine(a, b) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    return float(va @ vb / ((np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dims", type=int, default=None, help="Titan only: 1024, 512 or 256")
    parser.add_argument("--workers", nargs="*", type=int, default=[1, 4, 8], help="Titan concurrency levels to compare")
    args = parser.parse_args()

    configure_logging()

    base = BedrockEmbedder(model=args.model, dims=args.dims, use_cache=False)
    print(f"model     {base.model}")
    print(f"family    {'cohere (batches up to 96)' if base.is_cohere else 'titan (one per call)'}")
    print(f"dims      {base.dims}")

    query_vec = base.embed_query(QUERY)
    doc_vecs = base.embed_documents(CORPUS[:3])
    print(f"\nquery <-> answer      {cosine(query_vec, doc_vecs[0]):.4f}")
    print(f"query <-> related     {cosine(query_vec, doc_vecs[1]):.4f}")
    print(f"query <-> unrelated   {cosine(query_vec, doc_vecs[2]):.4f}")

    if base.is_cohere:
        print(
            "\nCohere note: this ran with input_type=search_query for the query and "
            "search_document for the corpus. Using one value for both silently degrades "
            "separation."
        )
        return 0

    print(f"\nTitan concurrency, embedding {len(CORPUS)} texts:")
    print(f"{'workers':>8s} {'seconds':>9s} {'texts/sec':>10s}")
    for workers in args.workers:
        embedder = BedrockEmbedder(model=args.model, dims=args.dims, use_cache=False, max_workers=workers)
        started = time.perf_counter()
        embedder.embed_documents(CORPUS)
        elapsed = time.perf_counter() - started
        print(f"{workers:8d} {elapsed:9.2f} {len(CORPUS) / elapsed:10.1f}")

    print(
        "\nRaising workers is how you get Titan throughput. Too high earns "
        "ThrottlingException, which the retry policy absorbs at the cost of latency."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
