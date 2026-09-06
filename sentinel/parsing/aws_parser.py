"""Amazon Textract adapter.

Textract is the AWS counterpart to Azure Document Intelligence, with two structural differences
that shape this adapter and that are worth knowing when someone asks you to compare the clouds:

1. **Sync vs async is a hard API split.** ``analyze_document`` accepts inline bytes but handles a
   single page only. Anything multi-page must go through ``start_document_analysis``, which reads
   from S3 and returns a JobId you poll. So using Textract on real documents means having an S3
   bucket in the path — there is no "just send me the PDF" option the way there is with Azure.

2. **The response is a flat block graph, not a document tree.** Textract returns every LINE, WORD,
   CELL and LAYOUT element as a node with `Relationships` pointing at children by Id. Reconstructing
   reading order and table grids is *your* job. Azure hands back markdown; here you assemble it.
   Most of this file is that assembly.

Feature selection drives both capability and cost:

* ``DetectDocumentText`` (no features) — OCR only, cheapest.
* ``TABLES`` — table structure with cell coordinates.
* ``FORMS`` — key-value pairs; useful for forms, noise for prose.
* ``LAYOUT`` — titles, headers, section headers, reading order. This is what makes Textract usable
  for RAG rather than just OCR, and it is the analogue of Document Intelligence's layout model.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import PARSE_QUALITY, estimate_page_cost, log
from sentinel.core.resilience import ParseFailure, with_retry
from sentinel.core.types import (
    Block,
    BlockType,
    Figure,
    ParsedDocument,
    ParseMetrics,
    Table,
)
from sentinel.parsing.base import BaseParser, DocFormat, detect_format, doc_id_for

# LAYOUT block types that carry a heading role, mapped to a heading level.
LAYOUT_HEADINGS = {
    "LAYOUT_TITLE": 1,
    "LAYOUT_SECTION_HEADER": 2,
    "LAYOUT_HEADER": 3,
}
LAYOUT_IGNORED = {"LAYOUT_PAGE_NUMBER", "LAYOUT_FOOTER"}


class TextractParser(BaseParser):
    name = "aws"
    supported = {DocFormat.PDF_NATIVE, DocFormat.PDF_SCANNED, DocFormat.IMAGE}

    def __init__(
        self,
        region: str | None = None,
        s3_bucket: str | None = None,
        s3_prefix: str | None = None,
        features: list[str] | None = None,
        poll_interval_s: float = 2.0,
        max_wait_s: float = 600.0,
    ):
        import boto3

        self.region = region or settings.aws_region
        self.s3_bucket = s3_bucket or settings.textract_s3_bucket
        self.s3_prefix = s3_prefix or settings.textract_s3_prefix
        self.features = features or settings.textract_features
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s

        session = boto3.Session(profile_name=settings.aws_profile or None, region_name=self.region)
        self.textract = session.client("textract")
        self.s3 = session.client("s3")

    # ------------------------------------------------------------------------------ parse
    def parse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        path = Path(path)
        fmt = kwargs.get("format") or detect_format(path)
        started = time.perf_counter()

        page_count = self._page_count(path)
        size_mb = round(path.stat().st_size / 1024 / 1024, 2)
        # Single-page work stays synchronous to avoid an S3 round trip and job polling.
        if page_count <= 1 and path.stat().st_size < 5 * 1024 * 1024:
            log.debug("textract_route", path=str(path), route="sync", pages=page_count, size_mb=size_mb)
            blocks_raw = self._analyze_sync(path)
        else:
            if not self.s3_bucket:
                raise ParseFailure("multi-page Textract requires textract_s3_bucket; the async API reads from S3")
            log.debug(
                "textract_route",
                path=str(path),
                route="async_s3",
                pages=page_count,
                size_mb=size_mb,
                features=self.features,
            )
            blocks_raw = self._analyze_async(path)

        kinds: dict[str, int] = {}
        for b in blocks_raw:
            kinds[b["BlockType"]] = kinds.get(b["BlockType"], 0) + 1
        log.debug("textract_response", path=str(path), raw_blocks=len(blocks_raw), block_kinds=kinds)
        blocks = self._build_blocks(blocks_raw)
        pages = max(page_count, self._pages_in_response(blocks_raw))

        feature_key = (
            "aws:textract-analyze-tables"
            if "TABLES" in self.features
            else "aws:textract-analyze-layout"
            if "LAYOUT" in self.features
            else "aws:textract-detect"
        )

        metrics = ParseMetrics(
            provider=self.name,
            duration_s=round(time.perf_counter() - started, 3),
            pages=pages,
            characters=sum(len(b.text) for b in blocks),
            tables=sum(1 for b in blocks if b.type is BlockType.TABLE),
            figures=sum(1 for b in blocks if b.type is BlockType.FIGURE),
            usd_cost=estimate_page_cost(feature_key, pages),
        )

        doc = ParsedDocument(
            doc_id=doc_id_for(path),
            source_uri=str(path),
            title=path.stem.replace("_", " ").replace("-", " "),
            blocks=blocks,
            metrics=metrics,
            doc_type=kwargs.get("doc_type", "page"),
            tenant_id=kwargs.get("tenant_id", "default"),
            meta={"format": fmt.value, "features": self.features},
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
            pages=pages,
            blocks=len(blocks),
            quality=report.score,
            usd=metrics.usd_cost,
            duration_s=metrics.duration_s,
        )
        return doc

    async def aparse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        import asyncio

        async def call() -> ParsedDocument:
            return await asyncio.to_thread(self.parse, path, **kwargs)

        return await with_retry(call, attempts=3, operation="textract_parse")

    # ----------------------------------------------------------------------- API calls
    def _analyze_sync(self, path: Path) -> list[dict]:
        """Single-page path: bytes inline, immediate response."""
        payload = path.read_bytes()
        if self.features:
            response = self.textract.analyze_document(Document={"Bytes": payload}, FeatureTypes=self.features)
        else:
            response = self.textract.detect_document_text(Document={"Bytes": payload})
        return response.get("Blocks", [])

    def _analyze_async(self, path: Path) -> list[dict]:
        """Multi-page path: upload to S3, start a job, poll, then page through results.

        The result set is paginated with a NextToken and can run to thousands of blocks for a long
        document, so all pages are accumulated before reconstruction begins.
        """
        key = f"{self.s3_prefix}{doc_id_for(path)}/{path.name}"
        self.s3.upload_file(str(path), self.s3_bucket, key)
        log.debug("textract_uploaded", bucket=self.s3_bucket, key=key)

        document_location = {"S3Object": {"Bucket": self.s3_bucket, "Name": key}}
        if self.features:
            job = self.textract.start_document_analysis(DocumentLocation=document_location, FeatureTypes=self.features)
        else:
            job = self.textract.start_document_text_detection(DocumentLocation=document_location)

        job_id = job["JobId"]
        getter = self.textract.get_document_analysis if self.features else self.textract.get_document_text_detection

        deadline = time.monotonic() + self.max_wait_s
        while True:
            response = getter(JobId=job_id)
            status = response["JobStatus"]
            if status == "SUCCEEDED":
                break
            if status == "FAILED":
                raise ParseFailure(f"Textract job failed: {response.get('StatusMessage', '')}")
            if time.monotonic() > deadline:
                raise ParseFailure(f"Textract job {job_id} exceeded {self.max_wait_s}s")
            time.sleep(self.poll_interval_s)

        blocks = list(response.get("Blocks", []))
        next_token = response.get("NextToken")
        while next_token:
            page = getter(JobId=job_id, NextToken=next_token)
            blocks.extend(page.get("Blocks", []))
            next_token = page.get("NextToken")

        return blocks

    # --------------------------------------------------------------- graph reconstruction
    def _build_blocks(self, raw: list[dict]) -> list[Block]:
        """Turns Textract's flat block graph into ordered document blocks.

        Strategy: prefer LAYOUT elements when present, because they encode reading order and
        heading roles that raw LINE order does not. Fall back to LINE blocks grouped by page when
        LAYOUT was not requested or returned nothing.
        """
        by_id = {b["Id"]: b for b in raw}
        table_line_ids = self._ids_inside_tables(raw, by_id)

        layout_blocks = [b for b in raw if b["BlockType"].startswith("LAYOUT_")]
        strategy = "layout" if layout_blocks else "lines_fallback"
        log.debug(
            "textract_reconstruction",
            strategy=strategy,
            layout_elements=len(layout_blocks),
            reason="LAYOUT feature returned elements" if layout_blocks else "no LAYOUT elements; grouping LINEs by gap",
        )
        prose_blocks: list[Block] = (
            self._blocks_from_layout(layout_blocks, by_id, table_line_ids)
            if layout_blocks
            else self._blocks_from_lines(raw, table_line_ids)
        )
        table_blocks = self._table_blocks(raw, by_id)
        log.debug("textract_assembled", prose_blocks=len(prose_blocks), table_blocks=len(table_blocks))
        return [*prose_blocks, *table_blocks]

    def _blocks_from_layout(self, layout_blocks: list[dict], by_id: dict[str, dict], skip_ids: set[str]) -> list[Block]:
        out: list[Block] = []
        for element in layout_blocks:
            kind = element["BlockType"]
            if kind in LAYOUT_IGNORED or kind == "LAYOUT_TABLE":
                continue  # tables are emitted separately with their grid intact

            text = self._child_text(element, by_id, skip_ids)
            if not text.strip():
                continue

            page = element.get("Page")
            confidence = element.get("Confidence")
            bbox = self._bbox(element)

            if kind in LAYOUT_HEADINGS:
                out.append(
                    Block(
                        BlockType.HEADING,
                        text,
                        page=page,
                        level=LAYOUT_HEADINGS[kind],
                        confidence=confidence,
                        bbox=bbox,
                    )
                )
            elif kind == "LAYOUT_LIST":
                out.append(Block(BlockType.LIST, text, page=page, confidence=confidence, bbox=bbox))
            elif kind == "LAYOUT_FIGURE":
                out.append(
                    Block(BlockType.FIGURE, text, page=page, figure=Figure(page=page, bbox=bbox, caption=text or None))
                )
            else:
                out.append(Block(BlockType.PARAGRAPH, text, page=page, confidence=confidence, bbox=bbox))
        return out

    def _blocks_from_lines(self, raw: list[dict], skip_ids: set[str]) -> list[Block]:
        """Fallback: group LINE blocks into paragraphs by vertical gap.

        Cruder than LAYOUT but still far better than concatenating every line, because a large
        vertical gap is a reliable paragraph boundary in single-column documents.
        """
        lines = [b for b in raw if b["BlockType"] == "LINE" and b["Id"] not in skip_ids]
        out: list[Block] = []

        by_page: dict[int, list[dict]] = {}
        for line in lines:
            by_page.setdefault(line.get("Page", 1), []).append(line)

        for page, page_lines in sorted(by_page.items()):
            page_lines.sort(key=lambda b: (b["Geometry"]["BoundingBox"]["Top"], b["Geometry"]["BoundingBox"]["Left"]))
            buffer: list[str] = []
            previous_bottom: float | None = None

            for line in page_lines:
                box = line["Geometry"]["BoundingBox"]
                top, height = box["Top"], box["Height"]
                # A gap wider than one line height marks a new paragraph.
                if previous_bottom is not None and (top - previous_bottom) > height * 0.8 and buffer:
                    out.append(Block(BlockType.PARAGRAPH, " ".join(buffer), page=page))
                    buffer = []
                buffer.append(line.get("Text", ""))
                previous_bottom = top + height

            if buffer:
                out.append(Block(BlockType.PARAGRAPH, " ".join(buffer), page=page))
        return out

    def _table_blocks(self, raw: list[dict], by_id: dict[str, dict]) -> list[Block]:
        """Rebuilds table grids from CELL blocks and their row/column indices."""
        out: list[Block] = []
        for table in (b for b in raw if b["BlockType"] == "TABLE"):
            cells = [
                by_id[cid] for cid in self._children(table, "CHILD") if by_id.get(cid, {}).get("BlockType") == "CELL"
            ]
            if not cells:
                continue

            rows = max(c["RowIndex"] for c in cells)
            cols = max(c["ColumnIndex"] for c in cells)
            grid = [["" for _ in range(cols)] for _ in range(rows)]

            for cell in cells:
                text = self._child_text(cell, by_id, set())
                r, c = cell["RowIndex"] - 1, cell["ColumnIndex"] - 1
                if 0 <= r < rows and 0 <= c < cols:
                    grid[r][c] = text

            model = Table(
                rows=rows,
                cols=cols,
                cells=grid,
                markdown="",
                page=table.get("Page"),
                confidence=table.get("Confidence"),
            )
            model.markdown = model.to_markdown()
            out.append(
                Block(
                    BlockType.TABLE,
                    model.markdown,
                    page=table.get("Page"),
                    table=model,
                    confidence=table.get("Confidence"),
                )
            )
        return out

    # ------------------------------------------------------------------------- helpers
    @staticmethod
    def _children(block: dict, relationship: str) -> list[str]:
        for rel in block.get("Relationships", []) or []:
            if rel.get("Type") == relationship:
                return rel.get("Ids", [])
        return []

    def _child_text(self, block: dict, by_id: dict[str, dict], skip_ids: set[str]) -> str:
        """Collects text from a block's descendants.

        LAYOUT elements point at LINE blocks, which in turn point at WORDs; CELLs point straight at
        WORDs. Both shapes are handled by recursing until text-bearing nodes are reached.
        """
        parts: list[str] = []
        for child_id in self._children(block, "CHILD"):
            child = by_id.get(child_id)
            if not child or child_id in skip_ids:
                continue
            kind = child["BlockType"]
            if kind in {"WORD", "LINE"}:
                parts.append(child.get("Text", ""))
            elif kind == "SELECTION_ELEMENT":
                parts.append("[x]" if child.get("SelectionStatus") == "SELECTED" else "[ ]")
            else:
                nested = self._child_text(child, by_id, skip_ids)
                if nested:
                    parts.append(nested)
        return " ".join(p for p in parts if p).strip()

    def _ids_inside_tables(self, raw: list[dict], by_id: dict[str, dict]) -> set[str]:
        """Ids of LINE/WORD blocks already consumed by a table.

        Without this, table contents appear twice: once in the reconstructed grid and again as
        loose paragraph text, which pollutes retrieval with numeric noise.
        """
        consumed: set[str] = set()
        for table in (b for b in raw if b["BlockType"] == "TABLE"):
            for cell_id in self._children(table, "CHILD"):
                consumed.add(cell_id)
                cell = by_id.get(cell_id, {})
                consumed.update(self._children(cell, "CHILD"))
        return consumed

    @staticmethod
    def _bbox(block: dict) -> tuple[float, float, float, float] | None:
        box = block.get("Geometry", {}).get("BoundingBox")
        if not box:
            return None
        return (box["Left"], box["Top"], box["Left"] + box["Width"], box["Top"] + box["Height"])

    @staticmethod
    def _pages_in_response(raw: list[dict]) -> int:
        pages = {b.get("Page", 1) for b in raw}
        return len(pages) if pages else 1

    @staticmethod
    def _page_count(path: Path) -> int:
        if path.suffix.lower() != ".pdf":
            return 1
        try:
            from pypdf import PdfReader

            return len(PdfReader(str(path)).pages)
        except Exception:
            return 1
