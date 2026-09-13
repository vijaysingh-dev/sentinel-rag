"""Full-visibility trace output for a traced ingestion run.

`IngestionPipeline.ingest_file` already does the real work (parse -> chunk -> ACL tag -> embed ->
upsert); this module wraps one call to it per document and writes what each stage actually
produced to `outputs/<stage>/<doc-stem>/`, the same convention `scripts/parsing/_trace.py` and
`scripts/chunking/01_chunk_compare.py` use standalone.

Two complementary sources, because no single one covers every stage:

* **`on_stage` callback.** `ingest_file(..., on_stage=...)` hands back the real `ParsedDocument`
  after parsing and the real `list[Chunk]` after chunking — see the parameter's docstring in
  `sentinel/ingest/pipeline.py`. This is what lets `parsing/hybrid.parsed.json` and
  `chunking/<strategy>.chunks.jsonl` exist at all; without it this script could only report counts,
  not the objects themselves.
* **`structlog.testing.capture_logs`**, for embedding and vectorstore. Those two stages don't
  produce an object worth dumping (a list of floats per chunk is not something you read), so their
  trace is built from the `stage_complete`/`stage_failed` events `core.observability.stage_timer`
  already emits — provider, chunk counts, duration, cost.

Layout, one folder per stage, one subfolder per document — `outputs/<stage>/<doc-stem>/`:

``parsing/<doc-stem>/hybrid.trace.log`` / ``.parsed.json`` / ``.content.md``
    The `ParsedDocument` from `on_stage("parsed", doc)`, plus every `HybridParser.parse` debug
    event captured during the run (format detection, quality scoring, the route decision).

``chunking/<doc-stem>/<strategy>.log`` / ``.chunks.jsonl``
    The `list[Chunk]` from `on_stage("chunked", chunks)` — `chunker.name` names the file, since
    `chunker_for_doc` already decided which strategy this doc got. One `dataclasses.asdict(Chunk)`
    per line in the `.jsonl`, same shape as `scripts/chunking/01_chunk_compare.py`.

``embedding/<doc-stem>/<provider>.trace.log``
    `stage_complete`/`stage_failed` events tagged `stage=embedding` — how many chunks were
    actually embedded (delta embedding skips unchanged ones, see `IngestionPipeline._write`),
    provider, duration.

``vectorstore/<doc-stem>/<provider>.trace.log``
    Same, tagged `stage=upsert` — chunks written, plus `index_ready` if this run created or
    recreated the index.

``ingest/<doc-stem>/summary.json``
    The `IngestStats` from `on_stage("written", stats)` — chunks new/unchanged/deleted, cost,
    duration for this one document. Kept per-document (not just the corpus-wide total `make
    ingest` prints) so one degraded or failing doc doesn't hide inside an average.

``outputs/`` is gitignored — these are artefacts of a run, not fixtures.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OUTPUT_ROOT = Path("outputs")

# Mirrors scripts/parsing/_trace.py's _TRACED_EVENTS — the HybridParser.parse debug events worth
# keeping. Kept as a separate copy rather than importing across script directories, consistent
# with how the other per-stage scripts are structured (each self-contained, no cross-imports).
_PARSE_EVENTS = {
    "format_detected",
    "pdf_text_layer_probe",
    "pdf_text_layer_probe_failed",
    "local_dispatch",
    "pdf_page_extracted",
    "local_blocks_built",
    "textract_route",
    "textract_response",
    "textract_reconstruction",
    "textract_assembled",
    "textract_uploaded",
    "azure_result",
    "azure_page_limited",
    "quality_components",
    "hybrid_entry",
    "parse_route",
    "parse_accepted_local",
    "parse_escalating",
    "parse_escalated",
    "parse_escalation_failed",
    "parse_merged",
    "parsed",
}


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _write_log(path: Path, header: str, events: list[dict[str, Any]]) -> None:
    lines = ["=" * 88, header, f"  generated: {datetime.now(UTC).isoformat()}", "=" * 88, ""]
    for entry in events:
        fields = {k: v for k, v in entry.items() if k not in {"event", "log_level", "level", "timestamp"}}
        rendered = "  ".join(f"{k}={v}" for k, v in fields.items())
        lines.append(f"log:{entry.get('event', '?'):<20}  {rendered}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class IngestTracer:
    """One instance per document. `run()` wraps `ingest_file`, `on_stage` feeds it real objects."""

    # stage_timer's `stage=` values (see pipeline.py) mapped to the output subfolder they land in.
    _LOG_STAGE_DIRS = {"embedding": "embedding", "upsert": "vectorstore"}

    def __init__(self, doc_stem: str):
        self.doc_stem = doc_stem
        self._captured: list[dict[str, Any]] = []
        self._parsed: Any = None
        self._chunks: Any = None
        self._stats: Any = None

    def on_stage(self, stage: str, value: Any) -> None:
        if stage == "parsed":
            self._parsed = value
        elif stage == "chunked":
            self._chunks = value
        elif stage == "written":
            self._stats = value

    @contextmanager
    def capture(self) -> Iterator[None]:
        from structlog.testing import capture_logs

        with capture_logs() as entries:
            try:
                yield
            finally:
                self._captured.extend(dict(e) for e in entries)

    def write(self) -> list[Path]:
        """Writes every artefact this run has material for. Returns the paths actually written."""
        written: list[Path] = []

        parse_events = [e for e in self._captured if e.get("event") in _PARSE_EVENTS]
        if self._parsed is not None or parse_events:
            parse_dir = OUTPUT_ROOT / "parsing" / self.doc_stem
            _write_log(parse_dir / "hybrid.trace.log", f"parsing trace — {self.doc_stem}", parse_events)
            written.append(parse_dir / "hybrid.trace.log")
            if self._parsed is not None:
                (parse_dir / "hybrid.parsed.json").write_text(
                    json.dumps(_to_jsonable(self._parsed), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                (parse_dir / "hybrid.content.md").write_text(self._parsed.markdown, encoding="utf-8")
                written += [parse_dir / "hybrid.parsed.json", parse_dir / "hybrid.content.md"]

        if self._chunks:
            strategy = self._chunks[0].strategy or "unknown"
            chunk_dir = OUTPUT_ROOT / "chunking" / self.doc_stem
            chunk_dir.mkdir(parents=True, exist_ok=True)
            log_lines = [
                "=" * 88,
                f"chunking trace — {self.doc_stem} — strategy={strategy}",
                f"  chunks: {len(self._chunks)}",
                "=" * 88,
                "",
            ]
            for i, c in enumerate(self._chunks):
                log_lines.append(f"[{i:03d}] {c.chunk_id}  tokens={c.token_count}  heading={c.heading_path!r}")
            (chunk_dir / f"{strategy}.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            with (chunk_dir / f"{strategy}.chunks.jsonl").open("w", encoding="utf-8") as fh:
                for c in self._chunks:
                    fh.write(json.dumps(asdict(c), ensure_ascii=False, default=str) + "\n")
            written += [chunk_dir / f"{strategy}.log", chunk_dir / f"{strategy}.chunks.jsonl"]

        by_stage: dict[str, list[dict[str, Any]]] = {}
        for entry in self._captured:
            target = self._LOG_STAGE_DIRS.get(entry.get("stage", ""))
            if target:
                by_stage.setdefault(target, []).append(entry)
        for target, events in by_stage.items():
            provider = events[-1].get("provider", "unknown")
            path = OUTPUT_ROOT / target / self.doc_stem / f"{provider}.trace.log"
            _write_log(path, f"{target} trace — {self.doc_stem} — provider={provider}", events)
            written.append(path)

        if self._stats is not None:
            summary_dir = OUTPUT_ROOT / "ingest" / self.doc_stem
            summary_dir.mkdir(parents=True, exist_ok=True)
            summary_path = summary_dir / "summary.json"
            summary_path.write_text(
                json.dumps(_to_jsonable(self._stats), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            written.append(summary_path)

        return written
