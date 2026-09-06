"""Compare every available parser on one document, then show what hybrid would choose.

    python scripts/parsing/04_hybrid_compare.py knowledge/suzuki-brezza.pdf

This answers "how did you decide which parser to use?" with a table instead of an opinion. It
runs each configured provider, scores them on the same heuristics, prints cost and latency
alongside quality, and then shows which route the configured hybrid strategy takes and why.

The interesting column is cost-per-quality-point, not quality: a parser that scores 0.02 higher
for eight times the price is rarely the right answer on a large corpus.

Per-provider traces (``outputs/<stem>/<provider>.trace.log``) and a comparison summary
(``outputs/<stem>/compare.trace.log``) are written so the decision is auditable after the fact.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # sibling _trace import

from _trace import ParseTracer

from sentinel.core.observability import configure_logging
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.parsing.base import detect_format
from sentinel.parsing.hybrid import HybridParser
from sentinel.parsing.quality import score_parse


async def run(path: str, strategy: str) -> int:
    fmt = detect_format(path)
    compare = ParseTracer(path, provider="compare")
    compare.section("inputs")
    compare.step("document", path=path, format=fmt.value, strategy=strategy)

    results: dict[str, tuple] = {}
    for provider in ("local", "azure", "aws"):
        try:
            parser = get_provider("parser", provider)
        except ProviderUnavailable as exc:
            compare.step("provider_unavailable", provider=provider, reason=str(exc)[:80])
            print(f"{provider:8s} unavailable — {str(exc)[:80]}")
            continue

        if not parser.supports(fmt):
            compare.step("provider_unsupported", provider=provider, format=fmt.value)
            print(f"{provider:8s} does not support {fmt.value}")
            continue

        tracer = ParseTracer(path, provider=provider)
        tracer.section("parse")
        try:
            with tracer.capture():
                doc = await parser.aparse(path)
        except Exception as exc:  # noqa: BLE001
            tracer.step("parse_failed", error=type(exc).__name__, detail=str(exc)[:200])
            tracer.write_partial()
            compare.step("provider_failed", provider=provider, error=type(exc).__name__)
            print(f"{provider:8s} failed — {type(exc).__name__}: {str(exc)[:70]}")
            continue

        doc.metrics.warnings.clear()
        report = score_parse(doc)
        tracer.finish(doc, report)
        results[provider] = (doc, report)
        compare.step(
            provider,
            quality=report.score,
            pages=doc.metrics.pages,
            blocks=len(doc.blocks),
            headings=sum(1 for b in doc.blocks if b.type.value == "heading"),
            tables=doc.metrics.tables,
            figures=doc.metrics.figures,
            usd=doc.metrics.usd_cost,
            seconds=doc.metrics.duration_s,
        )

    if not results:
        compare.write_partial()
        print("no parser produced a result")
        return 1

    # ----------------------------------------------------------------- terminal table
    cols = ("provider", "quality", "text", "tables", "blocks", "pages", "secs", "usd")
    header = f"{cols[0]:10s} " + " ".join(f"{c:>7s}" for c in cols[1:])
    print(f"\n{header}")
    print("-" * len(header))
    for provider, (doc, report) in results.items():
        print(
            f"{provider:10s} {report.score:8.3f} {report.word_validity:7.3f} "
            f"{report.table_score:7.3f} {len(doc.blocks):7d} {doc.metrics.pages:6d} "
            f"{doc.metrics.duration_s:7.2f} {doc.metrics.usd_cost:8.4f}"
        )

    best = max(results.items(), key=lambda kv: kv[1][1].score)
    compare.section("verdict")
    compare.step("highest_quality", provider=best[0], score=best[1][1].score)
    print(f"\nhighest quality: {best[0]} ({best[1][1].score:.3f})")

    paid = {p: (d, r) for p, (d, r) in results.items() if d.metrics.usd_cost > 0}
    if paid and "local" in results:
        local_score = results["local"][1].score
        print("\nis paying worth it?")
        for provider, (doc, report) in paid.items():
            gain = report.score - local_score
            verdict = (
                "no quality gain over local — do not pay"
                if gain <= 0
                else f"+{gain:.3f} quality for ${doc.metrics.usd_cost:.4f} (${doc.metrics.usd_cost / gain:.4f}/point)"
            )
            compare.step("cost_efficiency", provider=provider, gain=round(gain, 4), usd=doc.metrics.usd_cost)
            print(f"  {provider}: {verdict}")

    # ----------------------------------------------------------------- hybrid route
    compare.section(f"hybrid('{strategy}') route")
    with compare.capture():
        hybrid_doc = await HybridParser(strategy=strategy).parse(path)
    hybrid_report = score_parse(hybrid_doc)
    compare.step(
        "route",
        parse_route=hybrid_doc.meta.get("parse_route"),
        quality=hybrid_doc.meta.get("quality_score"),
        local_quality=hybrid_doc.meta.get("local_quality"),
        cloud_quality=hybrid_doc.meta.get("cloud_quality"),
        prose_from=hybrid_doc.meta.get("prose_from"),
        tables_from=hybrid_doc.meta.get("tables_from"),
    )
    compare.finish(hybrid_doc, hybrid_report)

    print(f"\n--- what hybrid('{strategy}') would do " + "-" * 30)
    print(f"route     {hybrid_doc.meta.get('parse_route')}")
    print(f"quality   {hybrid_doc.meta.get('quality_score')}")
    if "prose_from" in hybrid_doc.meta:
        print(f"prose     {hybrid_doc.meta['prose_from']}")
        print(f"tables    {hybrid_doc.meta['tables_from']}")
    print(f"\ntraces    outputs/{Path(path).stem}/*.trace.log")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument(
        "--strategy", default="hybrid_escalate", choices=["single", "hybrid_escalate", "hybrid_merge", "race"]
    )
    args = parser.parse_args()

    # Honour settings.debug / LOG_LEVEL; the trace file captures debug events regardless of level.
    configure_logging(json_output=False)
    return asyncio.run(run(args.path, args.strategy))


if __name__ == "__main__":
    sys.exit(main())
