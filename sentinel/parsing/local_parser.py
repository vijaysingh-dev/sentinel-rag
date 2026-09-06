"""Local parsing with free libraries.

Handles every format the pipeline supports, at zero marginal cost. For native PDFs, DOCX, PPTX and
spreadsheets this is genuinely competitive with the cloud services — those formats already carry
structure, and a cloud OCR service adds nothing but latency and a bill. The local path loses on
scanned documents, complex multi-column layouts, and tables whose structure is implied by ruling
lines rather than by markup.

Library choices and why:

* **pdfplumber** for PDFs rather than pypdf alone, because it exposes word positions and ruling
  lines, which is what makes table extraction and column detection possible. pypdf is used only
  for the cheap text-layer probe in `base.py`.
* **python-docx** and **python-pptx** because they read the OOXML directly; the structure is in the
  file already, so heuristics are unnecessary.
* **openpyxl** via pandas for spreadsheets, with each sheet becoming its own table block. A
  workbook is not one document.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sentinel.core.observability import PARSE_QUALITY, log
from sentinel.core.types import (
    Block,
    BlockType,
    Figure,
    ParsedDocument,
    ParseMetrics,
    Table,
)
from sentinel.parsing.base import BaseParser, DocFormat, detect_format, doc_id_for

# Heading detection for formats without explicit markup (PDF, plain text). A line qualifies when
# it is short, does not end in sentence punctuation, and either is title-cased or matches a
# numbered-section pattern.
NUMBERED_HEADING = re.compile(r"^\s*(\d+(\.\d+)*)\s+[A-Z].{0,80}$")
MAX_HEADING_CHARS = 90


class LocalParser(BaseParser):
    name = "local"
    supported = {
        DocFormat.PDF_NATIVE,
        DocFormat.PDF_SCANNED,
        DocFormat.DOCX,
        DocFormat.PPTX,
        DocFormat.XLSX,
        DocFormat.CSV,
        DocFormat.MARKDOWN,
        DocFormat.HTML,
        DocFormat.TEXT,
        DocFormat.JSON,
    }

    def __init__(self, extract_images: bool = False, image_dir: str | None = None):
        """
        Args:
            extract_images: Save embedded images to disk so the figure-enrichment step can
                describe them with a vision model. Off by default because it triples parse time
                on image-heavy decks and is wasted unless enrichment actually runs.
            image_dir: Where to write extracted images.
        """
        self.extract_images = extract_images
        self.image_dir = Path(image_dir) if image_dir else Path("data/figures")

    def parse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        path = Path(path)
        fmt = kwargs.get("format") or detect_format(path)
        started = time.perf_counter()

        handlers = {
            DocFormat.PDF_NATIVE: self._parse_pdf,
            DocFormat.PDF_SCANNED: self._parse_pdf,  # yields little; hybrid escalates
            DocFormat.DOCX: self._parse_docx,
            DocFormat.PPTX: self._parse_pptx,
            DocFormat.XLSX: self._parse_xlsx,
            DocFormat.CSV: self._parse_csv,
            DocFormat.MARKDOWN: self._parse_markdown,
            DocFormat.HTML: self._parse_html,
            DocFormat.TEXT: self._parse_text,
            DocFormat.JSON: self._parse_json,
        }
        handler = handlers.get(fmt)
        if handler is None:
            raise ValueError(f"local parser cannot handle {fmt}")

        log.debug("local_dispatch", path=str(path), format=fmt.value, handler=handler.__name__)
        blocks, pages = handler(path)
        log.debug(
            "local_blocks_built",
            path=str(path),
            blocks=len(blocks),
            headings=sum(1 for b in blocks if b.type is BlockType.HEADING),
            tables=sum(1 for b in blocks if b.type is BlockType.TABLE),
            figures=sum(1 for b in blocks if b.type is BlockType.FIGURE),
        )

        metrics = ParseMetrics(
            provider=self.name,
            duration_s=round(time.perf_counter() - started, 3),
            pages=pages,
            characters=sum(len(b.text) for b in blocks),
            tables=sum(1 for b in blocks if b.type is BlockType.TABLE),
            figures=sum(1 for b in blocks if b.type is BlockType.FIGURE),
            usd_cost=0.0,
        )

        doc = ParsedDocument(
            doc_id=doc_id_for(path),
            source_uri=str(path),
            title=path.stem.replace("_", " ").replace("-", " "),
            blocks=blocks,
            metrics=metrics,
            doc_type=kwargs.get("doc_type", "page"),
            tenant_id=kwargs.get("tenant_id", "default"),
            meta={"format": fmt.value},
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
            format=fmt.value,
            blocks=len(blocks),
            pages=pages,
            quality=report.score,
            duration_s=metrics.duration_s,
        )
        return doc

    # ------------------------------------------------------------------------------- PDF
    def _parse_pdf(self, path: Path) -> tuple[list[Block], int]:
        """Extracts text, tables and optionally images, page by page.

        Tables are extracted *first* and their bounding boxes excluded from the text pass, so table
        contents do not also appear as loose paragraph text. Without that exclusion every table's
        numbers appear twice: once as a grid and once as unstructured noise.
        """
        import pdfplumber

        blocks: list[Block] = []
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)

            for page_no, page in enumerate(pdf.pages, start=1):
                table_bboxes: list[tuple[float, float, float, float]] = []

                for table_obj in page.find_tables():
                    extracted = table_obj.extract()
                    if not extracted or len(extracted) < 2:
                        continue
                    cells = [[(c or "").strip() for c in row] for row in extracted]
                    table = Table(
                        rows=len(cells),
                        cols=max(len(r) for r in cells),
                        cells=cells,
                        markdown="",
                        page=page_no,
                    )
                    table.markdown = table.to_markdown()
                    blocks.append(
                        Block(type=BlockType.TABLE, text=table.markdown, page=page_no, table=table, bbox=table_obj.bbox)
                    )
                    table_bboxes.append(table_obj.bbox)

                # `filter` drops any word whose midpoint falls inside a table we already captured.
                def outside_tables(obj: dict) -> bool:
                    if not table_bboxes:
                        return True
                    x = (obj.get("x0", 0) + obj.get("x1", 0)) / 2
                    y = (obj.get("top", 0) + obj.get("bottom", 0)) / 2
                    return not any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in table_bboxes)

                text = page.filter(outside_tables).extract_text() or ""
                text_blocks = self._blocks_from_text(text, page_no)
                blocks.extend(text_blocks)

                image_blocks: list[Block] = []
                if self.extract_images:
                    image_blocks = self._extract_page_images(page, page_no, path)
                    blocks.extend(image_blocks)

                # Per-page visibility: a brochure page that is all display text and images yields
                # near-nothing here, and that is exactly where a whole-document quality score hides
                # the problem.
                log.debug(
                    "pdf_page_extracted",
                    page=page_no,
                    tables=len(table_bboxes),
                    text_chars=len(text),
                    text_blocks=len(text_blocks),
                    headings=sum(1 for b in text_blocks if b.type is BlockType.HEADING),
                    images=len(image_blocks),
                )

        return blocks, page_count

    def _extract_page_images(self, page: Any, page_no: int, path: Path) -> list[Block]:
        """Crops embedded images so a vision model can describe them later."""
        out: list[Block] = []
        self.image_dir.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(page.images):
            try:
                bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
                # Ignore decorative slivers: logos, rules, background bands.
                if (bbox[2] - bbox[0]) < 80 or (bbox[3] - bbox[1]) < 80:
                    continue
                target = self.image_dir / f"{doc_id_for(path)}_p{page_no}_{i}.png"
                page.crop(bbox).to_image(resolution=150).save(str(target))
                out.append(
                    Block(
                        type=BlockType.FIGURE,
                        text="",
                        page=page_no,
                        figure=Figure(page=page_no, bbox=bbox, image_uri=str(target)),
                    )
                )
            except Exception as exc:
                log.debug("image_extract_failed", page=page_no, error=str(exc)[:100])
        return out

    # ------------------------------------------------------------------------------ DOCX
    def _parse_docx(self, path: Path) -> tuple[list[Block], int]:
        """Reads OOXML directly.

        Body elements are walked in document order rather than reading `doc.paragraphs` and
        `doc.tables` separately, because those two collections lose the interleaving — you end up
        with every table at the end, detached from the prose that introduces it.
        """
        from docx import Document as DocxDocument
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph

        docx = DocxDocument(str(path))
        blocks: list[Block] = []

        for element in self._iter_docx_body(docx):
            if isinstance(element, Paragraph):
                text = element.text.strip()
                if not text:
                    continue
                style = (element.style.name or "").lower()
                if style.startswith("heading"):
                    level = int(style.replace("heading", "").strip() or 1)
                    blocks.append(Block(BlockType.HEADING, text, level=level))
                elif style.startswith("list") or element.text.lstrip().startswith(("•", "-", "*")):
                    blocks.append(Block(BlockType.LIST, text))
                else:
                    blocks.append(Block(BlockType.PARAGRAPH, text))

            elif isinstance(element, DocxTable):
                cells = [[cell.text.strip() for cell in row.cells] for row in element.rows]
                if not cells:
                    continue
                table = Table(rows=len(cells), cols=len(cells[0]), cells=cells, markdown="")
                table.markdown = table.to_markdown()
                blocks.append(Block(BlockType.TABLE, table.markdown, table=table))

        return blocks, 1

    @staticmethod
    def _iter_docx_body(docx: Any) -> Iterator[Any]:
        """Yields paragraphs and tables in true document order."""
        from docx.oxml.ns import qn
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph

        body = docx.element.body
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, docx)
            elif child.tag == qn("w:tbl"):
                yield DocxTable(child, docx)

    # ------------------------------------------------------------------------------ PPTX
    def _parse_pptx(self, path: Path) -> tuple[list[Block], int]:
        """One slide becomes a heading plus its body, and speaker notes are kept.

        Notes matter more than people expect: in architecture and design decks the rationale lives
        in the notes while the slide itself is a diagram with three words on it. Dropping notes
        discards the only text worth retrieving.
        """
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(str(path))
        blocks: list[Block] = []

        for slide_no, slide in enumerate(prs.slides, start=1):
            title = ""
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text.strip()
            blocks.append(Block(BlockType.HEADING, title or f"Slide {slide_no}", page=slide_no, level=2))

            for shape in slide.shapes:
                if shape == slide.shapes.title:
                    continue

                if shape.has_text_frame and shape.text_frame.text.strip():
                    blocks.append(Block(BlockType.PARAGRAPH, shape.text_frame.text.strip(), page=slide_no))

                elif shape.has_table:
                    cells = [[c.text.strip() for c in row.cells] for row in shape.table.rows]
                    if cells:
                        table = Table(rows=len(cells), cols=len(cells[0]), cells=cells, markdown="", page=slide_no)
                        table.markdown = table.to_markdown()
                        blocks.append(Block(BlockType.TABLE, table.markdown, page=slide_no, table=table))

                elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE and self.extract_images:
                    blocks.append(self._save_pptx_image(shape, slide_no, path))

            notes = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                blocks.append(
                    Block(BlockType.PARAGRAPH, f"Speaker notes: {notes}", page=slide_no, meta={"is_notes": True})
                )

        return blocks, len(prs.slides)

    def _save_pptx_image(self, shape: Any, slide_no: int, path: Path) -> Block:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        target = self.image_dir / f"{doc_id_for(path)}_s{slide_no}_{shape.shape_id}.png"
        try:
            target.write_bytes(shape.image.blob)
            uri = str(target)
        except Exception:
            uri = None
        return Block(type=BlockType.FIGURE, text="", page=slide_no, figure=Figure(page=slide_no, image_uri=uri))

    # ------------------------------------------------------------------------------ XLSX
    def _parse_xlsx(self, path: Path) -> tuple[list[Block], int]:
        """Each sheet becomes a heading plus a table block.

        Large sheets are truncated with an explicit note rather than silently: a chunk claiming to
        be a table but containing an arbitrary first 200 rows is a subtle source of wrong answers.
        """
        import openpyxl

        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        blocks: list[Block] = []
        max_rows = 200

        for sheet in wb.worksheets:
            rows = []
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= max_rows:
                    break
                if any(c is not None and str(c).strip() for c in row):
                    rows.append([("" if c is None else str(c).strip()) for c in row])

            if not rows:
                continue

            blocks.append(Block(BlockType.HEADING, sheet.title, level=2))
            table = Table(rows=len(rows), cols=max(len(r) for r in rows), cells=rows, markdown="", caption=sheet.title)
            table.markdown = table.to_markdown()
            blocks.append(Block(BlockType.TABLE, table.markdown, table=table))

            if sheet.max_row and sheet.max_row > max_rows:
                blocks.append(
                    Block(
                        BlockType.PARAGRAPH,
                        f"[Sheet '{sheet.title}' truncated: showing {max_rows} of {sheet.max_row} rows]",
                        meta={"truncated": True},
                    )
                )

        wb.close()
        return blocks, len(wb.worksheets)

    # ------------------------------------------------------------------------------- CSV
    def _parse_csv(self, path: Path) -> tuple[list[Block], int]:
        """Sniffs the dialect so tab- and semicolon-separated files work without configuration."""
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096])
        except csv.Error:
            dialect = csv.excel

        rows = [r for r in csv.reader(io.StringIO(raw), dialect) if any(c.strip() for c in r)]
        if not rows:
            return [], 1

        capped = rows[:500]
        table = Table(rows=len(capped), cols=max(len(r) for r in capped), cells=capped, markdown="", caption=path.stem)
        table.markdown = table.to_markdown()

        blocks = [Block(BlockType.HEADING, path.stem, level=1), Block(BlockType.TABLE, table.markdown, table=table)]
        if len(rows) > 500:
            blocks.append(
                Block(
                    BlockType.PARAGRAPH, f"[Truncated: {len(rows)} rows total, showing 500]", meta={"truncated": True}
                )
            )
        return blocks, 1

    # -------------------------------------------------------------------------- MARKDOWN
    def _parse_markdown(self, path: Path) -> tuple[list[Block], int]:
        """Markdown already carries structure, so parsing is a direct mapping.

        Fenced code blocks are captured whole. Splitting a code fence is uniquely damaging: half a
        function retrieves on its identifiers and is actively misleading to anyone acting on it.
        """
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks: list[Block] = []
        buffer: list[str] = []
        in_fence = False
        fence_lines: list[str] = []

        def flush() -> None:
            if buffer:
                joined = "\n".join(buffer).strip()
                if joined:
                    blocks.append(Block(BlockType.PARAGRAPH, joined))
                buffer.clear()

        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                if in_fence:
                    blocks.append(Block(BlockType.CODE, "\n".join(fence_lines)))
                    fence_lines = []
                    in_fence = False
                else:
                    flush()
                    in_fence = True
                continue

            if in_fence:
                fence_lines.append(line)
                continue

            heading = re.match(r"^(#{1,6})\s+(.*)$", line)
            if heading:
                flush()
                blocks.append(Block(BlockType.HEADING, heading.group(2).strip(), level=len(heading.group(1))))
            elif line.strip().startswith("|") and line.count("|") >= 2:
                buffer.append(line)  # markdown table rows stay together as a paragraph block
            elif not line.strip():
                flush()
            else:
                buffer.append(line)

        if in_fence and fence_lines:
            blocks.append(Block(BlockType.CODE, "\n".join(fence_lines)))
        flush()
        return blocks, 1

    # ------------------------------------------------------------------------------ HTML
    def _parse_html(self, path: Path) -> tuple[list[Block], int]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")

        # Navigation, scripts and styles are pure noise in a knowledge corpus and dilute every
        # chunk they survive into.
        for tag in soup(["script", "style", "nav", "footer", "aside", "noscript"]):
            tag.decompose()

        blocks: list[Block] = []
        for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]):
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            name = element.name
            if name.startswith("h") and len(name) == 2:
                blocks.append(Block(BlockType.HEADING, text, level=int(name[1])))
            elif name == "pre":
                blocks.append(Block(BlockType.CODE, text))
            elif name == "li":
                blocks.append(Block(BlockType.LIST, text))
            elif name == "table":
                cells = [
                    [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])] for tr in element.find_all("tr")
                ]
                cells = [r for r in cells if r]
                if cells:
                    table = Table(rows=len(cells), cols=max(len(r) for r in cells), cells=cells, markdown="")
                    table.markdown = table.to_markdown()
                    blocks.append(Block(BlockType.TABLE, table.markdown, table=table))
            else:
                blocks.append(Block(BlockType.PARAGRAPH, text))
        return blocks, 1

    # ------------------------------------------------------------------------------ TEXT
    def _parse_text(self, path: Path) -> tuple[list[Block], int]:
        return self._blocks_from_text(path.read_text(encoding="utf-8", errors="replace"), page=None), 1

    def _parse_json(self, path: Path) -> tuple[list[Block], int]:
        """Renders JSON as indented text.

        Deliberately not flattened into key-value pairs: nesting carries meaning, and a flattened
        `a.b.c = 1` loses the grouping that makes the document interpretable.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            return [Block(BlockType.PARAGRAPH, f"[invalid JSON: {exc}]")], 1
        return [Block(BlockType.CODE, json.dumps(data, indent=2)[:100_000])], 1

    # -------------------------------------------------------------------------- helpers
    @staticmethod
    def _blocks_from_text(text: str, page: int | None) -> list[Block]:
        """Splits raw text into heading and paragraph blocks using layout heuristics.

        Used for formats with no structural markup. The heuristics are intentionally cautious:
        misclassifying a paragraph as a heading corrupts the heading path that every chunk inherits,
        which is worse than missing a heading.
        """
        blocks: list[Block] = []
        for raw_para in re.split(r"\n\s*\n", text):
            para = raw_para.strip()
            if not para:
                continue

            lines = para.splitlines()
            if len(lines) == 1:
                line = lines[0].strip()
                is_heading = (
                    len(line) <= MAX_HEADING_CHARS
                    and not line.endswith((".", ",", ";", ":"))
                    and (
                        NUMBERED_HEADING.match(line)
                        or (line.istitle() and len(line.split()) <= 12)
                        or (line.isupper() and 3 <= len(line) <= MAX_HEADING_CHARS)
                    )
                )
                if is_heading:
                    level = 2 if NUMBERED_HEADING.match(line) else 3
                    blocks.append(Block(BlockType.HEADING, line, page=page, level=level))
                    continue

            blocks.append(Block(BlockType.PARAGRAPH, para, page=page))
        return blocks
