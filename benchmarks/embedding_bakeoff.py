"""Embedding model bake-off.

    python benchmarks/embedding_bakeoff.py --golden data/golden.jsonl

Compares embedding providers and dimensionalities on the same corpus and the same golden set,
reporting retrieval quality alongside index footprint, ingestion throughput and cost.

Three questions it answers:

1. **Does the expensive model actually retrieve better on this corpus?** Often by less than the
   price difference suggests, because domain-specific vocabulary is where general-purpose
   embedding models converge.
2. **What does Matryoshka truncation cost?** 3072 → 1024 typically loses about a point of recall
   and saves two thirds of the index footprint. Whether that is a good trade depends on whether
   storage or quality is your binding constraint.
3. **Is the local model good enough?** For corpora that cannot leave the building this is not a
   trade but a requirement, and the number worth knowing is how much quality that costs.

The index is rebuilt per configuration, because vectors from one model are meaningless against an
index built with another.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from harness import leaderboard, record, value_analysis, write_markdown

from sentinel.chunking.strategies import chunker_for_doc
from sentinel.core.observability import configure_logging
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.core.types import StageResult
from sentinel.evaluation.golden import load_golden
from sentinel.evaluation.retrieval_eval import evaluate_retrieval
from sentinel.parsing.hybrid import HybridParser
from sentinel.retrieval.pipeline import RetrievalPipeline

# (provider, dims). None means the provider's native dimensionality.
CONFIGURATIONS = [
    ("local", None),
    ("azure", 3072),
    ("azure", 1024),
    ("azure", 256),
    ("aws", 1024),
    ("aws", 256),
]


async def load_chunks(corpus: Path) -> list:
    """Parses and chunks once; every configuration embeds the identical chunk set.

    Re-chunking per configuration would confound the comparison — you would be measuring chunking
    and embedding together and unable to attribute a difference to either.
    """
    parser = HybridParser()
    chunks = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file():
            continue
        try:
            doc = await parser.parse(path, doc_type=path.parent.name)
        except Exception as exc:
            print(f"  parse failed for {path.name}: {type(exc).__name__}")
            continue
        chunks.extend(chunker_for_doc(doc).split(doc))
    return chunks


async def bake_off(corpus: Path, golden_path: Path, configurations: list[tuple[str, int | None]]) -> list[StageResult]:
    examples = load_golden(golden_path)
    chunks = await load_chunks(corpus)
    print(f"corpus: {len(chunks)} chunks, golden set: {len(examples)} examples\n")

    results: list[StageResult] = []

    for provider, dims in configurations:
        label = f"{provider}" + (f"@{dims}" if dims else "")
        try:
            kwargs = {"dims": dims} if dims else {}
            embedder = get_provider("embedder", provider, use_cache=False, **kwargs)
        except ProviderUnavailable as exc:
            print(f"{label:16s} unavailable — {str(exc)[:70]}")
            continue

        store = get_provider("vectorstore")
        store.ensure_index(embedder.dims, recreate=True)

        started = time.perf_counter()
        embedded = embedder.embed_chunks(chunks)
        embed_seconds = time.perf_counter() - started
        store.upsert(embedded)

        pipeline = RetrievalPipeline(store=store, embedder=embedder, use_cache=False)
        evaluation = evaluate_retrieval(pipeline, examples)

        # float32 per component; the practical index footprint scales with this.
        index_mb = len(chunks) * embedder.dims * 4 / (1024 * 1024)
        tokens = sum(c.token_count for c in chunks)

        results.append(
            StageResult(
                stage="embedding",
                provider=label,
                config={"dims": embedder.dims, "model": embedder.model, "truncations": embedder.truncation_count},
                metrics={
                    **evaluation.metrics,
                    "index_mb": round(index_mb, 2),
                    "chunks_per_sec": round(len(chunks) / max(embed_seconds, 0.001), 1),
                },
                duration_s=embed_seconds,
                usd_cost=_estimate_cost(embedder.model, tokens),
                n_items=len(examples),
            )
        )

        print(
            f"{label:16s} recall@5={evaluation.metrics.get('recall@5', 0):.4f} "
            f"mrr={evaluation.metrics.get('mrr', 0):.4f} "
            f"dims={embedder.dims:5d} {index_mb:7.2f}MB "
            f"{len(chunks) / max(embed_seconds, 0.001):6.1f} chunks/s"
        )

        if embedder.truncation_count:
            print(
                f"                 {embedder.truncation_count} chunks were truncated — "
                f"chunk size exceeds this model's input limit"
            )

    return results


def _estimate_cost(model: str, tokens: int) -> float:
    from sentinel.core.observability import estimate_cost

    return estimate_cost(model, tokens, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="knowledge")
    parser.add_argument("--golden", default="data/golden.jsonl")
    parser.add_argument("--providers", nargs="*", default=None, help="restrict to these providers")
    parser.add_argument("--out", default="docs/benchmarks-embedding.md")
    args = parser.parse_args()

    configure_logging()

    configurations = CONFIGURATIONS
    if args.providers:
        configurations = [c for c in CONFIGURATIONS if c[0] in args.providers]

    results = asyncio.run(bake_off(Path(args.corpus), Path(args.golden), configurations))
    if not results:
        print("no results — check provider availability with `make doctor`")
        return 1

    record(results, "embedding")
    write_markdown(
        args.out,
        {
            "Embedding models ranked by recall@5": leaderboard(
                results,
                sort_by="recall@5",
                columns=["recall@5", "recall@50", "mrr", "ndcg@5", "index_mb", "chunks_per_sec"],
            ),
            "Is the hosted model worth it?": value_analysis(results, "recall@5"),
            "Reading this table": "Compare index_mb against recall: dimension reduction is nearly "
            "free in quality terms until it is suddenly not, and where that knee sits is corpus-specific. Also check "
            "chunks_per_sec — a local model at 12k chunks is minutes of ingestion, at 12M chunks "
            "it is days, and that alone can decide the provider regardless of quality.",
        },
    )
    print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
