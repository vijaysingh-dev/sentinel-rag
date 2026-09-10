"""Regexes and helpers shared by more than one strategy module.

Kept separate so `_06_runbook`, `_08_semantic` and the sentence-based strategies do not have to
import each other just to reach a compiled pattern.
"""

from __future__ import annotations

import re

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
STEP_SPLIT = re.compile(r"\n(?=\s*(?:\d+[.)]|Step\s+\d+|[-*]\s))", re.IGNORECASE)


def split_sentences(text: str) -> list[str]:
    """Rule-based sentence splitting.

    Good enough for technical prose. Abbreviations such as "e.g." and "Fig. 3" will occasionally
    split wrongly; if that shows up as a measurable recall problem, swap in pysbd or spaCy here —
    but measure first, because the failure is usually harmless.
    """
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s.strip()]
