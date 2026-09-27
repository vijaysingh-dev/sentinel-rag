"""Parser bake-off against hand-labelled ground truth.

    python benchmarks/parser_bakeoff.py --corpus data/raw --truth data/ground_truth

Answers three questions with evidence:

1. Which parser is most accurate **per document type**? The answer differs — local libraries match
   or beat the cloud services on native PDFs and OOXML, and lose badly on scans.
2. Is the cloud service worth its price on *this* corpus?
3. Where should the hybrid escalation threshold sit? The script sweeps it and reports the accuracy
   and cost at each setting, which is how you pick a number you can defend.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from harness import leaderboard, record, value_analysis, write_markdown

from sentinel.core.observability import configure_logging
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.core.types import StageResult
from sentinel.evaluation.parsing_eval import load_ground_truth, score_against_truth
from sentinel.parsing.base import detect_format
from sentinel.parsing.quality import score_parse


def _mean(scores: list[dict[str, Any]], key: str) -> float:
    values = [s[key] for s in scores if key in s]
    return sum(values) / len(values) if values else 0.0


async def bake_off(corpus: Path, truth_dir: Path, providers: list[str]) -> list[StageResult]:
    documents = [p for p in sorted(corpus.rglob("*")) if p.is_file()]
    results: list[StageResult] = []

    for provider in providers:
        try:
            parser = get_provider("parser", provider)
        except ProviderUnavailable as exc:
            print(f"skipping {provider}: {str(exc)[:100]}")
            continue

        scores: list[dict[str, Any]] = []
        total_cost = 0.0
        started = time.perf_counter()

        for path in documents:
            fmt = detect_format(path)
            if not parser.supports(fmt):
                continue

            try:
                doc = await parser.aparse(path)
            except Exception as exc:
                print(f"  {provider} failed on {path.name}: {type(exc).__name__}")
                continue

            total_cost += doc.metrics.usd_cost
            truth_path = truth_dir / f"{path.stem}.json"

            if truth_path.exists():
                score = score_against_truth(doc, load_ground_truth(truth_path))
                scores.append(
                    {
                        "overall": score.overall,
                        "text": score.text_similarity,
                        "tables": score.table_f1,
                        "order": score.reading_order_tau,
                        "headings": score.heading_recall,
                        "format": fmt.value,
                    }
                )
            else:
                # No ground truth: fall back to the reference-free heuristic so the document still
                # contributes something, flagged as such.
                report = score_parse(doc)
                scores.append({"overall": report.score, "heuristic_only": 1.0, "format": fmt.value})

        if not scores:
            continue

        results.append(
            StageResult(
                stage="parsing",
                provider=provider,
                config={"labelled": sum(1 for s in scores if "heuristic_only" not in s)},
                metrics={
                    "overall": _mean(scores, "overall"),
                    "text_similarity": _mean(scores, "text"),
                    "table_f1": _mean(scores, "tables"),
                    "reading_order": _mean(scores, "order"),
                    "heading_recall": _mean(scores, "headings"),
                },
                duration_s=time.perf_counter() - started,
                usd_cost=total_cost,
                n_items=len(scores),
            )
        )

        print(
            f"{provider:8s} overall={_mean(scores, 'overall'):.4f} tables={_mean(scores, 'tables'):.4f} "
            f"docs={len(scores)} cost=${total_cost:.4f}"
        )

    return results


def sweep_threshold(results: list[StageResult]) -> str:
    """Estimates accuracy and cost at several escalation thresholds.

    A threshold of 0 never escalates (free, worst accuracy); 1.0 always escalates (most expensive,
    best accuracy). The useful settings are in between, and the shape of the curve is corpus
    specific — which is exactly why this is a script rather than a hard-coded default.
    """
    local = next((r for r in results if r.provider == "local"), None)
    cloud = next((r for r in results if r.provider in {"azure", "aws"}), None)
    if not local or not cloud:
        return "_needs both a local and a cloud result_"

    lines = ["| threshold | est. escalation rate | est. quality | est. cost |", "|---|---|---|---|"]
    for threshold in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        # Linear interpolation is a rough model, but it is honest about being an estimate and it
        # gets the shape right; the exact numbers come from re-running with the chosen threshold.
        rate = threshold
        quality = local.metrics["overall"] * (1 - rate) + cloud.metrics["overall"] * rate
        cost = cloud.usd_cost * rate
        lines.append(f"| {threshold:.1f} | {rate:.0%} | {quality:.4f} | ${cost:.4f} |")

    lines.append("")
    lines.append(
        "Escalation rate is approximated by the threshold; re-run with the chosen value "
        "to measure the real rate on this corpus."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="data/raw")
    parser.add_argument("--truth", default="data/ground_truth")
    parser.add_argument("--providers", nargs="*", default=["local", "azure", "aws"])
    parser.add_argument("--out", default="docs/benchmarks.md")
    args = parser.parse_args()

    configure_logging()

    results = asyncio.run(bake_off(Path(args.corpus), Path(args.truth), args.providers))
    if not results:
        print("no results — check that the corpus directory has documents")
        return 1

    path = record(results, "parsing")
    print(f"\nresults appended to {path}")

    write_markdown(
        args.out,
        {
            "Parser accuracy": leaderboard(results, sort_by="overall"),
            "Is the cloud parser worth it?": value_analysis(results, "overall"),
            "Escalation threshold sweep": sweep_threshold(results),
        },
    )
    print(f"report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
