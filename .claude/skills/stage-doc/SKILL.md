---
name: stage-doc
description: Generate or refresh the deep-dive doc for one Sentinel-RAG pipeline stage (docs/stages/<stage>.md), in the teaching style established for parsing and chunking, then shrink the matching section of docs/STAGE-OVERVIEW.md to a summary + link. Use when the user asks to "walk me through" a stage, write up a stage, or document a stage the way parsing/chunking were documented.
---

# Stage doc generator

This skill produces the deep-dive file for one pipeline stage and wires it into the two-tier
documentation structure this repo uses: `docs/STAGE-OVERVIEW.md` is the map (short, one screen
per stage), `docs/stages/<stage>.md` is the territory (everything — you should never need to
open a source file to answer a question about the stage after reading its doc).

`docs/stages/parsing.md` and `docs/stages/chunking.md` are the reference examples for structure
and depth. Read both before writing anything, even if you remember them from earlier in the
conversation — confirm they haven't changed.

## Argument

`args` is one stage name. Accepted values and what they map to:

| arg | code | scripts | benchmark | relevant ADRs (`docs/design-decisions.md`) |
|---|---|---|---|---|
| `parsing` | `sentinel/parsing/` | `scripts/parsing/` | `benchmarks/parser_bakeoff.py` | ADR-004 |
| `chunking` | `sentinel/chunking/` | `scripts/chunking/` | `benchmarks/chunking_sweep.py` | ADR-003, 007, 008, 009 |
| `embedding` | `sentinel/embedding/` | `scripts/embedding/` | `benchmarks/embedding_bakeoff.py` | ADR-001, 002, 029 |
| `vectorstore` | `sentinel/vectorstore/` | `scripts/vectorstore/` | — (see retrieval_sweep) | ADR-005, 006, 029 |
| `retrieval` | `sentinel/retrieval/` | `scripts/vectorstore/01_*` | `benchmarks/retrieval_sweep.py` | ADR-010, 011, 012, 013 |
| `reranking` | `sentinel/reranking/` | `scripts/reranking/` | `benchmarks/retrieval_sweep.py` | ADR-029 |
| `generation` | `sentinel/generation/` | `scripts/llm/` | — | ADR-028 (superseded), 029 |
| `evaluation` | `sentinel/evaluation/` | `scripts/evaluation/` | all benchmarks feed it | ADR-019, 020, 021, 022, 023 |
| `agent` / `rca` | `sentinel/rca/` (`graph.py`, `agents/`, `tools/`, `state.py`, `runtime.py`, `guard.py`, `clients.py`, `checkpointer.py`, `prompts.py`) | `make chat-server` | `tests/test_trajectory.py`, `data/agent_cases.jsonl` | ADR-013 through ADR-018, ADR-027 |

If no argument is given, ask the user which stage (list the ones still `_(pending)_` in
`docs/STAGE-OVERVIEW.md`'s Contents table).

## Process

1. **Read the exemplars.** `docs/stages/parsing.md` and `docs/stages/chunking.md` in full —
   these set the bar for depth, tone and section order. Also read the target stage's *current*
   section in `docs/STAGE-OVERVIEW.md` — for stages 3–9 this already contains a solid draft
   (what we used, why, a result table, what breaks) written before this skill existed; treat it
   as a source to deepen and verify against real code, not as filler to copy verbatim.

2. **Read every source file for the stage**, per the table above: every module in the code dir
   (every public class/function you're going to explain must actually be read, not guessed at),
   every script, the benchmark file, the relevant ADRs, and any test file that exercises the
   stage's non-negotomable invariants (grep `tests/` for the stage name). If the stage has a
   `CLAUDE.md`-listed invariant, quote it and cite it.

3. **Because this is a lot of reading and writing for one stage, do it in a forked or
   general-purpose subagent when this skill is invoked mid-conversation**, so the raw file
   contents don't stay in the main thread's context. Give that agent this entire skill file's
   content plus the stage argument as its prompt. Have it write the file directly and report
   back a short confirmation, not the file contents.

4. **Write `docs/stages/<stage>.md`** with this structure (match parsing.md/chunking.md's
   headers; adapt section names to what the stage actually needs — not every stage has
   "per-provider mechanics", some have "per-agent mechanics" etc.):

   - Header: stage number, link back to `../STAGE-OVERVIEW.md#N-<slug>`, and a `Code: ... ·
     Scripts: ... · Benchmark: ...` pointer line.
   - **What we used** — the provider/approach table.
   - **The big picture** — 2–4 paragraphs of plain-language framing before any code: what
     problem does this stage solve, phrased as a real tension or trade-off, with a concrete
     analogy a smart high-schooler would get (the way parsing.md leads with "no chunking
     strategy... recovers a flattened table" and chunking.md leads with "two consumers of the
     same text want opposite things"). This is new relative to the two existing docs — write it
     as connective prose, not bullets. Define any jargon the moment you first use it, the way
     CLAUDE.md's house style asks module docstrings to.
   - **Why this stage exists / why these choices** — same depth as the existing docs.
   - **Function/method-by-function walkthrough.** Every public function or method that matters
     gets its own subsection: a short code excerpt or signature, then **what it does** and
     **why it does it that way** (not "what" alone — the why is the point, per the user's
     explicit ask). Use natural-language paragraphs around any code block, not just a bullet
     list under it. This is the section most likely to be too thin — err toward completeness;
     the user explicitly does not want to have to open the source file afterward.
   - **Why these defaults** — a table (constant, value, derivation) for every magic number in
     the stage, in the style of parsing.md's quality-score constants table and chunking.md's
     `550`/`60`/`900`/`92` explanations. If a constant's derivation isn't in a comment or
     docstring, reason about it the same way those two docs do (physics-of-the-problem, cost
     asymmetry, etc.) rather than leaving it unexplained. If you truly cannot derive a reason,
     say so explicitly rather than inventing one — CLAUDE.md's house style values honesty about
     what's known over false precision.
   - **Common questions** — a short FAQ anticipating what someone would ask about this stage in
     review or an interview (aim for 5–10 questions). This is where "whatever I think the user
     is missing" goes: surprising failure modes, common misconceptions, things adjacent stages
     assume about this one.
   - **Result** — the illustrative table already in STAGE-OVERVIEW.md, carried over with the
     same `_(illustrative — run <benchmark>)_` caveat per CLAUDE.md's "when numbers appear in
     documentation" rule. Never present these as measured.
   - **What breaks in production** — same bullet style as the existing docs, one bullet per
     failure mode, each naming the code that catches or causes it.
   - **Files** — a table of every path mentioned, one line each.

5. **Update `docs/STAGE-OVERVIEW.md`**: replace the full section body for that stage with a
   short summary (What we used table, a "Why" paragraph or two, the one-thing-to-remember,
   result table, what-breaks bullets — same shape as sections 1 and 2 there today) ending in
   `**→ Full detail: [`stages/<stage>.md`](stages/<stage>.md)**`, and flip that stage's row in
   the Contents table from `_(pending)_` to the link. Do this edit yourself in the main thread
   after the subagent(s) finish, so all sections stay consistent with each other — don't let
   parallel subagents each rewrite the shared overview file.

## Content bar (do not skip any of these)

- Self-contained: after reading the stage doc, the user should be able to answer any question
  about that stage without opening another file.
- Every function/method that matters is walked through: what happens, and why.
- Every default value's derivation is explained, not just stated.
- Long enough to actually cover the stage — err long, the way parsing.md and chunking.md are
  ~250–350 lines.
- Written so a smart high-schooler could follow it: explain jargon on first use, use analogies,
  prefer natural-language paragraphs over compressed bullet fragments — while keeping the
  precision and technical honesty of the existing two docs. Accessible language, not reduced
  substance.
- Say what you think the user is missing, even if they didn't ask — a gotcha, a common
  misconception, a place this stage's assumptions get violated by another stage.
- Every claim is backed by code you actually read or a script that actually exists, per
  CLAUDE.md's "nothing in it is an assertion you cannot demonstrate."
