"""Parse a document with free local libraries.

    python scripts/parsing/01_parse_local.py data/raw/methodology.pdf

Shows what the local parser extracts and how the reference-free quality heuristics score it. Run
this first on any new corpus: the quality score and its warnings tell you immediately whether you
need a cloud parser at all, which is the difference between paying per page for everything and
paying for the 10% that need it.
"""

from __future__ import annotations

import argparse
import json
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.types import BlockType
from sentinel.parsing.base import detect_format
from sentinel.parsing.local_parser import LocalParser
from sentinel.parsing.quality import score_parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--images", action="store_true", help="extract embedded images for later figure enrichment")
    parser.add_argument("--show", type=int, default=5, help="blocks to print")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()

    configure_logging()

    fmt = detect_format(args.path)
    doc = LocalParser(extract_images=args.images).parse(args.path)
    report = score_parse(doc)

    if args.json:
        print(
            json.dumps(
                {
                    "format": fmt.value,
                    "quality": report.score,
                    "components": {
                        "text_density": report.text_density,
                        "word_validity": report.word_validity,
                        "structure": report.structure_score,
                        "encoding": report.encoding_score,
                        "tables": report.table_score,
                    },
                    "warnings": report.warnings,
                    "blocks": len(doc.blocks),
                    "duration_s": doc.metrics.duration_s,
                },
                indent=2,
            )
        )
        return 0

    print(f"format          {fmt.value}")
    print(f"pages           {doc.metrics.pages}")
    print(f"blocks          {len(doc.blocks)}")
    print(f"characters      {doc.metrics.characters:,}")
    print(f"tables          {doc.metrics.tables}")
    print(f"figures         {doc.metrics.figures}")
    print(f"duration        {doc.metrics.duration_s:.2f}s")
    print(f"cost            ${doc.metrics.usd_cost:.4f}")
    print()
    print(f"quality         {report.score:.3f}")
    print(f"  text density  {report.text_density:.3f}")
    print(f"  word validity {report.word_validity:.3f}")
    print(f"  structure     {report.structure_score:.3f}")
    print(f"  encoding      {report.encoding_score:.3f}")
    print(f"  tables        {report.table_score:.3f}")

    if report.warnings:
        print("\nwarnings")
        for warning in report.warnings:
            print(f"  - {warning}")

    print(f"\nescalation needed: {report.needs_escalation}")

    print("\n--- first blocks " + "-" * 50)
    for block in doc.blocks[: args.show]:
        marker = "#" if block.type is BlockType.HEADING else block.type.value[:4]
        print(f"[{marker}] {block.text[:200]}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
