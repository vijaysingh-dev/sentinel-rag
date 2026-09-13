"""Check which providers are actually usable in this environment.

    python scripts/00_doctor.py

Run this first. It probes every adapter without doing real work, so a missing SDK or an absent
credential is reported as one clear line here rather than as an ImportError three modules deep
during a twenty-minute ingestion run.
"""

from __future__ import annotations

import sys

from sentinel.core.config import settings
from sentinel.core.observability import configure_logging
from sentinel.core.registry import describe


def main() -> int:
    configure_logging()

    print("configured providers")
    print(f"  parse        {settings.parse_provider}  (strategy: {settings.parse_strategy})")
    print(f"  embed        {settings.embed_provider}  ({settings.embedding_model}, {settings.embedding_dims}d)")
    print(f"  vectorstore  {settings.vectorstore_provider}")
    print(f"  rerank       {settings.rerank_provider}")
    print(f"  llm          {settings.llm_provider}")
    print(f"  index version {settings.index_version}")

    print("\navailability")
    problems = 0
    for stage, providers in describe().items():
        print(f"  {stage}")
        for info in providers:
            if info.available:
                print(f"    ok          {info.name}")
            else:
                problems += 1
                print(f"    unavailable {info.name:8s} {info.reason[:70]}")

    print(
        "\nUnavailable providers are only a problem if you intend to use them. Install extras "
        "with: pip install -e '.[azure]' (parsing), '.[aws]' (parsing) or '.[inference]' "
        "(openai/anthropic/google/cohere)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
