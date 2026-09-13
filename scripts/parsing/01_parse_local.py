"""Parse a document with free local libraries — with a full trace of every step.

    python scripts/parsing/01_parse_local.py knowledge/suzuki-brezza.pdf

Run this first on any new corpus. The terminal summary tells you whether you need a cloud
parser at all — the difference between paying per page for everything and paying for the
10% that need it. The trace log it writes to ``outputs/parsing/<stem>/local.trace.log`` tells you
*why* the score came out where it did: format detection, the text-layer probe, which
handler ran, what each page yielded, how the blocks were built, and every component of the
quality score with its warnings.

Start here when you have been handed the parsing stage: read the trace top to bottom for
five documents before touching anything downstream.
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
from sentinel.parsing.base import detect_format
from sentinel.parsing.local_parser import LocalParser
from sentinel.parsing.quality import score_parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument("--images", action="store_true", help="extract embedded images for later figure enrichment")
    parser.add_argument("--show", type=int, default=8, help="blocks to print to the terminal")
    args = parser.parse_args()

    # Honour settings.debug / LOG_LEVEL; the trace file captures debug events regardless of level.
    configure_logging(json_output=False)

    tracer = ParseTracer(args.path, provider="local")

    tracer.section("inputs")
    fmt = detect_format(args.path)
    tracer.step("detect_format", path=args.path, format=fmt.value)
    tracer.step("parser_config", extract_images=args.images)
    tracer.note(
        "detect_format probes the PDF text layer: >=100 extractable chars/page on the first 5 "
        "pages means PDF_NATIVE (a free library handles it), below that means PDF_SCANNED "
        "(needs cloud OCR)."
    )

    tracer.section("parse")
    local = LocalParser(extract_images=args.images)
    with tracer.capture():
        doc = local.parse(args.path)  # scores internally; that log.debug is captured
    # Re-score for the report object, resetting the warning list the internal pass appended to.
    doc.metrics.warnings.clear()
    report = score_parse(doc)

    trace_path = tracer.finish(doc, report)

    # ----------------------------------------------------------------- terminal summary
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

    print(
        f"\nescalation needed (score {report.score:.3f} < threshold "
        f"{settings.parse_quality_threshold}): {report.needs_escalation}"
    )

    print("\n--- first blocks " + "-" * 50)
    for block in doc.blocks[: args.show]:
        marker = "#" if block.type is BlockType.HEADING else block.type.value[:4]
        print(f"[{marker}] {block.text[:200]}")
        print()

    print(f"trace           {trace_path}")
    print(f"parsed json     {trace_path.with_name('local.parsed.json')}")
    print(f"content md      {trace_path.with_name('local.content.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
