"""Run the evaluation suite and apply the CI gate.

    python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl
    python scripts/evaluation/01_evaluate_stages.py --golden data/golden.jsonl --write-baseline

Retrieval metrics are deterministic and cheap, so they run every time. Generation metrics need an
LLM judge, so they are opt-in with `--generation`. That split is the practical answer to putting a
non-deterministic system in CI: gate every pull request on the cheap deterministic layer, run the
expensive layer nightly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider
from sentinel.evaluation.gate import check_gate, write_baseline
from sentinel.evaluation.golden import check_coverage, load_golden
from sentinel.evaluation.retrieval_eval import evaluate_retrieval
from sentinel.retrieval.pipeline import RetrievalPipeline


async def run(args: argparse.Namespace) -> int:
    examples = load_golden(args.golden)

    coverage = check_coverage(examples)
    print(f"golden set: {coverage['total']} examples")
    for category, count in sorted(coverage["counts"].items()):
        print(f"  {category:18s} {count:3d}  ({coverage['observed_mix'][category]:.0%})")

    if not coverage["balanced"]:
        print("\n  underrepresented categories:")
        for category, detail in coverage["underrepresented"].items():
            print(f"    {category}: {detail['observed']:.0%} observed, {detail['target']:.0%} target")
        print("  A set without negative cases cannot measure hallucination or ACL leakage.")

    print("\n" + "=" * 66)
    print("retrieval")
    print("=" * 66)

    pipeline = RetrievalPipeline()
    retrieval = evaluate_retrieval(pipeline, examples, k_candidates=args.k, k_final=args.top_n)

    for key, value in sorted(retrieval.metrics.items()):
        interval = retrieval.confidence_intervals.get(key)
        suffix = f"   95% CI [{interval[0]:.3f}, {interval[1]:.3f}]" if interval else ""
        print(f"  {key:22s} {value:.4f}{suffix}")

    wide_key, narrow_key = f"recall@{args.k}", f"recall@{args.top_n}"
    wide, narrow = retrieval.metrics.get(wide_key), retrieval.metrics.get(narrow_key)
    print("\n  diagnosis:")
    if wide is None or narrow is None:
        # Both keys come from examples with relevant_chunk_ids populated (see retrieval_eval.py).
        # A golden set scored entirely via answer_substrings has neither — that is a labeling gap,
        # not a signal that retrieval found nothing, so it must not be conflated with recall=0.0.
        print(
            f"    {wide_key}/{narrow_key} not measured — no golden example has relevant_chunk_ids "
            f"populated. Recall/MRR/nDCG need that field; substring_hit does not require it."
        )
    elif wide - narrow > 0.15:
        print(
            f"    recall@{args.k}={wide:.3f} but recall@{args.top_n}={narrow:.3f} — the answers "
            f"are being retrieved but ranked badly. Invest in reranking."
        )
    elif wide < 0.7:
        print(
            f"    recall@{args.k}={wide:.3f} — answers are not in the candidate pool at all. "
            f"No reranker will help. Fix chunking, embeddings or parsing."
        )
    else:
        print("    candidate generation and ranking are both healthy.")

    metrics = dict(retrieval.metrics)

    if args.generation:
        print("\n" + "=" * 66)
        print("generation")
        print("=" * 66)
        from sentinel.evaluation.generation_eval import evaluate_generation

        answers = json.loads(Path(args.answers).read_text()) if args.answers else {}
        if not answers:
            print("  --generation needs --answers pointing at generated answers; skipping")
        else:
            judge = get_provider("llm").chat
            generation = await evaluate_generation(examples, answers, judge)
            print(f"  {generation.summary()}")
            if generation.unsupported_claims:
                print(f"\n  unsupported claims ({len(generation.unsupported_claims)}):")
                for claim in generation.unsupported_claims[:5]:
                    print(f"    - {claim[:110]}")
            metrics.update(
                {
                    "faithfulness": generation.faithfulness,
                    "answer_relevance": generation.answer_relevance,
                    "citation_validity": generation.citation_validity,
                    "refusal_accuracy": generation.refusal_accuracy,
                }
            )

    print("\n" + "=" * 66)
    print("gate")
    print("=" * 66)
    result = check_gate(metrics, baseline_path=args.baseline)
    print(result.report())

    if args.write_baseline:
        write_baseline(metrics, args.baseline)
        print(f"\nbaseline written to {args.baseline} — commit this file")

    return 0 if result.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", default="data/golden.jsonl")
    parser.add_argument("--baseline", default="benchmarks/baseline.json")
    parser.add_argument("--k", type=int, default=50)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--generation", action="store_true", help="also run LLM-judge metrics")
    parser.add_argument("--answers", default=None, help="JSON of generated answers to grade")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    configure_logging()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
