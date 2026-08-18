"""Generation evaluation: faithfulness, relevance, citations, refusal.

Retrieval metrics are deterministic; generation quality is not, so this layer uses an LLM judge.
That is a real methodological compromise and worth being honest about, because the known biases
are what an interviewer will press on:

* **Position bias** — judges favour whichever answer they see first. Mitigated by scoring one
  answer at a time against explicit criteria rather than comparing pairs.
* **Verbosity bias** — longer answers are scored higher regardless of correctness. Mitigated by
  decomposing into atomic claims, so length stops being a proxy for quality.
* **Self-preference** — a model rates its own output more highly. Mitigated by judging with a
  different model family from the one that generated.
* **Rationale-after-score** — a judge asked for a score first will rationalise it afterwards. The
  schemas here put the rationale field *before* the score so it is generated first, which measurably
  improves agreement with human labels.

The metric that matters most in an operations context is **faithfulness**: is every claim supported
by retrieved context? A fluent, well-cited, confidently wrong answer is the failure mode that gets
someone to run the wrong runbook during an incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from sentinel.core.observability import log
from sentinel.core.types import GoldenExample


class ClaimDecomposition(BaseModel):
    """Atomic factual claims extracted from an answer."""

    claims: list[str] = Field(description="Each independently verifiable, one fact each.")


class ClaimVerdict(BaseModel):
    # Rationale first so the model reasons before committing to a label.
    rationale: str = Field(max_length=300)
    supported: bool
    supporting_span: str = Field(
        default="", max_length=400, description="Verbatim context span, empty when unsupported."
    )


class AnswerJudgement(BaseModel):
    rationale: str = Field(max_length=500, description="Write this before any score.")
    addresses_question: bool
    uses_only_context: bool
    appropriately_hedged: bool = Field(description="True when uncertainty is stated where the context is thin.")
    actionable: bool
    score: float = Field(ge=0, le=1)


DECOMPOSE_PROMPT = """Break this answer into atomic factual claims — each independently checkable,
one fact each. Ignore pleasantries, hedges and restatements of the question.

Answer:
{answer}"""

VERIFY_PROMPT = """Decide whether the context supports this claim.

Supported means the context states it or directly entails it. Plausible, widely known, or likely
true is NOT supported. If the claim is not in the context, it is unsupported even if correct.

Give your rationale first, then the verdict, then quote the exact supporting span (empty if none).

Claim: {claim}

Context:
{context}"""

JUDGE_PROMPT = """Grade this documentation assistant's answer.

Question: {question}
{ground_truth_block}
Retrieved context:
{context}

Answer:
{answer}

Write a short rationale first, then judge each criterion, then give an overall score."""


@dataclass
class GenerationEvalResult:
    faithfulness: float = 0.0
    answer_relevance: float = 0.0
    citation_validity: float = 0.0
    refusal_accuracy: float = 0.0
    unsupported_claims: list[str] = field(default_factory=list)
    per_example: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"faithfulness={self.faithfulness:.3f} relevance={self.answer_relevance:.3f} "
            f"citations={self.citation_validity:.3f} refusal={self.refusal_accuracy:.3f}"
        )


async def faithfulness_score(answer: str, context: str, judge: Any) -> tuple[float, list[str]]:
    """Fraction of atomic claims supported by the retrieved context.

    Claim decomposition rather than whole-answer judging is what makes this robust: a whole-answer
    verdict collapses "eight right facts and one fabricated one" into a single fuzzy score, while
    decomposition surfaces the one fabrication — which is the thing you actually needed to know.
    """
    decomposer = judge.with_structured_output(ClaimDecomposition)
    verifier = judge.with_structured_output(ClaimVerdict)

    try:
        decomposition = await decomposer.ainvoke(DECOMPOSE_PROMPT.format(answer=answer))
    except Exception as exc:
        log.warning("claim_decomposition_failed", error=str(exc)[:160])
        return 0.0, []

    claims = decomposition.claims
    if not claims:
        return 1.0, []  # nothing asserted, nothing to contradict

    import asyncio

    async def verify(claim: str) -> tuple[str, bool]:
        try:
            verdict = await verifier.ainvoke(VERIFY_PROMPT.format(claim=claim, context=context[:12000]))
            return claim, verdict.supported
        except Exception:
            return claim, False  # fail closed: an unverifiable claim counts as unsupported

    results = await asyncio.gather(*(verify(c) for c in claims))
    unsupported = [claim for claim, ok in results if not ok]
    return (len(claims) - len(unsupported)) / len(claims), unsupported


def citation_validity(answer: str, valid_ids: set[str]) -> float:
    """Fraction of cited chunk ids that were actually retrieved.

    Catches the specific failure where a model invents a plausible-looking reference. This is a
    pure string check, no LLM needed, and it is one of the cheapest useful metrics in the suite.
    """
    import re

    cited = set(re.findall(r"\[([a-f0-9:_\-]{8,})\]", answer))
    if not cited:
        return 0.0
    return len(cited & valid_ids) / len(cited)


async def evaluate_generation(
    examples: list[GoldenExample],
    answers: dict[str, dict],
    judge: Any,
) -> GenerationEvalResult:
    """Scores answers produced for a golden set.

    Args:
        answers: Maps example id -> {"answer": str, "context": str, "chunk_ids": [str]}.
            Answers are passed in rather than generated here so the same evaluation can grade
            production traces, not only offline runs.
    """
    import asyncio

    rows: list[dict] = []
    all_unsupported: list[str] = []

    async def score_one(example: GoldenExample) -> dict | None:
        payload = answers.get(example.id)
        if not payload:
            return None

        answer = payload.get("answer", "")
        context = payload.get("context", "")
        chunk_ids = set(payload.get("chunk_ids", []))

        row: dict[str, Any] = {"id": example.id, "category": example.category}

        if example.expected_behaviour in {"refuse", "escalate"}:
            # Correct refusal is measured behaviourally: no invented content, and for ACL cases no
            # acknowledgement that a restricted document exists.
            refused = _looks_like_refusal(answer)
            leaked = any(term.lower() in answer.lower() for term in example.must_not_contain)
            row["refusal_correct"] = 1.0 if (refused and not leaked) else 0.0
            rows.append(row)
            return row

        score, unsupported = await faithfulness_score(answer, context, judge)
        row["faithfulness"] = score
        all_unsupported.extend(unsupported)

        row["citation_validity"] = citation_validity(answer, chunk_ids)

        try:
            judgement = await judge.with_structured_output(AnswerJudgement).ainvoke(
                JUDGE_PROMPT.format(
                    question=example.question,
                    ground_truth_block=(f"Reference answer: {example.ground_truth}\n" if example.ground_truth else ""),
                    context=context[:10000],
                    answer=answer,
                )
            )
            row["answer_relevance"] = judgement.score
            row["addresses_question"] = float(judgement.addresses_question)
            row["appropriately_hedged"] = float(judgement.appropriately_hedged)
        except Exception as exc:
            log.warning("judge_failed", example=example.id, error=str(exc)[:160])

        rows.append(row)
        return row

    await asyncio.gather(*(score_one(e) for e in examples))

    def mean(key: str) -> float:
        values = [r[key] for r in rows if key in r]
        return round(sum(values) / len(values), 4) if values else 0.0

    result = GenerationEvalResult(
        faithfulness=mean("faithfulness"),
        answer_relevance=mean("answer_relevance"),
        citation_validity=mean("citation_validity"),
        refusal_accuracy=mean("refusal_correct"),
        unsupported_claims=all_unsupported[:50],
        per_example=rows,
    )
    log.info(
        "generation_eval_complete",
        **{
            "faithfulness": result.faithfulness,
            "relevance": result.answer_relevance,
            "citations": result.citation_validity,
            "refusal": result.refusal_accuracy,
        },
    )
    return result


REFUSAL_MARKERS = (
    "not documented",
    "no documentation",
    "couldn't find",
    "could not find",
    "not available to you",
    "don't have",
    "do not have",
    "unable to find",
    "no information",
    "not covered",
    "escalate",
)


def _looks_like_refusal(answer: str) -> bool:
    """Heuristic refusal detector.

    Deliberately keyword-based rather than LLM-judged: refusal is a narrow, well-signposted
    behaviour, and spending a model call to detect it adds cost and variance for no accuracy gain.
    """
    lowered = answer.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)
