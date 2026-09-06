"""Azure AI Document Intelligence adapter.

Document Intelligence is the strongest of the three parsers on the cases where parsing actually
fails: scanned pages, multi-column layouts, and tables whose structure is implied by ruling lines
rather than markup. It is also the most expensive per page, which is precisely why the hybrid
strategy exists — pay for it on the documents that need it, not on the native PDFs a free library
already handles.

Model choice matters and is a common source of overspend:

* ``prebuilt-read`` — OCR only. Text and reading order, no layout. Cheapest.
* ``prebuilt-layout`` — text, headings, tables, figures, section hierarchy, and markdown output.
  This is the one to use for RAG; the structure it recovers is what chunking depends on.
* ``prebuilt-document`` — layout plus general key-value extraction.
* Custom models — for fixed high-volume forms, irrelevant to a knowledge corpus.

The single most useful feature for this pipeline is ``output_content_format="markdown"``. Asking
the service for markdown rather than assembling it yourself means headings arrive as ``##``, tables
arrive as markdown grids, and section hierarchy is preserved — all of which feed the structural
chunker directly.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import PARSE_QUALITY, estimate_page_cost, log
from sentinel.core.resilience import ParseFailure, RateLimited, with_retry
from sentinel.core.types import (
    Block,
    BlockType,
    Figure,
    ParsedDocument,
    ParseMetrics,
    Table,
)
from sentinel.parsing.base import BaseParser, DocFormat, detect_format, doc_id_for

MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class AzureDocIntelligenceParser(BaseParser):
    name = "azure"
    supported = {
        DocFormat.PDF_NATIVE,
        DocFormat.PDF_SCANNED,
        DocFormat.DOCX,
        DocFormat.PPTX,
        DocFormat.XLSX,
        DocFormat.HTML,
        DocFormat.IMAGE,
    }

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        use_managed_identity: bool = True,
    ):
        """
        Args:
            use_managed_identity: Prefer `DefaultAzureCredential` over an API key. Managed identity
                means no secret in config or environment, which is the first thing a security
                review looks for. The key path remains for local development.
        """
        from azure.ai.documentintelligence import DocumentIntelligenceClient

        self.endpoint = endpoint or settings.azure_doc_intelligence_endpoint
        self.model = model or settings.azure_doc_intelligence_model
        if not self.endpoint:
            raise ValueError("azure_doc_intelligence_endpoint is not configured")

        key = api_key or settings.azure_doc_intelligence_key or os.getenv("AZURE_DI_KEY", "")
        if use_managed_identity and not key:
            from azure.identity import DefaultAzureCredential

            credential: Any = DefaultAzureCredential()
        else:
            from azure.core.credentials import AzureKeyCredential

            credential = AzureKeyCredential(key)

        self.client = DocumentIntelligenceClient(endpoint=self.endpoint, credential=credential)

    # ------------------------------------------------------------------------------ parse
    def parse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        from azure.ai.documentintelligence.models import (
            AnalyzeDocumentRequest,
            DocumentContentFormat,
        )
        from azure.core.exceptions import HttpResponseError

        path = Path(path)
        fmt = kwargs.get("format") or detect_format(path)
        started = time.perf_counter()

        # In debug mode, cap analysis at the first N pages. This keeps development runs cheap
        # (Document Intelligence bills per page) and avoids the surprise that a free-tier resource
        # silently caps at two pages anyway. Production sends the whole document.
        page_range = kwargs.get("pages") or self._dev_page_range(path)

        try:
            with path.open("rb") as fh:
                poller = self.client.begin_analyze_document(
                    self.model,
                    AnalyzeDocumentRequest(bytes_source=fh.read()),
                    # Markdown output is the whole reason to prefer this service for RAG: headings
                    # and tables arrive already structured instead of as positioned text runs.
                    output_content_format=DocumentContentFormat.MARKDOWN,
                    # Figure extraction lets the enrichment step describe diagrams later.
                    features=kwargs.get("features"),
                    # None -> whole document; "1-2" -> first two pages only (debug mode).
                    pages=page_range,
                )
            result = poller.result()
        except HttpResponseError as exc:
            if exc.status_code == 429:
                raise RateLimited(str(exc)) from exc
            raise ParseFailure(f"Document Intelligence failed: {exc}") from exc

        blocks = self._blocks_from_result(result)
        pages = len(getattr(result, "pages", []) or [])
        content = getattr(result, "content", "") or ""
        if page_range:
            log.warning(
                "azure_page_limited",
                path=str(path),
                pages_analyzed=page_range,
                reason="debug mode; set debug=false or pass pages= to analyze the whole document",
            )
        log.debug(
            "azure_result",
            path=str(path),
            model=self.model,
            page_range=page_range or "all",
            pages_returned=pages,
            markdown_chars=len(content),
            tables=len(getattr(result, "tables", []) or []),
            figures=len(getattr(result, "figures", []) or []),
            paragraphs=len(getattr(result, "paragraphs", []) or []),
        )

        metrics = ParseMetrics(
            provider=self.name,
            duration_s=round(time.perf_counter() - started, 3),
            pages=pages,
            characters=sum(len(b.text) for b in blocks),
            tables=sum(1 for b in blocks if b.type is BlockType.TABLE),
            figures=sum(1 for b in blocks if b.type is BlockType.FIGURE),
            usd_cost=estimate_page_cost(f"azure:{self.model}", pages),
        )

        doc = ParsedDocument(
            doc_id=doc_id_for(path),
            source_uri=str(path),
            title=path.stem.replace("_", " ").replace("-", " "),
            blocks=blocks,
            metrics=metrics,
            doc_type=kwargs.get("doc_type", "page"),
            tenant_id=kwargs.get("tenant_id", "default"),
            meta={
                "format": fmt.value,
                "model": self.model,
                "page_range": page_range or "all",
                "raw_markdown": getattr(result, "content", "")[:200_000],
            },
        )

        from sentinel.parsing.quality import score_parse

        report = score_parse(doc)
        PARSE_QUALITY.labels(self.name, fmt.value).observe(report.score)
        log.debug(
            "quality_components",
            provider=self.name,
            path=str(path),
            score=report.score,
            text_density=report.text_density,
            word_validity=report.word_validity,
            structure=report.structure_score,
            encoding=report.encoding_score,
            tables=report.table_score,
            warnings=report.warnings,
        )
        log.info(
            "parsed",
            provider=self.name,
            path=str(path),
            model=self.model,
            pages=pages,
            blocks=len(blocks),
            quality=report.score,
            usd=metrics.usd_cost,
            duration_s=metrics.duration_s,
        )
        return doc

    @staticmethod
    def _dev_page_range(path: Path) -> str | None:
        """``"1-N"`` when debug mode caps analysis, else ``None`` for the whole document.

        Returns None (no cap) whenever the document is already within the limit, so the page-range
        parameter is only sent when it actually changes behaviour.
        """
        from sentinel.core.config import settings

        limit = settings.azure_di_dev_page_limit
        if not settings.debug or limit <= 0:
            return None
        try:
            from pypdf import PdfReader

            total = len(PdfReader(str(path)).pages)
        except Exception:
            total = limit + 1  # unknown length; assume it exceeds the cap
        return f"1-{limit}" if total > limit else None

    async def aparse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        """Retries transient failures; the service throttles under burst ingestion."""
        import asyncio

        async def call() -> ParsedDocument:
            return await asyncio.to_thread(self.parse, path, **kwargs)

        return await with_retry(call, attempts=3, operation="azure_di_parse")

    # -------------------------------------------------------------------------- mapping
    def _blocks_from_result(self, result: Any) -> list[Block]:
        """Converts the service response into the pipeline's block model.

        The markdown `content` field is used for prose because it already encodes heading levels,
        while `tables` and `figures` are read from the structured response because their markdown
        rendering loses cell boundaries and figure positions.
        """
        blocks: list[Block] = []

        table_spans = self._table_spans(result)
        content = getattr(result, "content", "") or ""

        for chunk in self._split_markdown_outside_tables(content, table_spans):
            heading = MARKDOWN_HEADING.match(chunk)
            if heading:
                blocks.append(Block(BlockType.HEADING, heading.group(2).strip(), level=len(heading.group(1))))
            elif chunk.strip():
                blocks.append(Block(BlockType.PARAGRAPH, chunk.strip()))

        for table in getattr(result, "tables", []) or []:
            blocks.append(self._table_block(table))

        for figure in getattr(result, "figures", []) or []:
            regions = getattr(figure, "bounding_regions", None) or []
            page = regions[0].page_number if regions else None
            caption = getattr(getattr(figure, "caption", None), "content", None)
            blocks.append(
                Block(
                    type=BlockType.FIGURE,
                    text=caption or "",
                    page=page,
                    figure=Figure(
                        page=page, caption=caption, bbox=self._polygon_to_bbox(regions[0].polygon) if regions else None
                    ),
                )
            )

        return blocks

    @staticmethod
    def _table_spans(result: Any) -> list[tuple[int, int]]:
        """Character ranges of tables inside the markdown content.

        Needed so table text is not emitted twice — once as a structured table block and again as
        loose paragraph text scraped from the same markdown.
        """
        spans: list[tuple[int, int]] = []
        for table in getattr(result, "tables", []) or []:
            for span in getattr(table, "spans", []) or []:
                spans.append((span.offset, span.offset + span.length))
        return sorted(spans)

    @staticmethod
    def _split_markdown_outside_tables(content: str, spans: list[tuple[int, int]]) -> list[str]:
        """Yields paragraph-sized pieces of markdown, skipping table regions."""
        if not content:
            return []

        kept: list[str] = []
        cursor = 0
        for start, end in spans:
            if start > cursor:
                kept.append(content[cursor:start])
            cursor = max(cursor, end)
        kept.append(content[cursor:])

        pieces: list[str] = []
        for segment in kept:
            for para in re.split(r"\n\s*\n", segment):
                stripped = para.strip()
                if stripped:
                    pieces.append(stripped)
        return pieces

    @staticmethod
    def _table_block(table: Any) -> Block:
        """Rebuilds a grid from the service's flat cell list.

        Cells arrive with explicit row and column indices rather than as nested rows, so the grid
        is allocated first and filled by coordinate. Spanning cells are written into their origin
        only, which keeps row widths uniform.
        """
        rows = table.row_count
        cols = table.column_count
        grid = [["" for _ in range(cols)] for _ in range(rows)]

        for cell in table.cells:
            r, c = cell.row_index, cell.column_index
            if 0 <= r < rows and 0 <= c < cols:
                grid[r][c] = (cell.content or "").strip()

        regions = getattr(table, "bounding_regions", None) or []
        page = regions[0].page_number if regions else None
        caption = getattr(getattr(table, "caption", None), "content", None)

        model = Table(rows=rows, cols=cols, cells=grid, markdown="", page=page, caption=caption)
        model.markdown = model.to_markdown()
        return Block(BlockType.TABLE, model.markdown, page=page, table=model)

    @staticmethod
    def _polygon_to_bbox(polygon: list[float]) -> tuple[float, float, float, float] | None:
        """Converts the service's flat [x1,y1,x2,y2,...] polygon into an axis-aligned box."""
        if not polygon or len(polygon) < 4:
            return None
        xs = polygon[0::2]
        ys = polygon[1::2]
        return (min(xs), min(ys), max(xs), max(ys))
