"""Compare every available parser on one document, then show what hybrid would choose.

    python scripts/parsing/04_hybrid_compare.py data/raw/methodology.pdf

This is the script that answers "how did you decide which parser to use?" with a table instead of
an opinion. It runs each provider that is actually configured, scores them on the same heuristics,
prints cost and latency alongside quality, and then shows which route the configured hybrid
strategy would take and why.

The interesting column is usually cost-per-quality-point, not quality: a parser that scores 0.02
higher for eight times the price is rarely the right answer on a large corpus.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.parsing.base import detect_format
from sentinel.parsing.hybrid import HybridParser
from sentinel.parsing.quality import score_parse


async def run(path: str, strategy: str) -> int:
    fmt = detect_format(path)
    print(f"document  {path}")
    print(f"format    {fmt.value}\n")

    results = {}
    for provider in ("local", "azure", "aws"):
        try:
            parser = get_provider("parser", provider)
        except ProviderUnavailable as exc:
            print(f"{provider:8s} unavailable — {str(exc)[:80]}")
            continue

        if not parser.supports(fmt):
            print(f"{provider:8s} does not support {fmt.value}")
            continue

        try:
            doc = await parser.aparse(path)
        except Exception as exc:
            print(f"{provider:8s} failed — {type(exc).__name__}: {str(exc)[:70]}")
            continue

        results[provider] = (doc, score_parse(doc))

    if not results:
        print("no parser produced a result")
        return 1

    print(f"\n{'provider':10s} {'quality':>8s} {'text':>7s} {'tables':>7s} {'blocks':>7s} {'secs':>7s} {'usd':>8s}")
    print("-" * 60)
    for provider, (doc, report) in results.items():
        print(
            f"{provider:10s} {report.score:8.3f} {report.word_validity:7.3f} "
            f"{report.table_score:7.3f} {len(doc.blocks):7d} "
            f"{doc.metrics.duration_s:7.2f} {doc.metrics.usd_cost:8.4f}"
        )

    best = max(results.items(), key=lambda kv: kv[1][1].score)
    print(f"\nhighest quality: {best[0]} ({best[1][1].score:.3f})")

    # Cost efficiency only means something when a paid provider actually won something.
    paid = {p: (d, r) for p, (d, r) in results.items() if d.metrics.usd_cost > 0}
    if paid and "local" in results:
        local_score = results["local"][1].score
        print("\nis paying worth it?")
        for provider, (doc, report) in paid.items():
            gain = report.score - local_score
            if gain <= 0:
                print(f"  {provider}: no quality gain over local — do not pay")
            else:
                print(
                    f"  {provider}: +{gain:.3f} quality for "
                    f"${doc.metrics.usd_cost:.4f} "
                    f"(${doc.metrics.usd_cost / gain:.4f} per quality point)"
                )

    print(f"\n--- what hybrid('{strategy}') would do " + "-" * 30)
    doc = await HybridParser(strategy=strategy).parse(path)
    print(f"route     {doc.meta.get('parse_route')}")
    print(f"quality   {doc.meta.get('quality_score')}")
    if "prose_from" in doc.meta:
        print(f"prose     {doc.meta['prose_from']}")
        print(f"tables    {doc.meta['tables_from']}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument(
        "--strategy", default="hybrid_escalate", choices=["single", "hybrid_escalate", "hybrid_merge", "race"]
    )
    args = parser.parse_args()

    configure_logging()
    return asyncio.run(run(args.path, args.strategy))


if __name__ == "__main__":
    sys.exit(main())
