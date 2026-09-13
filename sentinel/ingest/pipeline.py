"""End-to-end ingestion: file in, indexed chunks out.

    discover -> parse (hybrid) -> enrich figures -> chunk -> ACL tag -> embed -> upsert -> tombstone

The properties that make this safe to run repeatedly, which together are the answer to "how do you
keep the index in sync with changing documents":

* **Content-derived chunk ids** — unchanged text produces an identical id, so re-running is a
  no-op and citation links stay valid across re-ingestion.
* **Orphan tombstoning** — chunks that no longer exist in the source are deleted. Skipping this is
  how systems end up confidently citing a superseded runbook, which in an operations context is
  worse than returning nothing.
* **Delta embedding** — only new or changed chunks are embedded. Editing one paragraph of a
  90-page PDF re-embeds one chunk, not four hundred.
* **Dead-letter queue** — a document that fails parsing is recorded, not silently skipped. A job
  that "succeeds" while quietly dropping 12% of the corpus is the failure nobody notices.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel.chunking.strategies import chunker_for_doc
from sentinel.core.config import settings
from sentinel.core.observability import log, stage_timer
from sentinel.core.registry import get_provider
from sentinel.core.types import Chunk, ParsedDocument
from sentinel.ingest.acl import AclResolver
from sentinel.parsing.hybrid import HybridParser

SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".csv",
    ".tsv",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".txt",
    ".json",
}


@dataclass
class IngestStats:
    documents: int = 0
    chunks_new: int = 0
    chunks_deleted: int = 0
    chunks_unchanged: int = 0
    failures: list[dict] = field(default_factory=list)
    usd_cost: float = 0.0
    duration_s: float = 0.0
    parse_routes: dict[str, int] = field(default_factory=dict)

    def merge(self, other: IngestStats) -> None:
        self.documents += other.documents
        self.chunks_new += other.chunks_new
        self.chunks_deleted += other.chunks_deleted
        self.chunks_unchanged += other.chunks_unchanged
        self.failures.extend(other.failures)
        self.usd_cost += other.usd_cost
        for route, count in other.parse_routes.items():
            self.parse_routes[route] = self.parse_routes.get(route, 0) + count


class IngestionPipeline:
    def __init__(
        self,
        acl_rules: str | Path | None = "config/acl_rules.yaml",
        enrich_figures: bool = False,
        contextual: bool | None = None,
        dlq_path: str | Path = "data/dlq.jsonl",
    ):
        self.parser = HybridParser()
        self.embedder = get_provider("embedder")
        self.store = get_provider("vectorstore")
        self.acl = AclResolver(acl_rules)
        self.enrich_figures = enrich_figures
        self.contextual = settings.contextual_chunking if contextual is None else contextual
        self.dlq_path = Path(dlq_path)

    def prepare(self, recreate: bool = False) -> None:
        self.store.ensure_index(self.embedder.dims, recreate=recreate)

    # ------------------------------------------------------------------ single document
    async def ingest_file(
        self,
        path: str | Path,
        doc_type: str = "page",
        tenant_id: str = "default",
        chunk_strategy: str | None = None,
        on_stage: Callable[[str, Any], None] | None = None,
    ) -> IngestStats:
        """
        Args:
            on_stage: Optional hook invoked as `on_stage(stage, value)` right after each stage
                produces its real object — `("parsed", ParsedDocument)`, `("chunked", list[Chunk])`,
                `("written", IngestStats)` — for callers that want more than the aggregate
                `IngestStats` this method returns (`scripts/ingest/01_ingest_traced.py` uses it to
                write per-document trace artefacts). `None` by default and never called on the
                normal path, so `make ingest` pays nothing for it.
        """
        path = Path(path)
        stats = IngestStats()
        started = time.perf_counter()

        try:
            doc = await self.parser.parse(path, doc_type=doc_type, tenant_id=tenant_id)
        except Exception as exc:
            self._dead_letter(path, "parse", exc)
            stats.failures.append(
                {"path": str(path), "stage": "parse", "error": type(exc).__name__, "detail": str(exc)[:300]}
            )
            return stats
        if on_stage:
            on_stage("parsed", doc)

        route = doc.meta.get("parse_route", "unknown")
        stats.parse_routes[route] = stats.parse_routes.get(route, 0) + 1
        stats.usd_cost += doc.metrics.usd_cost

        if self.enrich_figures and doc.metrics.figures:
            try:
                from sentinel.parsing.figures import enrich_figures as run_enrich

                doc = await run_enrich(doc, get_provider("llm").vision)
            except Exception as exc:
                # Figure enrichment is an enhancement, not a requirement; a failure here must not
                # cost us the whole document.
                log.warning("figure_enrichment_failed", doc=doc.doc_id, error=str(exc)[:200])

        chunks = await self._chunk(doc, chunk_strategy)
        if not chunks:
            log.warning("no_chunks_produced", path=str(path), quality=doc.metrics.quality_score)
            return stats
        if on_stage:
            on_stage("chunked", chunks)

        roles, components = self.acl.resolve(doc.source_uri, doc.doc_type, doc.meta)
        for chunk in chunks:
            chunk.allowed_roles = roles
            chunk.components = components

        stats.merge(self._write(doc, chunks))
        stats.documents = 1
        stats.duration_s = time.perf_counter() - started
        if on_stage:
            on_stage("written", stats)

        log.info(
            "document_ingested",
            path=str(path),
            doc_id=doc.doc_id,
            route=route,
            chunks_new=stats.chunks_new,
            chunks_deleted=stats.chunks_deleted,
            roles=roles,
            quality=doc.metrics.quality_score,
            usd=round(stats.usd_cost, 5),
        )
        return stats

    async def _chunk(self, doc: ParsedDocument, override: str | None) -> list[Chunk]:
        chunker = chunker_for_doc(doc, override)

        if self.contextual:
            from sentinel.chunking.strategies import ContextualChunker

            wrapper = ContextualChunker(chunker, llm=get_provider("llm").cheap)
            with stage_timer("chunking", wrapper.name, doc=doc.doc_id):
                return await wrapper.asplit(doc)

        with stage_timer("chunking", chunker.name, doc=doc.doc_id):
            return chunker.split(doc)

    def _write(self, doc: ParsedDocument, chunks: list[Chunk]) -> IngestStats:
        """Delta upsert plus orphan tombstoning."""
        stats = IngestStats()

        existing = self.store.chunk_ids_for_doc(doc.doc_id)
        incoming = {c.chunk_id for c in chunks}

        new_chunks = [c for c in chunks if c.chunk_id not in existing]
        stats.chunks_unchanged = len(incoming & existing)

        if new_chunks:
            with stage_timer("embedding", self.embedder.name, count=len(new_chunks)):
                embedded = self.embedder.embed_chunks(new_chunks)
            with stage_timer("upsert", self.store.name, count=len(embedded)):
                stats.chunks_new = self.store.upsert(embedded)

        orphans = list(existing - incoming)
        if orphans:
            stats.chunks_deleted = self.store.delete(orphans)

        return stats

    # -------------------------------------------------------------------- bulk ingestion
    async def ingest_directory(
        self,
        root: str | Path,
        doc_type: str | None = None,
        tenant_id: str = "default",
        concurrency: int = 4,
    ) -> IngestStats:
        """Walks a tree and ingests every supported file.

        When `doc_type` is omitted, the immediate parent directory name is used — so
        `knowledge/runbook/*.md` becomes doc_type "runbook", which in turn selects the runbook
        chunking strategy. Convention over configuration, and it keeps the corpus layout
        self-documenting.
        """
        root = Path(root)
        paths = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES]

        if not paths:
            log.warning("no_documents_found", root=str(root))
            return IngestStats()

        semaphore = asyncio.Semaphore(concurrency)
        total = IngestStats()
        started = time.perf_counter()

        async def run(path: Path) -> IngestStats:
            async with semaphore:
                resolved_type = doc_type or path.parent.name
                return await self.ingest_file(path, resolved_type, tenant_id)

        for result in await asyncio.gather(*(run(p) for p in paths), return_exceptions=True):
            if isinstance(result, Exception):
                total.failures.append({"stage": "pipeline", "error": str(result)[:300]})
                continue
            total.merge(result)

        total.duration_s = time.perf_counter() - started

        log.info(
            "ingestion_complete",
            root=str(root),
            documents=total.documents,
            chunks_new=total.chunks_new,
            chunks_deleted=total.chunks_deleted,
            unchanged=total.chunks_unchanged,
            failures=len(total.failures),
            usd=round(total.usd_cost, 4),
            duration_s=round(total.duration_s, 1),
            routes=total.parse_routes,
            index_version=settings.index_version,
        )

        if total.failures:
            log.warning("ingestion_failures", count=len(total.failures), dlq=str(self.dlq_path))
        return total

    def _dead_letter(self, path: Path, stage: str, exc: Exception) -> None:
        """Records a failure for later inspection rather than dropping it silently."""
        self.dlq_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "path": str(path),
            "stage": stage,
            "error": type(exc).__name__,
            "detail": str(exc)[:1000],
            "timestamp": time.time(),
            "index_version": settings.index_version,
        }
        with self.dlq_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        log.error("document_failed", path=str(path), stage=stage, error=type(exc).__name__)
