"""Backfill `relevant_chunk_ids` in data/golden.jsonl from the indexed corpus.

    uv run python scripts/evaluation/02_backfill_golden_chunk_ids.py            # writes data/golden.jsonl
    uv run python scripts/evaluation/02_backfill_golden_chunk_ids.py --check    # report only

The golden set (`scripts/corpus/gen_golden.py`) is hand-authored against `answer_substrings`
because those survive re-chunking while chunk ids do not (see its module docstring). That leaves
`relevant_chunk_ids` empty for every example, which silently disables recall@k/precision@k/mrr/ndcg
in `evaluation/retrieval_eval.py` — they are only computed `if relevant:`. Only `substring_hit`
still works, which is why the eval gate showed a real substring_hit but "not measured" for
everything else.

This script closes that gap after the fact, once, by matching each example's `answer_substrings`
against the *already-ingested* chunk content: a chunk is "relevant" if it contains every substring
for that example (case-insensitive). That is deliberately conservative — it will miss a chunk that
paraphrases the answer instead of stating it verbatim, so treat the result as a lower bound, not
ground truth, and hand-review categories where 0 chunks matched.

Examples with no `answer_substrings` (live_status, unanswerable, acl_denied, and some escalate-only
mixed cases) are left with an empty list on purpose — there is no "relevant chunk" to find, since
the correct behaviour is not to answer from the corpus at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

GOLDEN_PATH = REPO_ROOT / "data" / "golden.jsonl"


def _find_matches(cur, table: str, index_version: str, substrings: list[str]) -> list[str]:
    if not substrings:
        return []
    clauses = " AND ".join(f"content ILIKE %(s{i})s" for i in range(len(substrings)))
    params = {f"s{i}": f"%{s}%" for i, s in enumerate(substrings)}
    params["index_version"] = index_version
    cur.execute(
        f"SELECT chunk_id FROM {table} WHERE index_version = %(index_version)s AND {clauses}",
        params,
    )
    return [row[0] for row in cur.fetchall()]


def run(check_only: bool) -> int:
    from sentinel.core.config import settings

    raw_dsn = settings.pgvector_dsn.replace("postgresql+psycopg://", "postgresql://")
    table = settings.pgvector_collection
    index_version = settings.index_version

    import psycopg

    examples = [json.loads(line) for line in GOLDEN_PATH.read_text().splitlines() if line.strip()]

    updated = 0
    unmatched: list[str] = []
    with psycopg.connect(raw_dsn) as conn, conn.cursor() as cur:
        for ex in examples:
            substrings = ex.get("answer_substrings") or []
            if not substrings:
                continue
            chunk_ids = _find_matches(cur, table, index_version, substrings)
            if not chunk_ids:
                unmatched.append(ex["id"])
                continue
            if chunk_ids != ex.get("relevant_chunk_ids"):
                ex["relevant_chunk_ids"] = sorted(chunk_ids)
                updated += 1

    print(f"{len(examples)} examples, {updated} updated with relevant_chunk_ids")
    if unmatched:
        print(f"\n{len(unmatched)} examples had substrings but no matching chunk (hand-review):")
        for ex_id in unmatched:
            print(f"  {ex_id}")

    if not check_only and updated:
        with GOLDEN_PATH.open("w") as f:
            for ex in examples:
                f.write(json.dumps(ex) + "\n")
        print(f"\nwrote {GOLDEN_PATH}")
    elif check_only:
        print("\n--check: no file written")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report only, do not write golden.jsonl")
    args = parser.parse_args()
    raise SystemExit(run(args.check))
