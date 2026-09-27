"""Chunking strategies.

Each strategy is registered by name and takes a `ParsedDocument`, so they are directly comparable
in a benchmark sweep. Read the modules in numbered order — they build on one another, and the
progression is the argument for why the later ones exist:

    _01_fixed             the control group
    _02_recursive         respect boundaries when you can  (the shared split engine)
    _03_structural        recursive inside heading sections, heading path prefixed  (the default)
    _04_sentence_window   embed one sentence, return its neighbours
    _05_parent_document   embed children, return the parent section
    _06_runbook           embed steps, always return the whole procedure   (doc-type specific)
    _07_glossary          one term per chunk                               (doc-type specific)
    _08_semantic          cut where meaning shifts  (embeds at chunk-decision time)
    _09_contextual        LLM-situate each chunk    (not registered)

Choosing between them is an empirical question, and `benchmarks/chunking_sweep.py` answers it for
a given corpus. The defensible default is `structural`: recursive splitting inside heading
sections, with the heading path prefixed onto every chunk. That prefix is usually worth more than
switching embedding models, because it turns unretrievable fragments into findable ones.

Importing this package runs every module's `@register`, so `sentinel.chunking` importing
`strategies` is what populates the registry. The names below are re-exported so
`from sentinel.chunking.strategies import StructuralChunker` keeps working after the split.
"""

from __future__ import annotations

from sentinel.chunking.strategies._00_shared import SENTENCE_SPLIT, STEP_SPLIT, split_sentences
from sentinel.chunking.strategies._01_fixed import FixedTokenChunker
from sentinel.chunking.strategies._02_recursive import RecursiveChunker
from sentinel.chunking.strategies._03_structural import StructuralChunker
from sentinel.chunking.strategies._04_sentence_window import SentenceWindowChunker
from sentinel.chunking.strategies._05_parent_document import ParentDocumentChunker
from sentinel.chunking.strategies._06_runbook import RunbookChunker
from sentinel.chunking.strategies._07_glossary import GlossaryChunker
from sentinel.chunking.strategies._08_semantic import SemanticChunker
from sentinel.chunking.strategies._09_contextual import ContextualChunker
from sentinel.core.types import ParsedDocument

__all__ = [
    "FixedTokenChunker",
    "RecursiveChunker",
    "StructuralChunker",
    "SentenceWindowChunker",
    "ParentDocumentChunker",
    "RunbookChunker",
    "GlossaryChunker",
    "SemanticChunker",
    "ContextualChunker",
    "SENTENCE_SPLIT",
    "STEP_SPLIT",
    "split_sentences",
    "DOC_TYPE_STRATEGY",
    "chunker_for_doc",
]

# Document type drives strategy. This mapping is the practical answer to "how do you chunk?" —
# not one strategy, but a routing table justified per content type.
DOC_TYPE_STRATEGY = {
    "runbook": "runbook",
    "glossary": "glossary",
    "methodology": "structural",
    "incident": "structural",
    "factsheet": "structural",
    "adr": "structural",
    "platform": "structural",
    "page": "structural",
}


def chunker_for_doc(doc: ParsedDocument, override: str | None = None):
    from sentinel.chunking.base import get_chunker

    if override:
        return get_chunker(override)
    return get_chunker(DOC_TYPE_STRATEGY.get(doc.doc_type, "structural"))
