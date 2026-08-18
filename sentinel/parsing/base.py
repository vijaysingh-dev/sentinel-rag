"""Parser interface and format detection.

Every parser adapter returns a `ParsedDocument` with structure preserved as `Block`s. That uniform
output is what makes the hybrid strategies in `hybrid.py` possible: you cannot merge or compare two
parsers whose outputs have different shapes.

A note on scope, because it is the thing most people get wrong: **parsing is stage one and it caps
everything downstream.** No chunking strategy, embedding model or reranker recovers a table that
was flattened into a column of orphaned numbers, or body text that was interleaved with a running
header on every page. Before tuning anything else in a RAG pipeline, print the raw extracted text
for five documents and read it.
"""

from __future__ import annotations

import hashlib
import mimetypes
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any

from sentinel.core.types import ParsedDocument


class DocFormat(StrEnum):
    """Formats the pipeline knows how to handle.

    The distinction between PDF_NATIVE and PDF_SCANNED drives routing: a native PDF has an
    extractable text layer and a local library handles it perfectly for free, while a scanned one
    is an image and needs OCR from a cloud service. Treating them as one format means either
    overpaying on every native PDF or returning empty text for every scan.
    """

    PDF_NATIVE = "pdf_native"
    PDF_SCANNED = "pdf_scanned"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    CSV = "csv"
    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"
    JSON = "json"
    IMAGE = "image"
    UNKNOWN = "unknown"


EXTENSION_MAP: dict[str, DocFormat] = {
    ".pdf": DocFormat.PDF_NATIVE,  # refined by probing the text layer
    ".docx": DocFormat.DOCX,
    ".doc": DocFormat.DOCX,
    ".pptx": DocFormat.PPTX,
    ".ppt": DocFormat.PPTX,
    ".xlsx": DocFormat.XLSX,
    ".xls": DocFormat.XLSX,
    ".xlsm": DocFormat.XLSX,
    ".csv": DocFormat.CSV,
    ".tsv": DocFormat.CSV,
    ".md": DocFormat.MARKDOWN,
    ".markdown": DocFormat.MARKDOWN,
    ".html": DocFormat.HTML,
    ".htm": DocFormat.HTML,
    ".txt": DocFormat.TEXT,
    ".log": DocFormat.TEXT,
    ".rst": DocFormat.TEXT,
    ".json": DocFormat.JSON,
    ".jsonl": DocFormat.JSON,
    ".png": DocFormat.IMAGE,
    ".jpg": DocFormat.IMAGE,
    ".jpeg": DocFormat.IMAGE,
    ".tiff": DocFormat.IMAGE,
    ".tif": DocFormat.IMAGE,
    ".bmp": DocFormat.IMAGE,
}

# Below this many extractable characters per page, a PDF is treated as scanned. Chosen because a
# genuinely text-bearing page almost always exceeds it, while a scanned page yields only stray
# artefacts from embedded stamps or watermarks.
NATIVE_TEXT_THRESHOLD_PER_PAGE = 100


def detect_format(path: str | Path) -> DocFormat:
    """Determines format from extension, then probes PDFs for a usable text layer.

    Extension alone is not enough for PDFs, which is the only format where the same suffix covers
    two operationally different things.
    """
    path = Path(path)
    fmt = EXTENSION_MAP.get(path.suffix.lower())

    if fmt is None:
        guessed, _ = mimetypes.guess_type(str(path))
        if guessed and guessed.startswith("text/"):
            fmt = DocFormat.TEXT
        else:
            return DocFormat.UNKNOWN

    if fmt is DocFormat.PDF_NATIVE:
        return DocFormat.PDF_NATIVE if _pdf_has_text_layer(path) else DocFormat.PDF_SCANNED
    return fmt


def _pdf_has_text_layer(path: Path, sample_pages: int = 5) -> bool:
    """Samples the first few pages for extractable text.

    Sampling rather than reading the whole document keeps detection cheap on large files, and the
    first pages are representative in practice: mixed documents (a native report with scanned
    appendices) are better handled by per-page routing inside the parser than by a whole-document
    verdict here.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = reader.pages[:sample_pages]
        if not pages:
            return False
        chars = sum(len((p.extract_text() or "").strip()) for p in pages)
        return chars / len(pages) >= NATIVE_TEXT_THRESHOLD_PER_PAGE
    except Exception:
        # If probing fails, assume native: the local parser will produce a low quality score and
        # the hybrid strategy escalates to a cloud OCR service anyway.
        return True


def doc_id_for(source_uri: str) -> str:
    """Stable document identifier derived from the source location.

    Path-derived rather than content-derived so that an edited document keeps its identity and its
    chunks can be diffed and superseded, rather than appearing as an unrelated new document.
    """
    return hashlib.sha256(str(source_uri).encode()).hexdigest()[:16]


class BaseParser(ABC):
    """Contract every parser adapter implements."""

    name: str = "base"
    supported: set[DocFormat] = set()

    @abstractmethod
    def parse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        """Extracts structured content from a file."""

    def supports(self, fmt: DocFormat) -> bool:
        return fmt in self.supported

    def can_parse(self, path: str | Path) -> bool:
        return self.supports(detect_format(path))

    async def aparse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        """Async wrapper.

        Defaults to running the synchronous implementation in a thread so that CPU-bound local
        parsing does not block the event loop during concurrent ingestion. Adapters with genuinely
        async SDK calls override this.
        """
        import asyncio

        return await asyncio.to_thread(self.parse, path, **kwargs)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name}>"
