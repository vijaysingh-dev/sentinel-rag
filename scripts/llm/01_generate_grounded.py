"""Answer a question from retrieved context, on any provider.

    python scripts/llm/01_generate_grounded.py "how do I restart the calculation engine"
    python scripts/llm/01_generate_grounded.py --provider aws "..." --no-context

The `--no-context` flag is the useful part. It runs the identical prompt with the context stripped
so you can see what the model invents when it has nothing to ground on. That contrast is the single
clearest demonstration of why RAG exists and why faithfulness is the metric that matters: the
ungrounded answer is usually fluent, plausible, specific, and wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sentinel.core.observability import configure_logging
from sentinel.core.registry import get_provider
from sentinel.retrieval.pipeline import RetrievalPipeline

SYSTEM = """You answer questions about a financial index calculation platform using only the
provided documentation.

Rules:
- Cite the chunk id in square brackets after every factual claim, e.g. [abc123::0001::def].
- If the documentation does not answer the question, say so plainly and suggest who to ask. Do not
  fill the gap from general knowledge — a confident wrong answer during an incident is worse than
  no answer.
- Quote exact figures and thresholds rather than paraphrasing them."""


async def run(question: str, provider: str | None, use_context: bool, role: str) -> int:
    llm = get_provider("llm", provider)

    context = ""
    refs: list[str] = []
    if use_context:
        pipeline = RetrievalPipeline()
        results = pipeline.retrieve(question, roles=[role])
        context = results.context
        refs = results.refs
        print(f"retrieved {len(results.chunks)} chunks (mode={results.mode}, {sum(results.latency_ms.values()):.0f}ms)")
        for item in results.chunks:
            print(f"  {item.score:7.4f}  {item.chunk.heading_path}")
        print()

    prompt = f"{SYSTEM}\n\nDocumentation:\n{context or '(none provided)'}\n\nQuestion: {question}"

    response = await llm.chat.ainvoke(prompt)
    answer = response.content

    print("-" * 70)
    print(answer)
    print("-" * 70)

    if use_context:
        from sentinel.evaluation.generation_eval import citation_validity

        valid = {r.replace("doc:", "") for r in refs}
        print(
            f"\ncitation validity: {citation_validity(answer, valid):.2f}  "
            f"(fraction of cited ids that were actually retrieved)"
        )
    else:
        print(
            "\nNo context was provided. Anything specific in that answer — a threshold, a "
            "procedure, a number — was invented."
        )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        print(f"tokens: {usage.get('input_tokens')} in, {usage.get('output_tokens')} out")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--provider", default=None, choices=["local", "azure", "aws"])
    parser.add_argument("--role", default="admin")
    parser.add_argument("--no-context", action="store_true", help="run ungrounded, to see what the model invents")
    args = parser.parse_args()

    configure_logging()
    return asyncio.run(run(args.question, args.provider, not args.no_context, args.role))


if __name__ == "__main__":
    sys.exit(main())
