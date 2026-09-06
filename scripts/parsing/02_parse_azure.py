"""Parse a document with Azure AI Document Intelligence — with a full trace of every step.

    export AZURE_DOC_INTELLIGENCE_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
    az login                       # or set AZURE_DOC_INTELLIGENCE_KEY
    python scripts/parsing/02_parse_azure.py knowledge/suzuki-brezza.pdf

The call this demonstrates:

    client.begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=...),
        output_content_format=DocumentContentFormat.MARKDOWN,
    )

``prebuilt-layout`` with markdown output is the combination that matters for RAG. ``prebuilt-read``
is ~7x cheaper but returns no headings, no tables and no section hierarchy — nothing structural
for the chunker. Compare them with ``--model``.

The trace (``outputs/<stem>/azure.trace.log``) records how many pages the service actually
returned — worth checking, because a free-tier (F0) resource silently caps analysis at two
pages per document, which shows up downstream as "half my document is missing from the index".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # sibling _trace import

from _trace import ParseTracer

from sentinel.core.observability import configure_logging
from sentinel.core.types import BlockType
from sentinel.parsing.azure_parser import AzureDocIntelligenceParser
from sentinel.parsing.base import detect_format
from sentinel.parsing.quality import score_parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument(
        "--model", default="prebuilt-layout", choices=["prebuilt-read", "prebuilt-layout", "prebuilt-document"]
    )
    parser.add_argument("--key-auth", action="store_true", help="use the API key instead of managed identity")
    args = parser.parse_args()

    # Honour settings.debug / LOG_LEVEL; the trace file captures debug events regardless of level.
    configure_logging(json_output=False)

    tracer = ParseTracer(args.path, provider="azure")
    tracer.section("inputs")
    fmt = detect_format(args.path)
    tracer.step("detect_format", path=args.path, format=fmt.value)
    tracer.step(
        "model", model=args.model, output_format="markdown", auth="key" if args.key_auth else "managed_identity"
    )
    tracer.note(
        "The whole reason to prefer this service for RAG is output_content_format=markdown: "
        "headings come back as ##, tables as markdown grids, section hierarchy preserved. The "
        "adapter still reads tables and figures from the structured response, because markdown "
        "rendering loses cell boundaries and figure positions."
    )

    tracer.section("parse")
    azure = AzureDocIntelligenceParser(model=args.model, use_managed_identity=not args.key_auth)
    try:
        with tracer.capture():
            doc = azure.parse(args.path)
    except Exception as exc:  # noqa: BLE001 — the point is to record the failure in the trace
        tracer.step("parse_failed", error=type(exc).__name__, detail=str(exc)[:200])
        path = tracer.write_partial()
        print(f"parse failed: {type(exc).__name__}: {exc}")
        print(f"trace           {path}")
        return 1

    doc.metrics.warnings.clear()
    report = score_parse(doc)
    trace_path = tracer.finish(doc, report)

    # ----------------------------------------------------------------- terminal summary
    print(f"model           {args.model}")
    print(f"pages returned  {doc.metrics.pages}")
    print(f"blocks          {len(doc.blocks)}")
    print(f"tables          {doc.metrics.tables}")
    print(f"figures         {doc.metrics.figures}")
    print(f"duration        {doc.metrics.duration_s:.2f}s")
    print(
        f"cost            ${doc.metrics.usd_cost:.4f}  (${doc.metrics.usd_cost / max(doc.metrics.pages, 1):.4f}/page)"
    )
    print(
        f"quality         {report.score:.3f}   structure {report.structure_score:.3f}   "
        f"tables {report.table_score:.3f}"
    )

    headings = [b for b in doc.blocks if b.type is BlockType.HEADING]
    print(f"\nheadings recovered: {len(headings)}")
    for block in headings[:10]:
        print(f"  h{block.level} {block.text[:90]}")

    tables = [b for b in doc.blocks if b.type is BlockType.TABLE]
    if tables:
        t = tables[0].table
        print(f"\nfirst table ({t.rows}x{t.cols}):")
        print("  " + t.markdown[:800].replace("\n", "\n  "))

    print(f"\ntrace           {trace_path}")
    print(f"parsed json     {trace_path.with_name('azure.parsed.json')}")
    print(f"content md      {trace_path.with_name('azure.content.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
