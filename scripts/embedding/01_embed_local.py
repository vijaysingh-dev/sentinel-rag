"""Embed text locally with sentence-transformers.

    python scripts/embedding/01_embed_local.py --model BAAI/bge-base-en-v1.5

Demonstrates the query/document asymmetry that silently costs several points of recall when it is
missed. BGE and E5 were trained with an instruction prefix on the query side only; this script
prints the similarity of the same pair with and without it so the difference is visible rather than
theoretical.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from sentinel.core.observability import configure_logging
from sentinel.embedding.local_embedder import QUERY_PREFIXES, LocalEmbedder

QUERY = "how do I restart the calculation engine"
PASSAGE = (
    "ICE Restart Procedure. Drain the input queue, wait for in-flight calculations to complete, "
    "then issue a rolling restart across the calculation nodes one region at a time."
)
DISTRACTOR = (
    "Exchange delivery latency SLO is 250ms at p99, measured from index publication to the "
    "exchange gateway acknowledgement."
)


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    return float(va @ vb / ((np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dims", type=int, default=None, help="unused locally; kept for symmetry")
    args = parser.parse_args()

    configure_logging()
    embedder = LocalEmbedder(model=args.model)

    print(f"model            {embedder.model}")
    print(f"dimensions       {embedder.dims}")
    print(f"max input tokens {embedder.max_input_tokens}")
    prefix = QUERY_PREFIXES.get(embedder.model, "")
    print(f"query prefix     {prefix!r}" if prefix else "query prefix     (none for this model)")

    query_vec = embedder.embed_query(QUERY)
    passage_vec, distractor_vec = embedder.embed_documents([PASSAGE, DISTRACTOR])

    print("\nwith correct prefix handling")
    print(f"  query <-> relevant passage    {cosine(query_vec, passage_vec):.4f}")
    print(f"  query <-> distractor          {cosine(query_vec, distractor_vec):.4f}")
    print(f"  margin                        {cosine(query_vec, passage_vec) - cosine(query_vec, distractor_vec):.4f}")

    if prefix:
        # Embedding the query as if it were a document is the bug this script exists to show.
        wrong = embedder.embed_documents([QUERY])[0]
        print("\ntreating the query as a document (the common mistake)")
        print(f"  query <-> relevant passage    {cosine(wrong, passage_vec):.4f}")
        print(f"  query <-> distractor          {cosine(wrong, distractor_vec):.4f}")
        print(f"  margin                        {cosine(wrong, passage_vec) - cosine(wrong, distractor_vec):.4f}")
        print("\nA smaller margin means the model separates relevant from irrelevant less well.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
