"""Query transformation.

The user's raw text is often a poor search query. Four techniques, in the order they actually pay
off in production.

**Conversational rewriting is first for a reason.** "What about the second one?" has no standalone
meaning; embedding it retrieves noise. Any multi-turn RAG system without rewriting is broken from
turn two onward, and it is the single most common unreported bug in deployed assistants.
"""

from __future__ import annotations

import re
from typing import Any

from sentinel.core.observability import log

# Cheap heuristic gate: only pay for a rewrite when the query looks context-dependent. Saves an
# LLM call and ~300ms on the majority of queries, which are already standalone.
DEPENDENT = re.compile(
    r"\b(it|its|this|that|these|those|they|them|the (first|second|third|last|other|same)|"
    r"there|then|he|she|his|her)\b",
    re.IGNORECASE,
)

REWRITE_PROMPT = """Rewrite the user's latest message into a standalone search query that makes
sense without the conversation history. Keep entity names, tickers and error codes exactly as
written. Output only the query, nothing else.

Conversation:
{history}

Latest message: {message}
Standalone query:"""

MULTI_QUERY_PROMPT = """Generate {n} alternative search queries for the question below. Vary the
vocabulary — use synonyms a document author might have used instead of the asker's words. One query
per line, no numbering, no preamble.

Question: {question}"""

HYDE_PROMPT = """Write a short factual paragraph that would answer this question, as it might
appear in internal technical documentation. Do not hedge or say you are unsure — write the passage
as if it were the documentation itself.

Question: {question}"""

DECOMPOSE_PROMPT = """If answering this question requires several separate lookups, break it into
standalone sub-questions, one per line. If a single lookup suffices, return the question unchanged.

Question: {question}"""


def needs_rewrite(message: str, history: str) -> bool:
    """True when the message references something outside itself."""
    return bool(history) and bool(DEPENDENT.search(message))


async def rewrite_query(message: str, history: str, llm: Any) -> str:
    """Resolves pronouns and references against conversation history.

    Falls back to the original message on any failure: a failed rewrite must degrade to
    "search the literal question", never to an error.
    """
    if not needs_rewrite(message, history):
        return message
    try:
        response = await llm.ainvoke(REWRITE_PROMPT.format(history=history[-2000:], message=message))
        rewritten = (response.content or "").strip()
        if rewritten:
            log.debug("query_rewritten", original_len=len(message), new_len=len(rewritten))
            return rewritten
    except Exception as exc:
        log.warning("rewrite_failed", error=str(exc)[:160])
    return message


async def multi_query(question: str, llm: Any, n: int = 3) -> list[str]:
    """Generates paraphrases to cover vocabulary mismatch between askers and authors.

    Costs n additional retrievals plus one LLM call, so it belongs on high-value or batch paths
    rather than on every interactive query. Fuse the results with RRF.
    """
    try:
        response = await llm.ainvoke(MULTI_QUERY_PROMPT.format(n=n, question=question))
        variants = [q.strip() for q in (response.content or "").splitlines() if q.strip()]
        return [question, *variants[:n]]
    except Exception as exc:
        log.warning("multi_query_failed", error=str(exc)[:160])
        return [question]


async def hyde(question: str, llm: Any) -> str:
    """Hypothetical Document Embeddings.

    Embeds a *fabricated answer* rather than the question, on the reasoning that an answer sits
    closer to real passages in embedding space than a question does — it addresses the
    query/document asymmetry from the retrieval side.

    Helps on jargon-heavy or out-of-domain queries; hurts when the model hallucinates the wrong
    domain entirely, which is why it is opt-in rather than default.
    """
    try:
        response = await llm.ainvoke(HYDE_PROMPT.format(question=question))
        return (response.content or "").strip() or question
    except Exception as exc:
        log.warning("hyde_failed", error=str(exc)[:160])
        return question


async def decompose(question: str, llm: Any) -> list[str]:
    """Splits a multi-hop question into independently retrievable sub-questions.

    "How does the divisor adjustment differ between special dividends and spin-offs?" is two
    lookups and a comparison, not one search.
    """
    try:
        response = await llm.ainvoke(DECOMPOSE_PROMPT.format(question=question))
        parts = [q.strip() for q in (response.content or "").splitlines() if q.strip()]
        return parts or [question]
    except Exception as exc:
        log.warning("decompose_failed", error=str(exc)[:160])
        return [question]
