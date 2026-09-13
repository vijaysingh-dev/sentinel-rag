"""Ingest a directory (or one file) through the real pipeline, with full per-document tracing.

    uv run python scripts/ingest/01_ingest_traced.py knowledge
    uv run python scripts/ingest/01_ingest_traced.py knowledge --recreate
    uv run python scripts/ingest/01_ingest_traced.py knowledge/runbook/checkout.docx --doc-type runbook

Same pipeline as `make ingest` (`sentinel.ingest.cli` -> `IngestionPipeline`) — this script does
not reimplement parsing, chunking, embedding or upserting, it calls exactly the same
`ingest_file` for each document. The only difference is visibility: `make ingest` prints one
aggregate summary line per document; this writes what each *stage* actually did, per document,
under `outputs/{parsing,chunking,embedding,vectorstore,ingest}/<doc-stem>/...` — see
`scripts/ingest/_trace.py` for exactly what lands where and why.

Concurrency is deliberately 1, not `IngestionPipeline.ingest_directory`'s default of 4: tracing
uses `structlog.testing.capture_logs`, which is not safe to nest across concurrent tasks in the
same process (log entries from two documents parsing at once would land in the same capture and
get attributed to whichever document's tracer happened to read them). For a one-off traced run
over a corpus of this size, sequential is a latency cost worth paying for a trace you can trust;
`make ingest` stays the fast path for routine re-ingestion once you don't need per-doc trace files.

Reads real credentials from `.env` for whichever providers are configured — `make doctor` first
if you're not sure what will actually run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # sibling _trace import

from _trace import IngestTracer

from sentinel.core.config import settings
from sentinel.core.observability import configure_logging
from sentinel.ingest.pipeline import SUPPORTED_SUFFIXES, IngestionPipeline, IngestStats


async def ingest_one(pipeline: IngestionPipeline, path: Path, doc_type: str, tenant: str) -> IngestStats:
    tracer = IngestTracer(path.stem)
    with tracer.capture():
        stats = await pipeline.ingest_file(path, doc_type=doc_type, tenant_id=tenant, on_stage=tracer.on_stage)
    written = tracer.write()
    status = "ok" if not stats.failures else "FAILED"
    print(f"  {path}  [{status}]  chunks_new={stats.chunks_new}  -> outputs/*/{path.stem}/ ({len(written)} files)")
    return stats


async def run(args: argparse.Namespace) -> int:
    pipeline = IngestionPipeline(acl_rules=args.acl_rules, enrich_figures=args.figures, contextual=args.contextual)
    pipeline.prepare(recreate=args.recreate)

    root = Path(args.root)
    if root.is_file():
        paths = [root]
    else:
        paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)

    if not paths:
        print(f"no supported documents found under {root}")
        return 1

    print(f"parse       {settings.parse_provider} ({settings.parse_strategy})")
    print(f"embed       {settings.embed_provider} / {settings.embedding_model} ({settings.embedding_dims}d)")
    print(f"store       {settings.vectorstore_provider}")
    print(f"version     {settings.index_version}")
    print(f"\ntracing {len(paths)} document(s), sequentially — see the module docstring for why\n")

    total = IngestStats()
    for path in paths:
        doc_type = args.doc_type or path.parent.name
        result = await ingest_one(pipeline, path, doc_type, args.tenant)
        total.merge(result)

    print(f"\ndocuments        {total.documents}/{len(paths)}")
    print(f"chunks new       {total.chunks_new}")
    print(f"chunks unchanged {total.chunks_unchanged}")
    print(f"chunks deleted   {total.chunks_deleted}")
    print(f"cost             ${total.usd_cost:.4f}")
    if total.parse_routes:
        print("parse routes     " + ", ".join(f"{r}={c}" for r, c in sorted(total.parse_routes.items())))
    if total.failures:
        print(f"\n{len(total.failures)} failures — see data/dlq.jsonl and each doc's outputs/*/<stem>/ folder")
        for failure in total.failures[:5]:
            print(f"  {failure.get('path', '?')}: {failure.get('error')}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="directory or single file to ingest")
    parser.add_argument("--doc-type", default=None, help="override; defaults to the parent directory name")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--recreate", action="store_true", help="drop and rebuild the index")
    parser.add_argument("--figures", action="store_true", help="describe diagrams with a vision model")
    parser.add_argument(
        "--contextual", action="store_true", help="LLM situating sentence per chunk (one call per chunk)"
    )
    parser.add_argument("--acl-rules", default="config/acl_rules.yaml")
    args = parser.parse_args()

    configure_logging()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
