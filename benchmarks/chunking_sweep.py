"""Chunking strategy sweep.

    python benchmarks/chunking_sweep.py --golden data/golden.jsonl

Runs every registered chunking strategy end to end — re-chunk, re-embed, re-index, evaluate — and
ranks them on retrieval quality against the golden set.

This is the benchmark that most often produces a surprising result. Semantic chunking is
fashionable and frequently loses to plain recursive splitting while costing an extra full pass of
embeddings at ingest. The structural strategy with heading-path prefixes usually wins, and the
prefix is doing most of the work.

Note the index is rebuilt per strategy into a separate collection, because chunk ids and vectors
are strategy-specific: evaluating strategy B against an index built with strategy A measures
nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from harness import leaderboard, record, write_markdown

from sentinel.chunking.base import REGISTRY, get_chunker
from sentinel.core.config import settings
from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider
from sentinel.core.types import StageResult
from sentinel.evaluation.golden import load_golden
from sentinel.evaluation.retrieval_eval import evaluate_retrieval
from sentinel.parsing.hybrid import HybridParser
from sentinel.retrieval.pipeline import RetrievalPipeline


async def sweep(corpus: Path, golden_path: Path, strategies: list[str]) -> list[StageResult]:
    examples = load_golden(golden_path)
    embedder = get_provider("embedder")
    parser = HybridParser()

    documents = []
    for path in sorted(corpus.rglob("*")):
        if path.is_file():
            try:
                documents.append(await parser.parse(path, doc_type=path.parent.name))
            except Exception as exc:
                print(f"  parse failed for {path.name}: {type(exc).__name__}")

    print(f"parsed {len(documents)} documents\n")
    results: list[StageResult] = []

    for name in strategies:
        started = time.perf_counter()
        chunker = get_chunker(name)

        chunks = []
        for doc in documents:
            chunks.extend(chunker.split(doc))

        if not chunks:
            print(f"{name:24s} produced no chunks")
            continue

        # A separate collection per strategy: mixing them into one index makes every subsequent
        # number meaningless.
        store = get_provider("vectorstore", settings.vectorstore_provider)
        store.ensure_index(embedder.dims, recreate=True)
        store.upsert(embedder.embed_chunks(chunks))

        pipeline = RetrievalPipeline(store=store, embedder=embedder, use_cache=False)
        evaluation = evaluate_retrieval(pipeline, examples)

        token_counts = [c.token_count for c in chunks]
        results.append(
            StageResult(
                stage="chunking",
                provider=name,
                config={"chunks": len(chunks), "median_tokens": sorted(token_counts)[len(token_counts) // 2]},
                metrics={
                    **evaluation.metrics,
                    "chunks_per_doc": len(chunks) / max(len(documents), 1),
                },
                duration_s=time.perf_counter() - started,
                n_items=len(examples),
            )
        )

        print(
            f"{name:24s} recall@5={evaluation.metrics.get('recall@5', 0):.4f} "
            f"mrr={evaluation.metrics.get('mrr', 0):.4f} chunks={len(chunks)}"
        )

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="knowledge")
    parser.add_argument("--golden", default="data/golden.jsonl")
    parser.add_argument("--strategies", nargs="*", default=None)
    parser.add_argument("--out", default="docs/benchmarks-chunking.md")
    args = parser.parse_args()

    configure_logging()
    strategies = args.strategies or sorted(REGISTRY)

    results = asyncio.run(sweep(Path(args.corpus), Path(args.golden), strategies))
    if not results:
        return 1

    record(results, "chunking")
    write_markdown(
        args.out,
        {
            "Chunking strategies ranked by recall@5": leaderboard(
                results, sort_by="recall@5", columns=["recall@5", "recall@50", "mrr", "ndcg@5", "chunks_per_doc"]
            ),
            "Reading this table": "More chunks is not better — it inflates index size and crowds the top-k with "
            "near-duplicates. Prefer the strategy with the best recall at the *lowest* "
            "chunks_per_doc. A large gap between recall@50 and recall@5 means ranking is the "
            "bottleneck, not chunking.",
        },
    )
    print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
