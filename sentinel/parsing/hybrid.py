"""Hybrid parsing strategies.

This is the module that turns three parser adapters into a cost-effective policy. The premise: on
a mixed corpus, most documents are handled perfectly by a free local library and a minority are
handled badly. Paying a cloud service per page for all of them wastes money; using only the local
parser silently loses the minority. Neither is acceptable, so route per document.

Four strategies, in increasing cost and sophistication:

``single``
    One parser, no fallback. The baseline you benchmark everything else against.

``hybrid_escalate`` (default)
    Parse locally. Score the result. Escalate to the cloud only when the score falls below
    threshold. On a corpus of native PDFs this costs almost nothing; on a corpus of scans it
    costs one wasted local parse per document, which is milliseconds.

``hybrid_merge``
    Parse with both, then take the best parts of each: prose from whichever scored higher on text
    quality, tables from whichever produced structurally sound grids. Slower and always pays cloud
    cost, but produces the best single artifact — appropriate for a small high-value corpus such
    as methodology documents that will be queried for years.

``race``
    Run both concurrently, take whichever finishes first with an acceptable score. Optimises
    latency rather than cost; useful for interactive upload paths where a user is waiting.

The escalation threshold is the one number worth tuning per corpus, and
`benchmarks/parser_bakeoff.py` exists to tune it with evidence rather than intuition.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import DEGRADED_MODE, log
from sentinel.core.registry import ProviderUnavailable, get_provider
from sentinel.core.resilience import ParseFailure
from sentinel.core.types import Block, BlockType, ParsedDocument
from sentinel.parsing.base import BaseParser, DocFormat, detect_format
from sentinel.parsing.quality import QualityReport, score_parse

# Formats where cloud parsing adds nothing: the structure is already in the file, so a layout model
# has nothing to recover and would only cost money and latency.
LOCAL_ONLY_FORMATS = {
    DocFormat.MARKDOWN,
    DocFormat.TEXT,
    DocFormat.JSON,
    DocFormat.CSV,
    DocFormat.HTML,
    DocFormat.XLSX,
}

# Formats where the local parser is structurally incapable and escalation is not worth attempting
# locally first. A scanned PDF has no text layer; running pdfplumber on it is a guaranteed miss.
CLOUD_PREFERRED_FORMATS = {DocFormat.PDF_SCANNED, DocFormat.IMAGE}


class HybridParser:
    """Routes a document to the right parser (or parsers) and returns the best result.

    Not a `BaseParser` subclass by design: it composes parsers rather than being one, and keeping
    it out of the registry prevents the confusing possibility of a hybrid strategy recursing into
    itself.
    """

    def __init__(
        self,
        strategy: str | None = None,
        local_provider: str = "local",
        cloud_provider: str | None = None,
        threshold: float | None = None,
    ):
        """
        Args:
            cloud_provider: Which cloud to escalate to. Defaults to the configured parse provider
                when that is a cloud, otherwise Azure — chosen as the default because its layout
                model returns markdown directly, which needs no reconstruction.
        """
        self.strategy = strategy or settings.parse_strategy
        self.local_provider = local_provider
        self.cloud_provider = cloud_provider or (
            settings.parse_provider if settings.parse_provider != "local" else "azure"
        )
        self.threshold = threshold if threshold is not None else settings.parse_quality_threshold

    # ------------------------------------------------------------------------------ entry
    async def parse(self, path: str | Path, **kwargs: Any) -> ParsedDocument:
        path = Path(path)
        fmt = detect_format(path)
        log.debug(
            "hybrid_entry",
            path=str(path),
            format=fmt.value,
            strategy=self.strategy,
            threshold=self.threshold,
            local=self.local_provider,
            cloud=self.cloud_provider,
        )

        if fmt in LOCAL_ONLY_FORMATS:
            log.debug("parse_route", path=str(path), route="local_only", format=fmt.value)
            return await self._parse_with(self.local_provider, path, **kwargs)

        if fmt in CLOUD_PREFERRED_FORMATS and self.strategy != "single":
            log.debug("parse_route", path=str(path), route="cloud_first", format=fmt.value)
            return await self._cloud_first(path, fmt, **kwargs)

        dispatch = {
            "single": self._single,
            "hybrid_escalate": self._escalate,
            "hybrid_merge": self._merge,
            "race": self._race,
        }
        handler = dispatch.get(self.strategy)
        if handler is None:
            raise ValueError(f"unknown parse strategy '{self.strategy}'")
        return await handler(path, fmt, **kwargs)

    # -------------------------------------------------------------------------- strategies
    async def _single(self, path: Path, fmt: DocFormat, **kwargs: Any) -> ParsedDocument:
        return await self._parse_with(settings.parse_provider, path, **kwargs)

    async def _escalate(self, path: Path, fmt: DocFormat, **kwargs: Any) -> ParsedDocument:
        """Local first; escalate only when quality is below threshold."""
        local_doc = await self._parse_with(self.local_provider, path, **kwargs)
        report = score_parse(local_doc)

        if report.score >= self.threshold:
            local_doc.meta["parse_route"] = "local"
            local_doc.meta["quality_score"] = report.score
            log.info("parse_accepted_local", path=str(path), quality=report.score)
            return local_doc

        log.info(
            "parse_escalating",
            path=str(path),
            quality=report.score,
            threshold=self.threshold,
            reasons=report.warnings[:3],
        )

        try:
            cloud_doc = await self._parse_with(self.cloud_provider, path, **kwargs)
        except (ProviderUnavailable, ParseFailure) as exc:
            # Degrading to the poor local parse beats failing ingestion entirely: a mediocre
            # document in the index is more useful than a missing one, provided it is flagged.
            DEGRADED_MODE.labels("parsing", "cloud_unavailable").inc()
            log.warning("parse_escalation_failed", path=str(path), error=str(exc)[:200])
            local_doc.meta.update(
                {"parse_route": "local_degraded", "quality_score": report.score, "degraded_reason": str(exc)[:200]}
            )
            local_doc.metrics.warnings.append("cloud escalation failed; using local parse")
            return local_doc

        cloud_report = score_parse(cloud_doc)
        winner, route = (
            (cloud_doc, "cloud") if cloud_report.score >= report.score else (local_doc, "local_after_failed_escalation")
        )
        winner.meta.update(
            {
                "parse_route": route,
                "quality_score": max(cloud_report.score, report.score),
                "local_quality": report.score,
                "cloud_quality": cloud_report.score,
            }
        )
        log.info("parse_escalated", path=str(path), route=route, local=report.score, cloud=cloud_report.score)
        return winner

    async def _merge(self, path: Path, fmt: DocFormat, **kwargs: Any) -> ParsedDocument:
        """Runs both parsers and assembles a document from the better half of each."""
        local_doc, cloud_doc = await asyncio.gather(
            self._parse_with(self.local_provider, path, **kwargs),
            self._parse_with(self.cloud_provider, path, **kwargs),
            return_exceptions=True,
        )

        if isinstance(cloud_doc, Exception):
            log.warning("merge_cloud_failed", path=str(path), error=str(cloud_doc)[:200])
            if isinstance(local_doc, Exception):
                raise ParseFailure(f"both parsers failed for {path}")
            return local_doc  # type: ignore
        if isinstance(local_doc, Exception):
            return cloud_doc  # type: ignore

        return self._merge_documents(local_doc, cloud_doc)  # type: ignore

    async def _race(self, path: Path, fmt: DocFormat, **kwargs: Any) -> ParsedDocument:
        """First acceptable result wins; the loser is cancelled.

        Optimises the latency an interactive uploader feels. Note that the cloud call still incurs
        cost even when cancelled, because billing starts at the service, not at your await.
        """
        tasks = {
            asyncio.create_task(self._parse_with(self.local_provider, path, **kwargs)): "local",
            asyncio.create_task(self._parse_with(self.cloud_provider, path, **kwargs)): "cloud",
        }
        results: list[tuple[str, ParsedDocument, QualityReport]] = []

        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name = tasks[task]
                    try:
                        doc = task.result()
                    except Exception as exc:
                        log.warning("race_branch_failed", branch=name, error=str(exc)[:200])
                        continue
                    report = score_parse(doc)
                    results.append((name, doc, report))
                    if report.score >= self.threshold:
                        doc.meta.update({"parse_route": f"race_{name}", "quality_score": report.score})
                        return doc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        if not results:
            raise ParseFailure(f"no parser produced a result for {path}")

        name, doc, report = max(results, key=lambda r: r[2].score)
        doc.meta.update({"parse_route": f"race_best_{name}", "quality_score": report.score})
        return doc

    async def _cloud_first(self, path: Path, fmt: DocFormat, **kwargs: Any) -> ParsedDocument:
        """For scans and images, where the local parser cannot succeed by construction."""
        try:
            doc = await self._parse_with(self.cloud_provider, path, **kwargs)
            doc.meta["parse_route"] = "cloud_first"
            return doc
        except (ProviderUnavailable, ParseFailure) as exc:
            DEGRADED_MODE.labels("parsing", "ocr_unavailable").inc()
            log.error("ocr_unavailable", path=str(path), error=str(exc)[:200])
            doc = await self._parse_with(self.local_provider, path, **kwargs)
            doc.meta.update({"parse_route": "local_no_ocr", "degraded_reason": str(exc)[:200]})
            doc.metrics.warnings.append("scanned document parsed without OCR; text will be sparse")
            return doc

    # --------------------------------------------------------------------------- merging
    @staticmethod
    def _merge_documents(local: ParsedDocument, cloud: ParsedDocument) -> ParsedDocument:
        """Combines two parses of the same document.

        Rules, derived from where each parser is reliably stronger:

        * **Prose** comes from whichever parse scored higher on word validity and text density.
          Mixing prose from both is not an option — you would get duplicated paragraphs with
          slightly different whitespace, which chunking cannot deduplicate.
        * **Tables** come from whichever produced more structurally sound grids, judged by the
          table component of the quality score. Table extraction is the dimension where the two
          diverge most, and it is independent of prose quality.
        * **Figures** are unioned, because a figure found by either parser is a figure that exists,
          and duplicates are cheap to filter by page and bounding box.
        """
        local_report = score_parse(local)
        cloud_report = score_parse(cloud)

        prose_source = (
            cloud
            if (cloud_report.word_validity + cloud_report.text_density)
            >= (local_report.word_validity + local_report.text_density)
            else local
        )
        table_source = cloud if cloud_report.table_score >= local_report.table_score else local

        blocks: list[Block] = [b for b in prose_source.blocks if b.type not in {BlockType.TABLE, BlockType.FIGURE}]
        blocks.extend(b for b in table_source.blocks if b.type is BlockType.TABLE)

        seen_figures: set[tuple[int | None, int]] = set()
        for source in (cloud, local):
            for block in source.blocks:
                if block.type is not BlockType.FIGURE or not block.figure:
                    continue
                key = (block.figure.page, int((block.figure.bbox or (0,))[0] * 1000))
                if key in seen_figures:
                    continue
                seen_figures.add(key)
                blocks.append(block)

        merged = ParsedDocument(
            doc_id=cloud.doc_id,
            source_uri=cloud.source_uri,
            title=cloud.title or local.title,
            blocks=blocks,
            metrics=cloud.metrics,
            doc_type=cloud.doc_type,
            tenant_id=cloud.tenant_id,
            meta={
                **local.meta,
                **cloud.meta,
                "parse_route": "merged",
                "prose_from": prose_source.metrics.provider,
                "tables_from": table_source.metrics.provider,
                "local_quality": local_report.score,
                "cloud_quality": cloud_report.score,
            },
        )
        merged.metrics.provider = f"merge({local.metrics.provider}+{cloud.metrics.provider})"
        merged.metrics.usd_cost = local.metrics.usd_cost + cloud.metrics.usd_cost
        merged.metrics.duration_s = max(local.metrics.duration_s, cloud.metrics.duration_s)
        merged.metrics.tables = sum(1 for b in blocks if b.type is BlockType.TABLE)
        merged.metrics.figures = sum(1 for b in blocks if b.type is BlockType.FIGURE)

        score_parse(merged)
        log.info(
            "parse_merged",
            doc=merged.doc_id,
            prose_from=prose_source.metrics.provider,
            tables_from=table_source.metrics.provider,
            quality=merged.metrics.quality_score,
        )
        return merged

    # --------------------------------------------------------------------------- helper
    async def _parse_with(self, provider: str, path: Path, **kwargs: Any) -> ParsedDocument:
        parser: BaseParser = get_provider("parser", provider)
        started = time.perf_counter()
        doc = await parser.aparse(path, **kwargs)
        doc.meta.setdefault("parse_seconds", round(time.perf_counter() - started, 3))
        return doc


async def parse_document(path: str | Path, **kwargs: Any) -> ParsedDocument:
    """Convenience entry point used by the ingestion pipeline."""
    return await HybridParser().parse(path, **kwargs)
