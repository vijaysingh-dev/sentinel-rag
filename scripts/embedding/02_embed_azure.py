"""Embed with Azure OpenAI, and measure what Matryoshka truncation costs you.

    export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
    az login
    python scripts/embedding/02_embed_azure.py --dims 3072 1024 512 256

The `text-embedding-3-*` models are trained so the leading dimensions are usable alone, so passing
`dimensions=512` returns a shorter vector that keeps most of the quality. Index size and query
latency both scale with dimensionality, and storage is often the binding constraint before quality
is — so this trade is worth measuring on your own data rather than assuming.

The exact call:

    client.embeddings.create(model=<deployment>, input=[...], dimensions=512)
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from sentinel.core.observability import configure_logging
from sentinel.embedding.azure_embedder import AzureOpenAIEmbedder

QUERY = "why did the divisor change after the special dividend"
PASSAGES = [
    "Special dividends require a divisor adjustment on the ex-date to keep the index level "
    "continuous across the cash distribution.",
    "Ordinary dividends are not adjusted for in a price-return index and flow through as a "
    "natural decline in the index level.",
    "Exchange delivery uses a FIX session with sequence-number gap-fill on reconnect.",
]


def cosine(a, b) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    return float(va @ vb / ((np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dims", nargs="*", type=int, default=[3072, 1024, 512, 256])
    parser.add_argument("--deployment", default=None)
    args = parser.parse_args()

    configure_logging()

    print(
        f"{'dims':>6s} {'relevant':>9s} {'related':>9s} {'unrelated':>10s} {'margin':>8s} {'ms':>7s} {'bytes/vec':>10s}"
    )
    print("-" * 64)

    for dims in args.dims:
        embedder = AzureOpenAIEmbedder(deployment=args.deployment, dims=dims, use_cache=False)

        started = time.perf_counter()
        query_vec = embedder.embed_query(QUERY)
        passage_vecs = embedder.embed_documents(PASSAGES)
        elapsed = (time.perf_counter() - started) * 1000

        scores = [cosine(query_vec, p) for p in passage_vecs]
        margin = scores[0] - scores[2]

        # float32 storage; the index footprint scales linearly with this number.
        print(
            f"{embedder.dims:6d} {scores[0]:9.4f} {scores[1]:9.4f} {scores[2]:10.4f} "
            f"{margin:8.4f} {elapsed:7.0f} {embedder.dims * 4:10d}"
        )

    print(
        "\nRead the margin column, not the absolute scores: what matters is whether the model "
        "still separates relevant from unrelated after truncation."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
