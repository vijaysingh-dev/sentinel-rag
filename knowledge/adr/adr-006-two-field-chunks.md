# ADR-006: A chunk carries a separate embed_text and context_text

**Status:** Accepted

## Context

The retriever wants small, single-topic units so the embedding vector is meaningful. The generator
wants enough surrounding context to actually answer — the heading the step sits under, the
sentence before the one that matched, the table's column headers. One text field cannot serve
both.

## Decision

Chunk has two text fields. embed_text is vectorised and kept tight. context_text is handed to the
LLM and may be larger — the parent section, a sentence window, the whole procedure. They are equal
by default and deliberately need not be.

## Alternatives considered

- **One text field, tuned chunk size.** Every size is a compromise between the two consumers; sentence-window and parent-document strategies cannot be expressed at all.
- **Store only embed_text, re-fetch context at query time.** Doubles the read path and couples retrieval latency to a second lookup; the context is already known at chunk time, so store it then.

## Consequences

Positive: the retriever and the generator each get the text shaped for them; advanced chunking
strategies become expressible. Negative: it is one more thing to get right — embed_chunks must
embed embed_text and never context_text, and getting that backwards silently destroys retrieval
quality with no error. That specific mistake is called out in CLAUDE.md and guarded in the ingest
tests.
