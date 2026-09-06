"""Parse a document with Amazon Textract — with a full trace of every step.

    export AWS_REGION=us-east-1 TEXTRACT_S3_BUCKET=my-bucket
    python scripts/parsing/03_parse_aws.py knowledge/suzuki-brezza.pdf --features TABLES LAYOUT

Two things this makes concrete, both genuine AWS-vs-Azure differences:

1. **Sync vs async.** ``analyze_document`` takes bytes but handles one page. Anything multi-page
   goes through ``start_document_analysis``, which reads from S3 and returns a JobId you poll — so
   an S3 bucket is a hard requirement, not a convenience. The trace records which route was taken.

2. **Feature selection drives cost and capability.** No features is plain OCR. ``LAYOUT`` recovers
   reading order and headings and is what makes the output usable for RAG. The trace shows the raw
   Textract block-kind histogram and whether reconstruction used LAYOUT elements or fell back to
   grouping LINEs by vertical gap. Run with and without ``LAYOUT`` and diff the structure score.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # sibling _trace import

from _trace import ParseTracer

from sentinel.core.config import settings
from sentinel.core.observability import configure_logging
from sentinel.core.types import BlockType
from sentinel.parsing.aws_parser import TextractParser
from sentinel.parsing.base import detect_format
from sentinel.parsing.quality import score_parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument(
        "--features",
        nargs="*",
        default=["TABLES", "LAYOUT"],
        choices=["TABLES", "FORMS", "LAYOUT", "SIGNATURES"],
        help="empty list uses DetectDocumentText (OCR only, cheapest)",
    )
    parser.add_argument("--bucket", default=None, help="override the configured S3 bucket")
    args = parser.parse_args()

    # Honour settings.debug / LOG_LEVEL; the trace file captures debug events regardless of level.
    configure_logging(json_output=False)

    tracer = ParseTracer(args.path, provider="aws")
    tracer.section("inputs")
    fmt = detect_format(args.path)
    tracer.step("detect_format", path=args.path, format=fmt.value)
    tracer.step(
        "textract_config",
        features=args.features or "none (OCR only)",
        region=settings.aws_region,
        s3_bucket=args.bucket or settings.textract_s3_bucket or "(none)",
    )
    tracer.note(
        "Textract returns a flat block graph (LINE / WORD / CELL / LAYOUT_* nodes with "
        "Relationships), not a document tree. The adapter reconstructs reading order and table "
        "grids itself. LAYOUT elements, when present, carry heading roles and reading order that "
        "raw LINE order does not — without them the adapter groups LINEs into paragraphs by "
        "vertical gap, which is cruder but still far better than concatenating every line."
    )

    tracer.section("parse")
    aws = TextractParser(features=args.features, s3_bucket=args.bucket)
    try:
        with tracer.capture():
            doc = aws.parse(args.path)
    except Exception as exc:  # noqa: BLE001 — record the failure in the trace
        tracer.step("parse_failed", error=type(exc).__name__, detail=str(exc)[:200])
        path = tracer.write_partial()
        print(f"parse failed: {type(exc).__name__}: {exc}")
        print(f"trace           {path}")
        return 1

    doc.metrics.warnings.clear()
    report = score_parse(doc)
    trace_path = tracer.finish(doc, report)

    # ----------------------------------------------------------------- terminal summary
    print(f"features        {args.features or 'none (OCR only)'}")
    print(f"pages           {doc.metrics.pages}")
    print(f"blocks          {len(doc.blocks)}")
    print(f"tables          {doc.metrics.tables}")
    print(f"figures         {doc.metrics.figures}")
    print(f"duration        {doc.metrics.duration_s:.2f}s")
    print(
        f"cost            ${doc.metrics.usd_cost:.4f}  (${doc.metrics.usd_cost / max(doc.metrics.pages, 1):.4f}/page)"
    )
    print(f"quality         {report.score:.3f}")
    print(f"  structure     {report.structure_score:.3f}  <- this is what LAYOUT buys you")

    headings = [b for b in doc.blocks if b.type is BlockType.HEADING]
    print(f"\nheadings recovered: {len(headings)}")
    for block in headings[:10]:
        print(f"  h{block.level} p{block.page} {block.text[:90]}")

    tables = [b for b in doc.blocks if b.type is BlockType.TABLE]
    print(f"\ntables recovered: {len(tables)}")
    for block in tables[:6]:
        t = block.table
        conf = f" conf={t.confidence:.0f}" if t.confidence is not None else ""
        print(f"  p{block.page} {t.rows}x{t.cols}{conf}")

    print(f"\ntrace           {trace_path}")
    print(f"parsed json     {trace_path.with_name('aws.parsed.json')}")
    print(f"content md      {trace_path.with_name('aws.content.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
