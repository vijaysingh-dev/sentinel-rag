"""Parse a document with Azure AI Document Intelligence.

    export AZURE_DOC_INTELLIGENCE_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
    az login                       # or set AZURE_DOC_INTELLIGENCE_KEY
    python scripts/parsing/02_parse_azure.py data/raw/scanned.pdf

The exact call this demonstrates:

    client.begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=...),
        output_content_format=DocumentContentFormat.MARKDOWN,
    )

`prebuilt-layout` with markdown output is the combination that matters for RAG. `prebuilt-read` is
roughly seven times cheaper but returns no headings, no tables and no section hierarchy — which
means your chunker has nothing structural to work with. Compare the two with `--model` to see the
difference on your own documents before choosing.
"""

from __future__ import annotations

import argparse
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.types import BlockType
from sentinel.parsing.azure_parser import AzureDocIntelligenceParser
from sentinel.parsing.quality import score_parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument(
        "--model", default="prebuilt-layout", choices=["prebuilt-read", "prebuilt-layout", "prebuilt-document"]
    )
    parser.add_argument("--show-markdown", action="store_true", help="print the raw markdown the service returned")
    args = parser.parse_args()

    configure_logging()

    doc = AzureDocIntelligenceParser(model=args.model).parse(args.path)
    report = score_parse(doc)

    print(f"model           {args.model}")
    print(f"pages           {doc.metrics.pages}")
    print(f"blocks          {len(doc.blocks)}")
    print(f"tables          {doc.metrics.tables}")
    print(f"figures         {doc.metrics.figures}")
    print(f"duration        {doc.metrics.duration_s:.2f}s")
    print(
        f"cost            ${doc.metrics.usd_cost:.4f}  (${doc.metrics.usd_cost / max(doc.metrics.pages, 1):.4f}/page)"
    )
    print(f"quality         {report.score:.3f}")

    tables = [b for b in doc.blocks if b.type is BlockType.TABLE]
    if tables:
        print(f"\n--- first table ({tables[0].table.rows}x{tables[0].table.cols}) " + "-" * 30)
        print(tables[0].table.markdown[:1200])

    if args.show_markdown:
        print("\n--- service markdown " + "-" * 46)
        print(doc.meta.get("raw_markdown", "")[:3000])

    return 0


if __name__ == "__main__":
    sys.exit(main())
