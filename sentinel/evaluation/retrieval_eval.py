"""Retrieval evaluation.

Runs the golden set through the retrieval pipeline and reports the ranking metrics. No LLM
involved, so this is fast and deterministic enough to gate every pull request.

The output separates recall at the candidate stage from recall after reranking, because those two
numbers tell you *which* stage to fix — see `evaluation/metrics.py` for why that split is the most
useful diagnostic in the whole suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sentinel.core.observability import log
from sentinel.core.types import GoldenExample, StageResult
from sentinel.evaluation.metrics import (
    aggregate,
    bootstrap_ci,
    hit_rate,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    substring_hit,
)


@dataclass
class RetrievalEvalResult:
    metrics: dict[str, float]
    per_example: list[dict] = field(default_factory=list)
    confidence_intervals: dict[str, tuple[float, float]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return " | ".join(f"{k}={v:.3f}" for k, v in sorted(self.metrics.items()))


def evaluate_retrieval(
    pipeline: Any,
    examples: list[GoldenExample],
    k_candidates: int = 50,
    k_final: int = 5,
) -> RetrievalEvalResult:
    """Scores retrieval quality over a golden set.

    ACL-denied examples are scored inverted: success means retrieving *nothing*. Mixing them into
    the same recall average as answerable questions would be meaningless, so they are reported as
    their own metric.
    """
    rows: list[dict] = []
    failures: list[dict] = []

    for example in examples:
        try:
            wide = pipeline.retrieve(
                example.question,
                roles=[example.role],
                k=k_candidates,
                top_n=k_candidates,
            )
            final = pipeline.retrieve(
                example.question,
                roles=[example.role],
                k=k_candidates,
                top_n=k_final,
            )
        except Exception as exc:
            failures.append({"id": example.id, "error": type(exc).__name__, "detail": str(exc)[:200]})
            continue

        wide_ids = [c.chunk.chunk_id for c in wide.chunks]
        final_ids = [c.chunk.chunk_id for c in final.chunks]
        relevant = set(example.relevant_chunk_ids)

        if example.category == "acl_denied":
            # Correct behaviour is an empty result set. Any hit is a security failure, not a
            # relevance miss, so it is tracked separately and should gate releases on its own.
            rows.append(
                {
                    "id": example.id,
                    "category": example.category,
                    "acl_respected": 1.0 if not final_ids else 0.0,
                }
            )
            continue

        if example.category == "unanswerable":
            # Nothing should score above the relevance floor. `retrieved_nothing` measures whether
            # the floor is doing its job rather than whether ranking is good.
            rows.append(
                {
                    "id": example.id,
                    "category": example.category,
                    "retrieved_nothing": 1.0 if not final_ids else 0.0,
                    "top_score": final.chunks[0].score if final.chunks else 0.0,
                }
            )
            continue

        row = {
            "id": example.id,
            "category": example.category,
            "candidates": len(wide_ids),
            "mode": final.mode,
            "latency_ms": sum(final.latency_ms.values()),
        }

        if relevant:
            relevance = {cid: 1.0 for cid in relevant}
            row.update(
                {
                    f"recall@{k_candidates}": recall_at_k(wide_ids, relevant, k_candidates),
                    f"recall@{k_final}": recall_at_k(final_ids, relevant, k_final),
                    f"precision@{k_final}": precision_at_k(final_ids, relevant, k_final),
                    f"hit@{k_final}": hit_rate(final_ids, relevant, k_final),
                    "mrr": mrr(final_ids, relevant),
                    f"ndcg@{k_final}": ndcg_at_k(final_ids, relevance, k_final),
                }
            )
        if example.answer_substrings:
            row["substring_hit"] = substring_hit(
                [c.chunk.context_text for c in final.chunks], example.answer_substrings
            )

        rows.append(row)

    metrics = aggregate(rows)
    intervals = {}
    for key in (f"recall@{k_final}", "mrr"):
        values = [r[key] for r in rows if key in r]
        if values:
            intervals[key] = bootstrap_ci(values)

    log.info(
        "retrieval_eval_complete",
        examples=len(examples),
        failures=len(failures),
        **{k: round(v, 4) for k, v in metrics.items()},
    )

    return RetrievalEvalResult(metrics=metrics, per_example=rows, confidence_intervals=intervals, failures=failures)


def to_stage_result(result: RetrievalEvalResult, provider: str, config: dict, duration_s: float) -> StageResult:
    return StageResult(
        stage="retrieval",
        provider=provider,
        config=config,
        metrics=result.metrics,
        duration_s=duration_s,
        n_items=len(result.per_example),
        notes=f"{len(result.failures)} failures" if result.failures else "",
    )
