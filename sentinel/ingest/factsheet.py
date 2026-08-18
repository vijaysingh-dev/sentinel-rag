"""Generated index fact sheets.

New indices launch continuously and nobody writes a wiki page for each one. Two bad options follow:
leave them undocumented, or try to RAG over thousands of rows of a metadata database. Neither
works — the first leaves a gap, the second produces chunks of orphaned field values.

The right answer is to **generate a document from structured metadata** and ingest it like any
other. Triggered from the existing Kafka topics, so a new index is searchable within minutes of
launch.

Note the boundary, because it is the interesting part: fact sheets carry the *prose* — methodology,
operational quirks, distribution notes. Precise questions ("which indices rebalance in March",
"how many launched this quarter") route to SQL against the metadata database instead, because that
is exact, always current, and cheaper. Knowing which questions belong to which system is the
design decision; the fact sheet is just the implementation.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.observability import log
from sentinel.core.types import Block, BlockType, ParsedDocument, ParseMetrics

TEMPLATE = """# {name} ({ticker})

Launched {launch_date} · Owner {owner_team} · Calculation {calc_type} · Currency {currency}

## Methodology

{methodology}

## Calculation schedule

Rebalance frequency: {rebalance_freq}. Calculation window: {calc_window}. Cutoff: {cutoff}.

## Distribution

Delivered to: {exchanges}. Feed: {feed_name}. Latency SLO: {latency_slo}.

## Operational notes

{operational_notes}
"""

REQUIRED = ("ticker", "name")


def render_factsheet(event: dict[str, Any]) -> ParsedDocument:
    """Builds a ParsedDocument from an index-metadata event.

    Missing fields render as an explicit placeholder rather than an empty string, so a half-populated
    fact sheet is visibly incomplete instead of quietly implying the field does not apply.
    """
    missing = [f for f in REQUIRED if not event.get(f)]
    if missing:
        raise ValueError(f"index event missing required fields: {missing}")

    fields = {
        "name": event["name"],
        "ticker": event["ticker"],
        "launch_date": event.get("launch_date", "unknown"),
        "owner_team": event.get("owner_team", "unassigned"),
        "calc_type": event.get("calc_type", "unknown"),
        "currency": event.get("currency", "unknown"),
        "methodology": event.get("methodology") or "_Not yet documented._",
        "rebalance_freq": event.get("rebalance_freq", "unknown"),
        "calc_window": event.get("calc_window", "unknown"),
        "cutoff": event.get("cutoff", "unknown"),
        "exchanges": ", ".join(event.get("exchanges", [])) or "none recorded",
        "feed_name": event.get("feed_name", "unknown"),
        "latency_slo": event.get("latency_slo", "unspecified"),
        "operational_notes": event.get("operational_notes") or "_None recorded._",
    }
    markdown = TEMPLATE.format(**fields)

    blocks: list[Block] = []
    for section in markdown.split("\n## "):
        lines = section.strip().splitlines()
        if not lines:
            continue
        heading = lines[0].lstrip("# ").strip()
        body = "\n".join(lines[1:]).strip()
        blocks.append(Block(BlockType.HEADING, heading, level=2))
        if body:
            blocks.append(Block(BlockType.PARAGRAPH, body))

    return ParsedDocument(
        doc_id=f"factsheet::{event['ticker']}",
        source_uri=f"internal://index/{event['ticker']}",
        title=f"{event['name']} ({event['ticker']})",
        blocks=blocks,
        metrics=ParseMetrics(provider="generated", duration_s=0.0, pages=1, characters=len(markdown)),
        doc_type="factsheet",
        tenant_id=event.get("tenant_id", "default"),
        effective_date=event.get("updated_at", "")[:10] or None,
        meta={"corpus": "indices", "ticker": event["ticker"], "components": ["ice"], "generated": True},
    )


async def on_index_event(event: dict[str, Any], pipeline: Any) -> dict[str, Any]:
    """Kafka consumer entry point for index.launched / amended / retired.

    Retirement **deletes** the fact sheet. An assistant confidently describing a decommissioned
    index is worse than one that says nothing, and in a regulated environment it is a finding.
    """
    action = event.get("action", "launched")
    ticker = event.get("ticker", "?")

    if action == "retired":
        doc_id = f"factsheet::{ticker}"
        orphans = list(pipeline.store.chunk_ids_for_doc(doc_id))
        deleted = pipeline.store.delete(orphans) if orphans else 0
        log.info("factsheet_retired", ticker=ticker, chunks_deleted=deleted)
        return {"action": "retired", "ticker": ticker, "chunks_deleted": deleted}

    doc = render_factsheet(event)
    chunks = await pipeline._chunk(doc, override="structural")

    roles, components = pipeline.acl.resolve(doc.source_uri, doc.doc_type, doc.meta)
    for chunk in chunks:
        chunk.allowed_roles = roles
        chunk.components = components or ["ice"]

    stats = pipeline._write(doc, chunks)
    log.info(
        "factsheet_ingested",
        ticker=ticker,
        action=action,
        chunks_new=stats.chunks_new,
        chunks_deleted=stats.chunks_deleted,
    )
    return {"action": action, "ticker": ticker, "chunks_new": stats.chunks_new}
