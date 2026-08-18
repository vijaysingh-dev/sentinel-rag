"""Golden set: loading, generation and category coverage.

The golden set is the single highest-leverage artifact in a RAG project and the one most teams
skip. Without it every change is a vibe check, and "it seems better" is not something you can put
in a pull request.

Rules that make one useful rather than decorative:

* **Every category must be represented**, especially the negative ones. A set of only answerable
  questions cannot measure hallucination, which is usually the failure you most need to catch.
* **Unanswerable questions are first-class.** The correct behaviour is refusal, and a system that
  scores well only because it always answers is not good, it is untested.
* **ACL cases are first-class.** The correct behaviour for a restricted question is not just
  "no answer" but "no acknowledgement that the document exists".
* **Seed it by hand, grow it from production.** Twenty hand-written examples beat two hundred
  synthetic ones; then mine real failures and add them, which is what keeps it honest over time.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from sentinel.core.observability import log
from sentinel.core.types import GoldenExample

# Minimum share of the set each category should occupy for the suite to be meaningful. Enforced by
# `check_coverage` so a set that has quietly drifted into all-easy-questions is caught.
TARGET_MIX = {
    "terminology": 0.10,
    "howto": 0.20,
    "design_rationale": 0.10,
    "lookup": 0.15,
    "live_status": 0.10,
    "mixed": 0.10,
    "unanswerable": 0.15,
    "acl_denied": 0.10,
}


def load_golden(path: str | Path) -> list[GoldenExample]:
    """Reads a JSONL golden set."""
    examples: list[GoldenExample] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        examples.append(GoldenExample(**json.loads(line)))
    log.info("golden_loaded", path=str(path), count=len(examples))
    return examples


def save_golden(examples: list[GoldenExample], path: str | Path) -> None:
    from dataclasses import asdict

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(json.dumps(asdict(e)) for e in examples) + "\n", encoding="utf-8")


def check_coverage(examples: list[GoldenExample]) -> dict[str, Any]:
    """Reports whether the set is balanced enough to trust.

    Returns the observed mix and any categories that fall short, so CI can fail a pull request
    that adds fifty easy questions and calls it improved coverage.
    """
    counts = Counter(e.category for e in examples)
    total = len(examples) or 1
    observed = {k: round(v / total, 3) for k, v in counts.items()}

    gaps = {
        category: {"target": target, "observed": observed.get(category, 0.0)}
        for category, target in TARGET_MIX.items()
        if observed.get(category, 0.0) < target * 0.6
    }

    return {
        "total": len(examples),
        "counts": dict(counts),
        "observed_mix": observed,
        "underrepresented": gaps,
        "balanced": not gaps,
    }


GENERATE_PROMPT = """You are building an evaluation set for a documentation assistant serving a
financial index calculation platform.

Read this document chunk and write {n} questions an engineer or support analyst would realistically
ask that this chunk answers.

Rules:
- Ask the way a person actually types: short, sometimes with a typo, sometimes using a synonym the
  document does not use.
- Do not quote the chunk's wording back. A question that reuses the chunk's exact phrasing tests
  string matching, not retrieval.
- Skip anything the chunk does not actually answer.

Return one JSON object per line: {{"question": "...", "category": "terminology|howto|lookup|design_rationale"}}

Chunk:
{chunk}"""


async def generate_from_chunks(
    chunks: list[Any], llm: Any, per_chunk: int = 2, max_chunks: int = 50
) -> list[GoldenExample]:
    """Bootstraps candidate questions from indexed chunks.

    Useful for the first pass when no golden set exists — but the output is *candidates*, not a
    golden set. Synthetic questions are systematically easier than real ones because they are
    generated from the answer, so a system can score 0.95 on them and still fail users. Review and
    edit before trusting, and replace them with real questions as they arrive.
    """
    import asyncio

    examples: list[GoldenExample] = []
    semaphore = asyncio.Semaphore(4)

    async def one(chunk: Any, index: int) -> list[GoldenExample]:
        async with semaphore:
            try:
                response = await llm.ainvoke(GENERATE_PROMPT.format(n=per_chunk, chunk=chunk.context_text[:3000]))
            except Exception as exc:
                log.warning("golden_generation_failed", error=str(exc)[:160])
                return []

            out = []
            for line in (response.content or "").splitlines():
                line = line.strip().strip("`")
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.append(
                    GoldenExample(
                        id=f"gen-{index}-{len(out)}",
                        question=payload["question"],
                        category=payload.get("category", "lookup"),
                        relevant_chunk_ids=[chunk.chunk_id],
                        role="admin",
                        meta={"synthetic": True, "source_chunk": chunk.chunk_id},
                    )
                )
            return out

    results = await asyncio.gather(*(one(c, i) for i, c in enumerate(chunks[:max_chunks])))
    for batch in results:
        examples.extend(batch)

    log.info("golden_generated", count=len(examples), note="synthetic; review before use")
    return examples
