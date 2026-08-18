"""Ingestion CLI.

    python -m sentinel.ingest.cli knowledge
    python -m sentinel.ingest.cli knowledge --recreate --figures
    python -m sentinel.ingest.cli data/raw --doc-type methodology --concurrency 8

Document type defaults to the immediate parent directory name, so `knowledge/runbook/*.md` becomes
doc_type "runbook" and is chunked with the runbook strategy. Convention over configuration, and it
keeps the corpus layout self-documenting.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sentinel.core.config import settings
from sentinel.core.observability import configure_logging
from sentinel.ingest.pipeline import IngestionPipeline


async def run(args: argparse.Namespace) -> int:
    pipeline = IngestionPipeline(
        acl_rules=args.acl_rules,
        enrich_figures=args.figures,
        contextual=args.contextual,
    )
    pipeline.prepare(recreate=args.recreate)

    print(f"parse       {settings.parse_provider} ({settings.parse_strategy})")
    print(f"embed       {settings.embed_provider} / {settings.embedding_model} ({settings.embedding_dims}d)")
    print(f"store       {settings.vectorstore_provider}")
    print(f"version     {settings.index_version}")
    print()

    stats = await pipeline.ingest_directory(
        args.root,
        doc_type=args.doc_type,
        tenant_id=args.tenant,
        concurrency=args.concurrency,
    )

    print(f"\ndocuments       {stats.documents}")
    print(f"chunks new      {stats.chunks_new}")
    print(f"chunks unchanged {stats.chunks_unchanged}")
    print(f"chunks deleted  {stats.chunks_deleted}")
    print(f"cost            ${stats.usd_cost:.4f}")
    print(f"duration        {stats.duration_s:.1f}s")

    if stats.parse_routes:
        print("\nparse routes")
        for route, count in sorted(stats.parse_routes.items(), key=lambda kv: -kv[1]):
            print(f"  {route:28s} {count}")
        # A corpus where almost everything escalated is a corpus where the threshold is wrong.
        escalated = sum(c for r, c in stats.parse_routes.items() if "cloud" in r)
        total = sum(stats.parse_routes.values())
        if total:
            print(f"  escalation rate: {escalated / total:.0%}")

    if stats.failures:
        print(f"\n{len(stats.failures)} failures — see data/dlq.jsonl")
        for failure in stats.failures[:5]:
            print(f"  {failure.get('path', '?')}: {failure.get('error')}")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="directory to ingest")
    parser.add_argument("--doc-type", default=None, help="override; defaults to the parent directory name")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--concurrency", type=int, default=4)
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
