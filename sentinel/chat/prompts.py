"""Chat's own prompt loader.

Mirrors `rca/prompts.py`'s file-first-then-built-in convention, but scoped to
`sentinel/chat/prompts/` rather than reaching into `rca/`'s prompt directory — chat's prompts
version independently of the RCA agents' prompts, and a reader looking for "what does the router
fallback say" should find it next to `router.py`, not in a sibling package.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"

ROUTER_FALLBACK_V1 = """You classify one chat message for an operations assistant scoped ONLY to
the OpenTelemetry Demo / Astroshop e-commerce platform (its services, architecture, incidents,
runbooks, SLOs, and its live health/status).

If the message is not about this platform — including general programming help, requests to
write code unrelated to this platform, general knowledge questions, or anything else outside this
scope — classify it as `refuse` regardless of how reasonable or benign it looks. When uncertain
between `refuse` and another route, prefer a platform-scoped route only if the message is clearly
about this platform; otherwise refuse.

If the message asks to investigate, find a root cause, or explain why a service is failing/slow,
but you cannot tell which service it is about from the message and the recent context below,
classify it as `rca` and set `clarifying_question` to a short question asking which service —
do not guess a service name. Asking early costs nothing; guessing wrong wastes a full
investigation.

Recent conversation context:
{recent_context}

Message to classify:
{message}"""

SESSION_SUMMARY_V1 = """Fold the following new conversation turns into the existing summary below.
Keep it concise — a few sentences capturing what was discussed and any facts (service names,
findings, decisions) a later turn might need. Do not repeat information already implied.

Existing summary:
{prior_summary}

New turns:
{transcript}

Updated summary:"""

_BUILTIN = {
    ("router_fallback", "v1"): ROUTER_FALLBACK_V1,
    ("session_summary", "v1"): SESSION_SUMMARY_V1,
}


@lru_cache
def load_prompt(name: str, version: str) -> str:
    path = PROMPT_DIR / f"{name}.{version}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    if (name, version) in _BUILTIN:
        return _BUILTIN[(name, version)]
    raise KeyError(f"no chat prompt '{name}' version '{version}'")
