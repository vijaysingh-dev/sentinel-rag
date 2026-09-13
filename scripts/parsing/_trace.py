"""Full-visibility trace output for the parsing demo scripts.

The demo scripts print a summary to the terminal. That is enough to answer "did it work",
but not "what did each step actually produce" — which is the question you are asking when
you have been handed the parsing stage and told to improve it.

This module writes three artefacts per run to ``outputs/parsing/<source-stem>/``:

``<provider>.trace.log``
    A step-by-step, human-readable log of the parse: format detection and the text-layer
    probe, the handler that was dispatched, per-page extraction counts, block-building
    strategy, the quality score with every component broken out, and — for the hybrid
    strategy — the route decision and why. This is the file to read first.

``<provider>.parsed.json``
    ``dataclasses.asdict(ParsedDocument)`` — every block, table grid, figure, bbox, the
    metrics, and ``meta``. The machine-readable ground truth for "what is in the object".

``<provider>.content.md``
    ``doc.markdown`` — the reconstructed document as the chunker will see it. Diff this
    across providers to see, concretely, what escalating to a cloud parser buys you.

The trace log is assembled from two sources: explicit ``tracer.step(...)`` calls in the
script, and the structured ``log`` events the parsing modules emit (captured with
``structlog.testing.capture_logs`` for the duration of the parse). Both end up interleaved
in call order, so the log reads as one narrative.

``outputs/`` is gitignored — these are artefacts of a run, not fixtures.
"""

from __future__ import annotations

import dataclasses
import json
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel.core.types import ParsedDocument
from sentinel.parsing.quality import QualityReport

OUTPUT_ROOT = Path("outputs")

# The parsing-module log events worth folding into the trace. Anything else (a cache backend,
# an HTTP client) is noise for this purpose.
_TRACED_EVENTS = {
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
    """Recursively convert dataclasses / tuples / sets so ``json.dumps`` accepts them."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    return obj


class ParseTracer:
    """Collects trace steps and captured log events, then writes the three artefacts.

    One tracer per script run. ``capture()`` wraps the actual ``parse()`` call so the
    module-level ``log.debug`` events land in the same timeline as the script's own steps.
    """

    def __init__(self, source_path: str | Path, provider: str):
        self.source_path = Path(source_path)
        self.provider = provider
        self.out_dir = OUTPUT_ROOT / "parsing" / self.source_path.stem
        self._lines: list[str] = []
        self._t0 = time.perf_counter()
        self._captured: list[dict[str, Any]] = []

        self._header(
            f"parse trace — {self.source_path.name} — provider={provider}",
            started=datetime.now(UTC).isoformat(),
        )

    # ------------------------------------------------------------------ trace assembly
    def _elapsed(self) -> str:
        return f"+{time.perf_counter() - self._t0:7.3f}s"

    def _header(self, title: str, **fields: Any) -> None:
        self._lines.append("=" * 88)
        self._lines.append(title)
        for k, v in fields.items():
            self._lines.append(f"  {k}: {v}")
        self._lines.append("=" * 88)

    def section(self, title: str) -> None:
        self._lines.append("")
        self._lines.append(f"── {title} " + "─" * max(0, 84 - len(title)))

    def step(self, event: str, **fields: Any) -> None:
        """Record one explicit step in the script's control flow. ``None`` fields are dropped."""
        rendered = "  ".join(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)
        self._lines.append(f"{self._elapsed()}  {event:<28}  {rendered}")

    def note(self, text: str) -> None:
        for line in textwrap.wrap(text, width=84) or [""]:
            self._lines.append(f"{'':>11}  # {line}")

    def obj(self, label: str, value: Any) -> None:
        """Dump an object (dataclass, dict, ...) into the trace, pretty-printed."""
        self._lines.append(f"{self._elapsed()}  {label}")
        blob = json.dumps(_to_jsonable(value), indent=2, ensure_ascii=False)
        for line in blob.splitlines():
            self._lines.append(f"{'':>13}{line}")

    # ------------------------------------------------------------------ log capture
    @contextmanager
    def capture(self) -> Iterator[None]:
        """Fold the parsing modules' ``log`` events into the trace timeline.

        Uses ``structlog.testing.capture_logs``, which swaps in a capture processor for the
        duration and correctly bypasses the module-level lazy-proxy loggers' caching. It also
        silences console output while active, so the parse runs quietly and every event lands
        in the trace file instead.
        """
        from structlog.testing import capture_logs

        with capture_logs() as entries:
            try:
                yield
            finally:
                for entry in entries:
                    event = entry.get("event", "")
                    if event not in _TRACED_EVENTS:
                        continue
                    self._captured.append(dict(entry))
                    fields = {k: v for k, v in entry.items() if k not in {"event", "log_level", "level", "timestamp"}}
                    rendered = "  ".join(f"{k}={_fmt(v)}" for k, v in fields.items())
                    # capture_logs gives no timestamps; order is call order, which is what matters.
                    self._lines.append(f"{'':>11}  log:{event:<24}  {rendered}")

    # ------------------------------------------------------------------ finalisation
    def finish(self, doc: ParsedDocument, report: QualityReport) -> Path:
        """Append the resolved document + quality report and write all three files."""
        # A doc may have been scored more than once upstream (the parser scores internally, the
        # hybrid strategy re-scores). Collapse the duplicated warning list before dumping it.
        doc.metrics.warnings[:] = list(dict.fromkeys(doc.metrics.warnings))

        self.section("resolved ParsedDocument")
        self.step(
            "document",
            doc_id=doc.doc_id,
            title=doc.title,
            doc_type=doc.doc_type,
            tenant=doc.tenant_id,
            blocks=len(doc.blocks),
        )
        self.obj("metrics =", doc.metrics)
        self.obj("meta =", doc.meta)

        self.section("blocks (reading order)")
        for i, b in enumerate(doc.blocks):
            head = f"[{i:03d}] {b.type.value:<9} p{b.page if b.page is not None else '-'}"
            if b.type.value == "heading":
                head += f" h{b.level}"
            if b.table:
                head += f" grid={b.table.rows}x{b.table.cols}"
                if b.table.confidence is not None:
                    head += f" conf={b.table.confidence:.1f}"
            if b.figure:
                head += f" fig(uri={'yes' if b.figure.image_uri else 'no'})"
            preview = b.text.replace("\n", " ⏎ ")[:120]
            self._lines.append(f"{head}")
            self._lines.append(f"        {preview!r}")

        self.section("quality report")
        self.obj("QualityReport =", report)

        self.section("captured debug events (raw)")
        for ev in self._captured:
            self._lines.append(json.dumps(_to_jsonable(ev), ensure_ascii=False))

        self.out_dir.mkdir(parents=True, exist_ok=True)
        trace_path = self.out_dir / f"{self.provider}.trace.log"
        trace_path.write_text("\n".join(self._lines) + "\n", encoding="utf-8")

        (self.out_dir / f"{self.provider}.parsed.json").write_text(
            json.dumps(_to_jsonable(doc), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (self.out_dir / f"{self.provider}.content.md").write_text(doc.markdown, encoding="utf-8")

        return trace_path

    def write_partial(self) -> Path:
        """Write just the trace log — used when the parse raised before a document existed."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        trace_path = self.out_dir / f"{self.provider}.trace.log"
        trace_path.write_text("\n".join(self._lines) + "\n", encoding="utf-8")
        return trace_path


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (dict, list)):
        return json.dumps(_to_jsonable(v), ensure_ascii=False)
    return str(v)
