"""Figure enrichment: making diagrams retrievable.

An architecture diagram contributes nothing to a text index. In a corpus of design decisions that
is a serious gap, because the rationale is often *in* the picture and the surrounding prose says
little more than "see figure 3".

The fix is to describe each figure with a vision model at ingest time and index the description,
keeping a pointer to the original image so an answer can link to it rather than only paraphrasing.

Cost control matters here because a slide deck can contain hundreds of images, most of them logos
and decorative rules:

* Small regions are skipped before any model call (`MIN_FIGURE_AREA`).
* Descriptions are cached by image content hash, so re-ingesting a document is free.
* Surrounding text is passed as context, which both improves the description and lets the model
  say "this restates the table above" rather than inventing detail.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from pathlib import Path
from typing import Any

from sentinel.core.cache import backend
from sentinel.core.observability import log, record_tokens
from sentinel.core.types import Block, BlockType, ParsedDocument

# Below this pixel area a region is almost certainly a logo, icon or ruling line.
MIN_FIGURE_AREA = 120 * 120

FIGURE_PROMPT = """This is a figure from technical documentation for a microservices e-commerce
platform (the OpenTelemetry Demo, themed here as Astroshop).

Describe it as searchable text:
- Name every component, label and annotation visible in the figure.
- Describe the direction of data flow or the relationship between components.
- State what the figure appears to explain.

Do not speculate beyond what is visible. If the figure is decorative (a logo, a divider, a stock
photograph) reply with exactly: DECORATIVE

Plain prose, no preamble, no markdown."""


def _image_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:24]


def _cache_key(image_hash: str, model: str) -> str:
    return f"figdesc:{model}:{image_hash}"


async def describe_figure(
    image_path: Path,
    llm: Any,
    doc_title: str = "",
    nearby_text: str = "",
    model_name: str = "vision",
) -> str | None:
    """Returns a text description, or None when the figure is decorative or fails.

    Returning None rather than an empty string is deliberate: the caller distinguishes "described
    as nothing" from "not worth describing", and only the latter is dropped from the index.
    """
    try:
        image_hash = _image_hash(image_path)
    except OSError as exc:
        log.warning("figure_unreadable", path=str(image_path), error=str(exc)[:120])
        return None

    key = _cache_key(image_hash, model_name)
    try:
        cached = backend().get(key)
        if cached is not None:
            return None if cached == "DECORATIVE" else cached
    except Exception:
        pass

    payload = base64.b64encode(image_path.read_bytes()).decode()
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    context = f"Document: {doc_title}\nSurrounding text: {nearby_text[:800]}" if doc_title else ""

    try:
        response = await llm.ainvoke(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{FIGURE_PROMPT}\n\n{context}"},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{payload}"}},
                    ],
                }
            ]
        )
    except Exception as exc:
        log.warning("figure_description_failed", path=str(image_path), error=str(exc)[:160])
        return None

    text = (response.content or "").strip()

    usage = getattr(response, "usage_metadata", None) or {}
    if usage:
        record_tokens(
            model_name, usage.get("input_tokens", 0), usage.get("output_tokens", 0), stage="figure_enrichment"
        )

    with contextlib.suppress(Exception):
        backend().set(key, text or "DECORATIVE")

    return None if text.upper().startswith("DECORATIVE") or not text else text


async def enrich_figures(
    doc: ParsedDocument,
    llm: Any,
    max_concurrent: int = 4,
    model_name: str = "vision",
) -> ParsedDocument:
    """Describes every substantive figure in a document, in place.

    Figures without a saved image (the parser recorded a bounding box but no file) are left alone;
    a description cannot be produced from coordinates. Decorative figures are removed entirely so
    they do not become empty chunks.
    """
    targets: list[tuple[int, Block, Path]] = []
    for i, block in enumerate(doc.blocks):
        if block.type is not BlockType.FIGURE or not block.figure:
            continue
        uri = block.figure.image_uri
        if not uri:
            continue
        path = Path(uri)
        if not path.exists():
            continue
        if block.figure.bbox:
            x0, y0, x1, y1 = block.figure.bbox
            if abs((x1 - x0) * (y1 - y0)) < MIN_FIGURE_AREA:
                continue
        targets.append((i, block, path))

    if not targets:
        return doc

    semaphore = asyncio.Semaphore(max_concurrent)

    async def run(index: int, block: Block, path: Path) -> tuple[int, str | None]:
        async with semaphore:
            nearby = _nearby_text(doc, index)
            description = await describe_figure(path, llm, doc.title, nearby, model_name)
            return index, description

    results = await asyncio.gather(*(run(i, b, p) for i, b, p in targets))

    decorative: set[int] = set()
    described = 0
    for index, description in results:
        block = doc.blocks[index]
        if description is None:
            decorative.add(index)
            continue
        block.figure.description = description  # type: ignore[union-attr]
        # The description becomes the block's text so chunkers treat it as ordinary content.
        block.text = description
        described += 1

    if decorative:
        doc.blocks = [b for i, b in enumerate(doc.blocks) if i not in decorative]

    doc.metrics.figures = sum(1 for b in doc.blocks if b.type is BlockType.FIGURE)
    log.info("figures_enriched", doc=doc.doc_id, described=described, dropped_decorative=len(decorative))
    return doc


def _nearby_text(doc: ParsedDocument, index: int, window: int = 2) -> str:
    """Collects text immediately around a figure to give the vision model context.

    A caption alone is usually too thin; the paragraph that introduces the figure carries the
    vocabulary that makes the description retrievable by realistic queries.
    """
    lo = max(0, index - window)
    hi = min(len(doc.blocks), index + window + 1)
    return " ".join(b.text for b in doc.blocks[lo:hi] if b.type in {BlockType.PARAGRAPH, BlockType.HEADING} and b.text)
