"""Parse a document with Amazon Textract.

    export AWS_REGION=us-east-1 TEXTRACT_S3_BUCKET=my-bucket
    python scripts/parsing/03_parse_aws.py data/raw/scanned.pdf --features TABLES LAYOUT

Two things this makes concrete, both of which are genuine AWS-vs-Azure differences:

1. **Sync vs async.** `analyze_document` takes bytes but handles one page. Anything multi-page goes
   through `start_document_analysis`, which reads from S3 and returns a JobId you poll — so an S3
   bucket is a hard requirement, not a convenience.

2. **Feature selection drives cost and capability.** No features is plain OCR. `LAYOUT` is what
   recovers reading order and headings and makes the output usable for RAG rather than just text.
   Run with and without it and compare the structure score.
"""

from __future__ import annotations

import argparse
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.types import BlockType
from sentinel.parsing.aws_parser import TextractParser
from sentinel.parsing.quality import score_parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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

    configure_logging()

    doc = TextractParser(features=args.features, s3_bucket=args.bucket).parse(args.path)
    report = score_parse(doc)

    print(f"features        {args.features or 'none (OCR only)'}")
    print(f"pages           {doc.metrics.pages}")
    print(f"blocks          {len(doc.blocks)}")
    print(f"tables          {doc.metrics.tables}")
    print(f"duration        {doc.metrics.duration_s:.2f}s")
    print(
        f"cost            ${doc.metrics.usd_cost:.4f}  (${doc.metrics.usd_cost / max(doc.metrics.pages, 1):.4f}/page)"
    )
    print(f"quality         {report.score:.3f}")
    print(f"  structure     {report.structure_score:.3f}  <- this is what LAYOUT buys you")

    headings = [b for b in doc.blocks if b.type is BlockType.HEADING]
    print(f"\nheadings recovered: {len(headings)}")
    for block in headings[:8]:
        print(f"  h{block.level} {block.text[:90]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
