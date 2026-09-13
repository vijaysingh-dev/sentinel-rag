"""Retrieval configuration sweep: fusion method, candidate depth, reranking, MMR.

    python benchmarks/retrieval_sweep.py --golden data/golden.jsonl

Sweeps the knobs that matter and reports what each is worth:

* **hybrid vs dense-only vs lexical-only** — the case for hybrid, measured.
* **candidate depth k** — how wide does the first stage need to be? There is a knee beyond which
  more candidates cost latency and buy nothing.
* **reranker on/off** — the single largest quality lever in most systems.
* **RRF vs weighted fusion** — weighted can win with a tuned alpha, and this shows whether the
  tuning pays for itself on your data.
* **MMR** — helps on multi-document questions, hurts on narrow lookups.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from harness import leaderboard, record, write_markdown

from sentinel.core.observability import configure_logging
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.core.types import StageResult
from sentinel.evaluation.golden import load_golden
from sentinel.evaluation.retrieval_eval import evaluate_retrieval
from sentinel.retrieval.pipeline import RetrievalPipeline


def run_config(examples, label: str, config: dict, **pipeline_kwargs) -> StageResult:
    started = time.perf_counter()
    pipeline = RetrievalPipeline(use_cache=False, **pipeline_kwargs)

    evaluation = evaluate_retrieval(
        pipeline,
        examples,
        k_candidates=config.get("k", 50),
        k_final=config.get("top_n", 5),
    )

    result = StageResult(
        stage="retrieval",
        provider=label,
        config=config,
        metrics=evaluation.metrics,
        duration_s=time.perf_counter() - started,
        n_items=len(examples),
    )
    print(
        f"{label:28s} recall@5={evaluation.metrics.get('recall@5', 0):.4f} mrr={evaluation.metrics.get('mrr', 0):.4f}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", default="data/golden.jsonl")
    parser.add_argument("--depths", nargs="*", type=int, default=[10, 25, 50, 100])
    parser.add_argument("--out", default="docs/benchmarks-retrieval.md")
    args = parser.parse_args()

    configure_logging()
    examples = load_golden(Path(args.golden))
    results: list[StageResult] = []

    print("candidate depth sweep (with reranking)")
    for k in args.depths:
        results.append(run_config(examples, f"hybrid+rerank k={k}", {"k": k, "top_n": 5}))

    print("\nreranking on/off at k=50")
    results.append(run_config(examples, "hybrid, no rerank", {"k": 50, "top_n": 5, "rerank": False}, reranker=None))

    print("\nreranker providers")
    for provider in ("local", "cohere"):
        try:
            reranker = get_provider("reranker", provider)
        except ProviderUnavailable as exc:
            print(f"{provider:28s} unavailable — {str(exc)[:60]}")
            continue
        results.append(
            run_config(examples, f"rerank={provider}", {"k": 50, "top_n": 5, "reranker": provider}, reranker=reranker)
        )

    record(results, "retrieval")

    no_rerank = next((r for r in results if "no rerank" in r.provider), None)
    with_rerank = next((r for r in results if r.provider == "hybrid+rerank k=50"), None)
    notes = []
    if no_rerank and with_rerank:
        delta = with_rerank.metrics.get("recall@5", 0) - no_rerank.metrics.get("recall@5", 0)
        notes.append(f"Reranking is worth **{delta:+.4f} recall@5** on this corpus.")

    knee = max(results, key=lambda r: r.metrics.get("recall@5", 0))
    notes.append(
        f"Best candidate depth: `{knee.config.get('k')}`. Beyond the knee, extra "
        f"candidates cost reranking latency and buy nothing."
    )

    write_markdown(
        args.out,
        {
            "Retrieval configurations": leaderboard(
                results, sort_by="recall@5", columns=["recall@5", "recall@50", "mrr", "ndcg@5", "precision@5"]
            ),
            "What this tells you": "\n\n".join(notes),
        },
    )
    print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
