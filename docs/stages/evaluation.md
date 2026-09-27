# Evaluation

> Stage 8 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#8-evaluation); this file is the deep version.
>
> Code: `sentinel/evaluation/` (`metrics.py`, `golden.py`, `gate.py`, `parsing_eval.py`, `retrieval_eval.py`, `generation_eval.py`, `agent_eval.py`) · Scripts: `scripts/evaluation/01_evaluate_stages.py`, `scripts/evaluation/02_evaluate_agent.py`, `scripts/evaluation/02_backfill_golden_chunk_ids.py` · Every benchmark in `benchmarks/` ultimately feeds this layer.

---

## The big picture

Imagine you changed the recipe for your pipeline — swapped the embedding model, tweaked a chunk size, edited a prompt — and someone asks "did that make things better or worse?" Without this stage, the honest answer is "I re-ran a couple of questions by hand and it felt fine." That's not a real answer. It's a guess wearing a lab coat.

Evaluation is the stage that turns "it seems better" into a number you can put in a pull request. It works by keeping a fixed, curated set of questions with known-correct answers — the **golden set** — and re-running the whole system (or just one stage of it) against that set every time something changes. If the numbers hold or improve, ship it. If they drop, you caught a regression before a user did.

The tricky part is that a RAG pipeline has two very different kinds of things to measure, and they need two very different kinds of judges:

- **Did we find the right information?** This is retrieval — did the chunk containing the answer show up in the results? This question has a mechanically checkable answer: either chunk `abc123` is in the top 5 results or it isn't. No opinion required, no LLM required, just set arithmetic. Fast, free, and perfectly repeatable.
- **Did we say the right thing with it?** This is generation — did the final English sentence the model wrote actually reflect what the retrieved documents said, without making anything up? This is not something you can check with `==`. You need something that can *read* — which means another LLM, acting as a judge. That judge costs money, takes time, and (this is the part worth being honest about) is not perfectly consistent from one run to the next.

That distinction — cheap-and-deterministic vs. expensive-and-judged — is the organizing idea of this entire stage. Everything else (what runs on every pull request vs. what runs overnight, why some scores are hard floors and others are soft) falls out of it.

There's a second big idea worth understanding before you read any code: **a metric you never look at is worse than no metric at all**, because it creates false confidence. A dashboard nobody checks, a test suite nobody runs, a golden set that's 90% easy questions — these all *look* like rigor while quietly measuring nothing that matters. Most of the design decisions below (the coverage-balance checks, the safety gates that are never averaged, the two-tier threshold system) exist specifically to stop that from happening here.

---

## What we used

Four layers, ordered by how expensive and how deterministic they are.

| Layer | File | Metrics | LLM needed? | Runs |
|---|---|---|---|---|
| Parsing accuracy | `parsing_eval.py` | text similarity, table F1, reading-order tau, heading recall | no | whenever the corpus or a parser changes |
| Retrieval quality | `retrieval_eval.py` (+ `metrics.py`) | recall@k, precision@k, MRR, nDCG, hit rate | no | **every pull request** |
| Generation quality | `generation_eval.py` | faithfulness, answer relevance, citation validity, refusal accuracy | **yes** | nightly |
| Agent behaviour | `agent_eval.py` | outcome accuracy, trajectory (route accuracy, tool precision/recall), safety violations | yes (fixtures make the tools deterministic) | every pull request that touches `rca/` |

On top of all four sits `gate.py`, which turns a dictionary of numbers into a pass/fail verdict for CI, and `golden.py`, which manages the question set every layer is scored against.

---

## Why the split exists: putting a non-deterministic system in CI

CI (continuous integration — the automated checks that run every time someone proposes a code change) wants two things that are in tension with a RAG pipeline: it wants to be **fast** (nobody waits twenty minutes for a merge check) and it wants to be **trustworthy** (a green checkmark should mean something). A retrieval score is fast and trustworthy — it's arithmetic over a fixed set of chunk ids, so running it twice gives you the same number both times. An LLM-judged faithfulness score is neither: it costs real API calls, takes real wall-clock time across 46 examples, and because language models have some randomness in how they generate text, judging the same answer twice can occasionally give you a slightly different score.

If you tried to gate every pull request on the expensive, noisy layer, two things would happen: your CI would get slow enough that people start ignoring it, and it would occasionally fail merges for no real reason (judge noise), which trains people to click "merge anyway" — the single most effective way to make a safety check worthless. So the deterministic layer blocks merges immediately, and the judged layer runs on a schedule (nightly) where a bit of latency and a bit of noise are both fine, and its results are read as a trend rather than a single verdict.

---

## Layer 1: retrieval metrics — the ones with no LLM in them (`metrics.py`)

Every function here answers a slightly different version of "did the right document come back?", and they're all pure math over two things: a list of retrieved chunk ids (ordered — position matters) and a set of chunk ids that are actually correct for this question.

### `hit_rate(retrieved, relevant, k=5)`

```python
def hit_rate(retrieved, relevant, k=5):
    return 1.0 if set(retrieved[:k]) & relevant else 0.0
```

**What it does:** looks at only the top `k` results, and asks the blunt yes/no question — is *any* correct chunk in there at all? **Why it's useful despite being crude:** it's the easiest number to explain to a non-technical stakeholder ("6 out of 10 questions found something relevant in the top 5"), and it's a reasonable sanity check before you trust the fancier metrics below.

### `recall_at_k(retrieved, relevant, k)`

```python
def recall_at_k(retrieved, relevant, k):
    if not relevant:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)
```

**What it does:** of all the chunks that are *actually* correct for this question, what fraction did we manage to pull into the top `k`? If a question has 4 correct chunks and 3 of them are in the top 50, recall@50 is 0.75. **Why this one matters most:** it's the metric that tells you whether the answer *exists somewhere in your candidate pool at all*. Everything downstream — reranking, the LLM writing an answer — can only work with what recall handed it. A reranker cannot promote a chunk that was never retrieved.

### `precision_at_k(retrieved, relevant, k)`

```python
def precision_at_k(retrieved, relevant, k):
    if k == 0:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / k
```

**What it does:** the mirror image of recall — of the `k` chunks we actually showed, how many were correct? **Why it matters separately from recall:** recall can be gamed by returning everything (retrieve the whole corpus and you'll never miss a correct chunk), but that tanks precision, because now most of what you handed the generator is noise. A generator with a tight token budget cares about precision; a retriever deciding how wide to cast its net cares about recall. You need both numbers to know whether a system is actually good or just indiscriminate.

### `mrr(retrieved, relevant)` — Mean Reciprocal Rank

```python
def mrr(retrieved, relevant):
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0
```

**What it does:** finds the position of the *first* correct result and returns 1 divided by that position. First place scores 1.0, second place scores 0.5, tenth place scores 0.1, not-found scores 0. **Why this is the right metric for most real questions:** most operational lookups (like "what's the retry policy for the checkout service?") have essentially one right answer, and what the user cares about is whether it appeared *first*, not whether three other related documents also technically showed up somewhere in the list. Recall doesn't capture "but did I have to scroll" — MRR does.

### `ndcg_at_k(retrieved, relevance, k)` — Normalized Discounted Cumulative Gain

```python
def ndcg_at_k(retrieved, relevance, k):
    gains = [relevance.get(doc_id, 0.0) for doc_id in retrieved[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0
```

**What it does:** this is the most sophisticated of the ranking metrics, and it does two things the others don't. First, it supports *graded* relevance — a chunk doesn't have to be simply "correct" or "wrong", it can be "somewhat relevant" (`relevance` is a dict mapping chunk id → a relevance score, not just a set). Second, it applies a **position discount**: `1 / log2(rank + 2)` means a relevant result at rank 1 contributes close to its full value, but the same result at rank 10 contributes much less. The `log2` curve (rather than a straight `1/rank`) is the standard choice from information-retrieval research — it discounts more gently than reciprocal rank, reflecting that users do sometimes look past the very first result but rarely scroll deep. Dividing by `idcg` (the score of a *perfectly* ordered list) normalizes the result to `[0, 1]`, so a score of 0.9 always means "90% as good as the best possible ordering," regardless of how many relevant documents existed for that particular question. **Why it's here at all when MRR and recall already exist:** nDCG is the one metric that would catch a subtle ranking regression — say, the second-most-relevant result moved from position 2 to position 4 — that MRR (which only cares about the *first* hit) would completely miss.

### `average_precision(retrieved, relevant)`

```python
def average_precision(retrieved, relevant):
    hits, total = 0, 0.0
    for i, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            hits += 1
            total += hits / i
    return total / len(relevant)
```

**What it does:** walks down the ranked list, and every time it hits a correct result, it records the precision *at that point* (how many correct results have I seen out of how many total results so far), then averages those snapshots. **Why it exists alongside nDCG:** it rewards getting *all* the correct results bunched near the top, not just the first one — useful when a question genuinely has several equally-important correct chunks (a multi-hop question, for instance) and you want to penalize a system that finds one of them early but scatters the rest of them at ranks 30, 40, 50.

### `substring_hit(contexts, substrings)`

```python
def substring_hit(contexts, substrings):
    joined = " ".join(contexts).lower()
    return sum(1 for s in substrings if s.lower() in joined) / len(substrings)
```

**What it does:** rather than checking "did the exact right chunk id come back," it checks "does the retrieved text *contain* certain expected words or phrases." **Why this exists as a fallback rather than the main metric:** chunk-id-based recall requires the golden set to be labeled with precise chunk ids, and chunk ids change whenever the chunker changes (they're content-derived — see the chunking stage doc). Early in a project, or right after a chunking change, your golden set's chunk-id labels can go briefly stale while the substance of the documents hasn't changed at all. Substring matching survives that: "the answer mentions SLO and unreliability somewhere in what we retrieved" is still meaningful even if the exact chunk boundaries moved. It's a pragmatic patch, not a replacement — chunk-id recall is strictly more precise when it's available, because a substring can appear in a chunk that's contextually wrong (imagine the word "timeout" appearing in an unrelated document).

### `aggregate(rows, keys=None)`

**What it does:** takes a list of per-question metric dictionaries and averages each column, producing one summary dict. **The one subtlety worth knowing:** if you don't pass `keys` explicitly, it doesn't just look at what keys the *first* row has — it unions the keys across *every* row. This matters because different question categories produce different metrics: an `acl_denied` question only ever has an `acl_respected` field, an `unanswerable` question has `retrieved_nothing`, and even among ordinary answerable questions, `recall`/`mrr`/`ndcg` are only computed when the golden example actually has `relevant_chunk_ids` populated (some only have `answer_substrings`). If `aggregate` only trusted `rows[0]`'s keys and the very first example in your golden set happened to be an ACL case, every recall/MRR number for the whole run would silently vanish from the report. Unioning across all rows is what prevents that.

### `bootstrap_ci(values, iterations=1000, confidence=0.95)`

```python
def bootstrap_ci(values, iterations=1000, confidence=0.95):
    means = []
    n = len(values)
    for _ in range(iterations):
        sample = [values[random.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((1 - confidence) / 2 * iterations)]
    hi = means[int((1 + confidence) / 2 * iterations) - 1]
    return (round(lo, 4), round(hi, 4))
```

**What it does — the plain-English version:** you have 46 golden questions and you computed an average recall of, say, 0.82. But that 0.82 came from a small sample — if you'd happened to pick 46 *slightly different* questions, would you have gotten 0.82 again, or could it easily have been 0.75 or 0.89? Bootstrapping answers that by simulation: it repeatedly builds a fake "resample" by drawing 46 values *with replacement* from the ones you actually have (so the same real value can be picked more than once, or not at all, in any given resample) and recomputes the average each time. Do that 1000 times, sort the 1000 resulting averages, and the range covering the middle 95% of them is your confidence interval — a plausible range for what the "true" average would be if you had infinite questions instead of just 46. **Why this is worth the extra computation:** with a golden set this small, a "2-point improvement" (0.82 → 0.84) is exactly the kind of thing that could just be noise. Reporting a bare delta without a confidence interval is precisely how teams end up shipping a regression that "looked like an improvement" — the interval is what stops you from fooling yourself. `scripts/evaluation/01_evaluate_stages.py` prints these intervals next to `recall@5` and `mrr`.

---

## Layer 1, applied: `retrieval_eval.py`

`evaluate_retrieval(pipeline, examples, k_candidates=50, k_final=5)` is the function that actually runs every golden question through the live retrieval pipeline and scores it. A few things worth understanding about *how* it does that:

**It retrieves twice per question, at two different widths.** Once asking for the wide candidate pool (`top_n=k_candidates`, i.e. 50) and once asking for the final, reranked short list (`top_n=k_final`, i.e. 5). This isn't wasted work — it's what makes the "which stage is broken" diagnosis possible (see below): recall@50 tells you what the candidate-generation half of the pipeline (hybrid search + fusion) found, and recall@5 tells you what survived after reranking cut it down.

**Three question categories get scored completely differently, on purpose**, because "success" means something different for each:

- **Ordinary answerable questions** get the full suite: recall@50, recall@5, precision@5, hit@5, MRR, nDCG@5, and (if the example has `answer_substrings`) a substring check on top.
- **`acl_denied` questions** — where the correct behaviour is that the system finds *nothing*, because the caller's role isn't allowed to see that document — get scored inverted: `acl_respected = 1.0` means the final result list came back **empty**. Averaging these into the normal recall score would be actively misleading, because for this category a *low* "recall" is the success case. They're tracked as their own metric specifically so a security failure can't hide inside a blended average.
- **`unanswerable` questions** — ones where the corpus genuinely has no answer — get scored on `retrieved_nothing`: did the relevance floor correctly suppress everything, or did the system confidently hand back five chunks that don't actually answer the question? This is measuring whether the "I don't know" behaviour actually works, which is a different thing entirely from ranking quality.

**Why this three-way split is not optional:** if you dump `acl_denied` and `unanswerable` rows into the same recall average as everything else, the number that comes out the other end doesn't mean anything — you'd be averaging "did we find the right thing" with "did we correctly find nothing," which are opposite goals wearing the same units.

`to_stage_result(...)` just repackages the result into the shared `StageResult` dataclass so retrieval fits into the same benchmark-comparison machinery every other stage uses.

---

## Layer 2: generation metrics — where an LLM has to be the judge (`generation_eval.py`)

This is the layer that grades the actual English sentences the system produces, and it's the one where you have to be honest that you're asking a model to grade another model — with everything that implies about consistency and bias.

### The four known judge biases, and what stops each one

An "LLM as judge" setup fails in specific, well-documented ways. This codebase names each one and pairs it with a concrete fix rather than hoping it doesn't happen:

| Bias | What goes wrong | The fix here |
|---|---|---|
| **Position bias** | A judge shown two answers side by side tends to prefer whichever one it saw first, regardless of actual quality. | Never compare pairwise. Score one answer at a time against a fixed rubric (`AnswerJudgement`'s explicit criteria), so there's no "first" to be biased toward. |
| **Verbosity bias** | Longer answers get scored higher even when they're not more correct — padding looks like thoroughness to a judge reading quickly. | Faithfulness is measured by decomposing the answer into atomic (single-fact) claims and checking each independently. A rambling answer just produces more claims to individually verify — length stops being a shortcut to a high score. |
| **Self-preference** | A model tends to rate its own writing style more favorably than another model's. | The judge model is deliberately a different model family from whatever generated the answer being graded. |
| **Rationale-after-score** | If you ask a model for a numeric score *and then* ask it to explain, it tends to produce a score first and then invent a justification that fits — because the explanation is generated after, and conditioned on, the score it already committed to. | Every judge schema in this codebase (`AnswerJudgement`, `ClaimVerdict`, and others elsewhere like `RouteDecision`) puts the `rationale` field *first* in the Pydantic model, forcing the model to write its reasoning before it writes the number. See "why field order matters" below. |

### Why field order in a Pydantic schema changes model behaviour

This is one of those details that looks like a stylistic nitpick and is actually load-bearing. Language models generate structured output token by token, left to right, and each token is conditioned on everything generated before it (this is what "autoregressive" means). If `score` is the first field in the schema, the model commits to a number before it has written a single word of reasoning — and everything it writes in the `rationale` field afterward is then *conditioned on* having already picked that number, which nudges it toward justifying rather than genuinely reasoning. Put `rationale` first, and the score the model eventually writes is conditioned on the reasoning that came before it — cause and effect in the right order. It costs a few more output tokens per judgement call and, per the ADR that documents this (ADR-020), measurably improves how well the judge's scores agree with human-labeled scores. It's a free win, so it's used everywhere a judge produces a score in this codebase.

### `faithfulness_score(answer, context, judge)` — the most important metric in this file

```python
async def faithfulness_score(answer, context, judge):
    decomposition = await decomposer.ainvoke(DECOMPOSE_PROMPT.format(answer=answer))
    claims = decomposition.claims
    if not claims:
        return 1.0, []
    results = await asyncio.gather(*(verify(c) for c in claims))
    unsupported = [claim for claim, ok in results if not ok]
    return (len(claims) - len(unsupported)) / len(claims), unsupported
```

**What it does, step by step:** first, an LLM call breaks the generated answer into a list of individually-checkable factual claims ("the checkout service uses a circuit breaker," "the timeout is set to 30 seconds," etc.) — this is the `ClaimDecomposition` step. Then, for *each* claim independently, a second LLM call decides whether the retrieved context actually supports that specific claim, running all the checks concurrently with `asyncio.gather` for speed. The final score is simply the fraction of claims that were supported.

**Why decomposing into claims beats grading the answer as a whole:** picture an answer with eight correct facts and one completely fabricated one. A judge asked to grade the *whole answer* on a 0–1 scale has to somehow compress that mix into one number — maybe it says 0.85, maybe 0.7, depending on how much the fabrication bothered it that particular run. Either way, you've lost the one piece of information you actually needed: *which specific claim was made up*. Decomposition surfaces that directly — you get back a list of exactly which sentences weren't backed by anything real, which is what an engineer reviewing the failure actually needs to see. This is also why faithfulness is singled out as "the metric that matters most in an operations context": a fluent, well-cited, confidently *wrong* answer is the specific failure mode that gets someone to run the wrong runbook during an actual incident. A vague or hedgy answer is annoying; a wrong-but-convincing one is dangerous.

**A quiet but important detail — "fail closed":** the `verify` helper catches any exception from the judge call and returns `False` (unsupported) rather than letting the exception propagate or silently skipping that claim. If the judge itself fails for one particular claim, that claim is counted against the answer, not ignored. Think about the alternative: if a judge timeout meant "don't count this claim either way," a flaky judge API would quietly *inflate* your faithfulness scores by shrinking the denominator of claims actually being checked. Counting a failed check as "unsupported" is the conservative, safe-by-default choice.

**Also worth noticing — the empty-claims case returns 1.0, not 0.0:** `if not claims: return 1.0, []`. If the decomposer found *zero* factual claims in the answer (e.g. it's a pure refusal like "I don't have information on that"), there is nothing to contradict, so it can't be *unfaithful* — vacuous truth. This matters because refusal correctness is measured separately anyway (see below), so this branch isn't secretly grading refusals as good; it's just correctly declining to penalize an answer for facts it never asserted.

### `citation_validity(answer, valid_ids)`

```python
def citation_validity(answer, valid_ids):
    cited = set(re.findall(r"\[([a-f0-9:_\-]{8,})\]", answer))
    if not cited:
        return 0.0
    return len(cited & valid_ids) / len(cited)
```

**What it does:** pulls every citation-looking token out of the answer with a regex (looking for `[hex-ish-id]` patterns matching chunk id shapes) and checks what fraction of them correspond to chunk ids that were *actually* in the retrieved context, i.e. `valid_ids`. **Why this needs no LLM at all:** this is a pure membership check — either the cited id is one the system genuinely retrieved, or it's a hallucinated reference that looks plausible but points at nothing real. That's a `set` intersection, and paying for an LLM call to do arithmetic a computer already does exactly would be wasteful and would introduce judge noise into a question that has an unambiguous right answer. It's called out in the docstring as "one of the cheapest useful metrics in the suite" for exactly that reason — maximum signal, essentially zero cost.

### `_looks_like_refusal(answer)`

**What it does:** a plain keyword search (`"not documented"`, `"couldn't find"`, `"don't have"`, `"escalate"`, etc.) that decides whether an answer reads as a refusal. **Why keyword matching rather than another LLM judge:** refusal is a narrow, stereotyped kind of language — the system was explicitly prompted to phrase a refusal a certain way, so it isn't a subtle judgment call the way "is this faithful" is. Spending an LLM call (with its attendant cost and run-to-run variance) to detect something a fixed word list already catches reliably would be adding noise for no accuracy gain.

### `evaluate_generation(examples, answers, judge)` — how refusal and answered questions are scored differently

For any golden example whose `expected_behaviour` is `"refuse"` or `"escalate"`, the function takes a completely different scoring path than for ordinary answerable questions: it checks whether the answer *reads* as a refusal (`_looks_like_refusal`) **and** whether it avoided mentioning any of the `must_not_contain` terms — because for an ACL-restricted document, refusing to answer while still naming the document ("I can't share details of the payment-service incident post-mortem with you") has already leaked that the incident happened, which defeats the entire point of the refusal. Both conditions have to hold for `refusal_correct` to be 1.0. For ordinary answerable examples, it runs `faithfulness_score`, `citation_validity`, and a general `AnswerJudgement` (addresses the question? uses only the given context? appropriately hedged when the context is thin? is it actionable?) — all in parallel across the whole golden set via `asyncio.gather`.

**Why answers are passed in as an argument rather than generated inside this function:** `answers: dict[str, dict]` is supplied by the caller, which means the exact same evaluation code can grade answers that came from an offline batch run *or* answers pulled from real production traces. The evaluator doesn't care where the text came from, only whether it holds up — which is what lets the same faithfulness/citation logic double as an offline benchmark and a production quality-monitoring tool (see "online evaluation" below).

---

## Layer 3: agent evaluation — three ways a multi-step system can fail (`agent_eval.py`)

Everything above scores a single retrieval or a single generated answer in isolation. The RCA agent (see the agent-layer stage doc) is different: it's a multi-step investigation that calls tools, reasons across several turns, and eventually writes a finding. A single "was the final answer right" score would hide a lot of interesting failure — which is why this layer splits into three independent dimensions.

### Why three layers instead of one score

- **Outcome** — was the *final* answer correct? This is what a user ultimately cares about, but it's dangerously incomplete on its own: an agent can stumble into a correct answer after nine wasted tool calls and a lucky guess, and an outcome-only score calls that a success — teaching you nothing about whether the *reasoning* was any good, or whether you'll get that lucky again next time.
- **Trajectory** — did it take a *sensible path* to get there? This is what catches "right answer, terrible process" — the agent looping on the same tool call, skipping a check it should have made, or wandering through specialists it didn't need. This is also, empirically, the layer that degrades first and fastest when someone tweaks a prompt, which makes it the most sensitive early-warning signal of the three.
- **Safety** — did it stay inside its boundaries? No unapproved writes, no leaked ACL-restricted content, no claim invented out of thin air. These are treated as pass/fail gates, never averaged into a percentage — more on why below.

### `score_trajectory(tool_calls, expected_tools, max_steps=12)`

```python
precision = len(called_set & expected_set) / len(called_set) if called_set else 0.0
recall = len(called_set & expected_set) / len(expected_set) if expected_set else 1.0
```

**What it does:** compares the set of tools the agent actually called against the set the fixture says it *should* have called for this incident scenario, computing precision and recall over that set. **Why both, reported separately, rather than one combined number:** they represent two different kinds of risk, and conflating them would hide which one you're looking at. Low recall means the agent *skipped* a tool it needed — for instance, it never checked the metrics before drafting a root-cause finding — which is a **correctness risk**: the finding could simply be wrong. Low precision means the agent called extra tools it didn't need — it's thorough to a fault, burning time and API cost — which is a **cost risk**, not a correctness one. A system that's slow-but-right and a system that's fast-but-wrong both look identical if you only report one blended F1-style number; keeping them apart tells you which problem you're actually looking at.

**The repeated-call detector:**
```python
signatures = [f"{c['name']}:{json.dumps(c.get('args', {}), sort_keys=True)}" for c in tool_calls]
repeated = len(signatures) - len(set(signatures))
```
Builds a string signature of each call (tool name plus its exact arguments, with keys sorted so argument-order differences don't create false mismatches), and counts how many of those signatures repeat. **Why this matters:** an agent calling the *identical* tool with the *identical* arguments more than once in a single run is the textbook signature of a reasoning loop — it got confused, forgot it already has that information, and asked again. This is cheap to detect and catches a failure mode that would otherwise just look like "took a while."

### `check_safety(state, evidence_refs)` — the hard invariants

Three checks, each one directly encoding a non-negotiable rule from `CLAUDE.md`:

1. **`unapproved_write`** — if the state reached `"published"` without a recorded `approved_by`, that's a violation. This directly checks the invariant that `publish_node` is unreachable without human approval (see the agent-layer stage doc and ADR-016).
2. **`fabricated_reference`** — walks every piece of evidence in the state and checks its `source_ref` actually appears in `evidence_refs` (the set of refs that genuinely came back from real tool calls during this run). A reference that doesn't check out means the agent's evidence includes a citation to something that never actually happened — which is exactly the class of bug `observed_refs()` (in the agent layer) is supposed to prevent from ever reaching this point, so seeing one here means that mechanism itself has a gap.
3. **`overconfident_degraded`** — if the run is marked `degraded` (some tool or data source failed partway through) but the draft's confidence is still above 0.7, that's a violation. A degraded investigation is, by definition, working with incomplete information, and presenting it with high confidence anyway is actively misleading to whoever reads the finding.

**Why these are never averaged into a percentage score, unlike everything else in this stage:** picture a report that says "98% of runs correctly enforced human approval before publishing." That statement sounds almost fine — but it means 2% of runs *published something a human never approved*, which is not a quality issue, it's an incident. Averaging turns a hard boundary into a fuzzy trend line, and a fuzzy trend line is exactly the kind of thing that's easy to shrug off under deadline pressure ("only 2%, we'll fix it next sprint"). Keeping these as an explicit, all-or-nothing list of violations — and having `AgentEvalResult.passed` return `False` if that list is non-empty at all — makes them impossible to average away.

### Why fixtures instead of a live otel-demo run

`evaluate_agent(cases, runner)` takes a `runner` callable rather than calling the live graph directly, and the eval cases in `data/agent_cases.jsonl` bind to recorded tool output in `data/fixtures/` rather than a running Prometheus/Jaeger. Two reasons: a live run against otel-demo's incident generator isn't reproducible (it picks fault scenarios semi-randomly, so the same "test" could exercise a completely different failure each run), and it isn't something you can commit as a CI artifact anyway (it needs live infrastructure up). By recording the tool outputs a real investigation would see for one specific, known fault scenario and replaying them deterministically, the evaluation isolates exactly the thing worth testing on every pull request: the agent's *reasoning*, not whether Prometheus happened to be up. Genuinely interesting live failures are still valuable — they just get captured and turned into new fixture-backed cases rather than run live in CI (see "Known gaps" in `CLAUDE.md`).

---

## The golden set: the highest-leverage artifact in this whole stage (`golden.py`)

Every layer above is only as good as the questions it's tested against. If the golden set is bad — too easy, too narrow, missing the hard cases — every green checkmark this stage produces is measuring the wrong thing.

### `GoldenExample` — why it has the fields it has

Each entry is one evaluation case. Two fields do work that's easy to underestimate:

- **`role`** exists because this is a role-scoped system (see `SearchFilter` in the vector-store stage) — the *same question* can have a different correct answer depending on who's asking. "What's the payment gateway's failover procedure?" might be fully answerable for an `admin` role and correctly refused for a `viewer` role. Without `role` on the golden example, you couldn't even express that as a test case.
- **`expected_behaviour: Literal["answer", "refuse", "escalate"]`** exists because it lets "the system correctly said nothing" be a *passing* result rather than something the eval harness has no way to represent. Without this field, you could only measure "did the system produce a good answer" — which makes it structurally impossible to test whether the system hallucinates when it shouldn't be answering at all. That's arguably the single most important thing an operations assistant needs to get right, and a golden set without this field can't check it.

### `TARGET_MIX` and `check_coverage` — keeping the set honest

```python
TARGET_MIX = {
    "terminology": 0.10, "howto": 0.20, "design_rationale": 0.10, "lookup": 0.15,
    "live_status": 0.10, "mixed": 0.10, "unanswerable": 0.15, "acl_denied": 0.10,
}
```

**What this is:** the minimum share of the golden set each question category should occupy. **Why it exists:** it is very easy — and very tempting under time pressure — for a golden set to drift toward being 90% easy, straightforward, answerable lookup questions, because those are the fastest to write and the fastest to pass. A set like that will report a beautiful, climbing accuracy number release after release, right up until a real user asks something the corpus doesn't cover and the system confidently makes something up — a failure mode the golden set was never actually testing for. `check_coverage()` computes the observed mix and flags any category sitting below 60% of its target share (`observed < target * 0.6`), which is meant to make a golden set that's quietly become all-easy-questions impossible to miss in a PR review: it shows up as an explicit warning, not a silently-passing green check.

**Why `unanswerable` (15%) and `acl_denied` (10%) specifically get guaranteed minimums:** these are the two categories whose entire purpose is measuring the *negative* case — does the system correctly say nothing when it should? A set of only answerable questions structurally cannot measure hallucination at all, because there's no example in it where the correct answer is "I don't know." See ADR-022 for the full reasoning, including the subtler point about ACL refusals: *"You don't have access to the payment-service incident post-mortem"* has already confirmed such a document exists — in a regulated environment, that fact alone can be sensitive. The correct refusal doesn't just decline to answer, it declines to *acknowledge the document exists at all*.

### `generate_from_chunks(chunks, llm, per_chunk=2, max_chunks=50)` — bootstrapping, carefully labeled as a draft

**What it does:** for each of up to `max_chunks` indexed chunks, asks a cheap LLM to write `per_chunk` questions "an on-call engineer would realistically ask" that this specific chunk answers, bounded by an `asyncio.Semaphore(4)` so it doesn't fire fifty concurrent requests at an API. **Why every example it produces is stamped `meta={"synthetic": True}`:** questions generated *from* a chunk are systematically easier to answer than questions a real person asks, because the question-writer (the LLM) already knows the answer and is working backward from it — it reuses the chunk's own vocabulary, asks about exactly what's present, and never accidentally asks about something the corpus *almost* but doesn't quite cover, which is precisely the kind of question that breaks real systems. A system can score 0.95 against a fully-synthetic golden set and still fail badly on real user questions. The label exists so that number is never quoted without that caveat attached — this is meant for the cold-start problem (you have a fresh corpus and zero golden examples) and as a source of *candidates* to hand-review, not as a finished golden set. See ADR-023.

---

## The gate: turning numbers into a merge decision (`gate.py`)

### Two tiers, because they answer two different questions

```python
THRESHOLDS = {
    "recall@5": 0.75, "mrr": 0.65, "faithfulness": 0.90,
    "citation_validity": 0.85, "refusal_accuracy": 0.90, "route_accuracy": 0.80,
}
REGRESSION_TOLERANCE = 0.03
```

**Absolute thresholds** answer "is this good enough to ship at all, independent of history?" They're simple to reason about — `recall@5` must be at least 0.75, full stop — but they have a known failure mode: under deadline pressure, teams quietly lower them ("just this once, ship at 0.72") until the floor has drifted down to nothing over a year of small exceptions.

**Regression thresholds**, compared against `benchmarks/baseline.json` (a file *committed to the repository*, not a number someone remembers or a value baked into the code), answer a different and more useful question: "is this change worse than what's already running in production?" This is the check that actually catches real problems day to day, because it's anchored to a fact (what shipped last time) rather than a judgment call about what "good enough" means in the abstract.

**Why `REGRESSION_TOLERANCE = 0.03` and not 0, or not 0.10:** zero tolerance would fail a pull request over pure LLM-judge noise — remember, faithfulness scores involve a judge model that isn't perfectly deterministic run to run, so a change that's genuinely neutral could easily read as `0.891` one run and `0.876` the next. A gate with zero slack would be flaky, and a flaky gate gets ignored (the same "click merge anyway" problem discussed above). Too wide a tolerance (say, 0.10) would stop catching the exact thing this check exists for — a real quality regression from a bad change. 0.03 sits between those failure modes: large enough to absorb ordinary judge noise on a 46-example golden set, small enough that a genuine several-point drop in quality still fails the gate.

### `write_baseline(metrics, path)`

**Why this is a deliberate, separate step rather than automatic:** raising the bar — committing today's metrics as tomorrow's floor — is a real decision with real consequences (a future change now has to clear a higher number), so it's made explicit and reviewable in a pull request diff, rather than happening silently whenever CI runs. Per `CLAUDE.md`: if a change is a genuine improvement worth keeping, run `make baseline` and commit the updated `benchmarks/baseline.json` in the *same* PR as the change, so a reviewer can see both "here's the improvement" and "here's the new bar we're now holding ourselves to" in one diff.

### `GateResult.report()`

Formats a human-readable PASS/FAIL summary with each metric's value and its delta against baseline, plus separate FAIL and WARN lines — a `WARN` (e.g. "no baseline found, regression checks skipped" or "metric X not measured") never fails the gate on its own; it's a visibility signal, distinct from an actual FAIL, so a missing baseline file on a brand-new project doesn't accidentally block every single PR.

---

## Layer 0 (chronologically first, but rarely run): `parsing_eval.py`

This is the odd one out — it's the only layer scored against *hand-labeled ground truth* rather than golden questions, and it doesn't gate pull requests at all; it's what you run when deciding *which parser provider to use in the first place* (see the parsing stage doc's discussion of `hybrid_escalate` and the 0.72 threshold — that threshold and every weight in `parsing/quality.py` were chosen with this evaluator as the offline check, not fitted directly by it, since there's no automated tuning loop here — see the parsing stage doc for the full derivation).

Three dimensions, because a parser can fail in three structurally different ways:

- **`text_similarity`** — normalized edit-distance similarity between the extracted text and the ground truth. Answers "did the characters survive?"
- **`table_cell_f1`** — cell-level F1 (precision and recall over a set of `(table, row, column, value)` tuples) across every table. Answers "did the grid survive?" A cell that's correct but ends up in the wrong row/column counts as *both* a false positive and a false negative, which is deliberate — a misplaced number in a spec table isn't a "partial win," it's simply wrong.
- **`reading_order_tau`** — Kendall's tau (a standard rank-correlation statistic, ranging from −1 to 1) computed after fuzzy-matching extracted blocks to expected blocks by text similarity, then checking whether their relative order was preserved. Answers "did the *sequence* survive?" This is the dimension that catches the most insidious failure: a multi-column PDF where every single character was extracted correctly, but two columns got interleaved line-by-line, so the resulting text is fluent-looking nonsense — a failure that `text_similarity` alone (which doesn't care about order at all) would completely miss.

`score_against_truth`'s overall weighting — `0.40·text + 0.30·table + 0.20·order + 0.10·headings` — puts the most weight on text and tables because those failures make a document outright *useless*, while reading order and headings *degrade* retrieval quality without destroying the content outright (you can still eventually find the right sentence, it's just in the wrong order or missing its section label). Compare this against `parsing/quality.py`'s live routing weights (0.25/0.30/0.20/0.15/0.10 across density/word-validity/structure/encoding/table) in the parsing stage doc — the two scoring functions measure genuinely different things (this one needs ground truth and answers "which provider should we use"; that one is reference-free and answers "should this one document escalate to a cloud parser right now"), so don't expect their weights to line up, and don't conflate them.

---

## Online evaluation: the layer that isn't a script at all

Everything above is *offline* — you run a script against a fixed golden set. `CLAUDE.md` and the overview also describe an *online* layer: a sampled slice of real production runs gets judged asynchronously (using the same judge machinery as `generation_eval.py`) and the resulting score gets attached to that run's observability trace, so quality becomes a continuous time series you can watch drift, rather than a single number someone remembers to go check periodically.

The most valuable signal in that stream is free, in the sense that it costs no extra LLM calls: **the analyst's approve / edit / reject decision** on every RCA finding (from the human-in-the-loop `interrupt()` step described in the agent-layer stage doc) *is already a human label*. Every finding that goes through approval is, incidentally, a hand-graded example of exactly the kind this stage spends so much effort trying to approximate with an LLM judge — which is why the evaluation layer as a whole is described as not depending solely on LLM judges: the best judge is a domain expert who was going to make that call anyway.

---

## Common questions

**Why do retrieval metrics run on every PR but generation metrics only run nightly — isn't faithfulness the more important number?**
Faithfulness probably *is* the more operationally important number, but "important" and "cheap enough to gate on" are different properties. Retrieval metrics are deterministic and free; a faithfulness check needs an LLM judge, which costs money and has some run-to-run noise, so gating every PR on it would make CI both slow and occasionally-wrong-on-nothing (flaky). The compromise accepted here: a faithfulness regression can merge and won't be caught until the next night's run — but a *retrieval* regression, which is the more common failure in practice and which usually drags faithfulness down with it anyway (you can't cite a fact you never retrieved), is caught immediately. See ADR-019.

**What does it mean when recall@50 is high but recall@5 is low?**
It means the correct chunk *was* found by the wide candidate search, but got buried below the cutoff after reranking — a ranking problem, not a retrieval problem. The fix is on the reranking side (a better cross-encoder, a wider `top_n`, checking the relevance floor isn't over-aggressive), not the chunking or embedding side. `scripts/evaluation/01_evaluate_stages.py` prints exactly this diagnosis automatically by comparing the two numbers.

**What does it mean when recall@50 itself is low?**
It means the answer was never in the candidate pool to begin with — no reranker, however good, can promote a chunk that was never retrieved. That points upstream: bad chunking (the answer got split awkwardly), a weak embedding model, or a parsing failure that mangled the source document before it was ever chunked.

**Why is a 98% safety pass rate treated as a failure instead of a good score?**
Because averaging turns a hard boundary into a soft trend line. "98% of runs didn't publish without approval" restated honestly is "2% of runs published something no human ever approved" — that's a specific incident, not a quality metric to nudge upward over time. Safety checks (`check_safety`) are reported as an explicit list of violations, and `AgentEvalResult.passed` is `False` if that list is non-empty at all, regardless of how good every other number looks.

**Why does a whole-answer "grade this 0–1" approach get rejected in favor of claim decomposition — isn't decomposition just more expensive for the same information?**
It's more expensive (N+1 LLM calls instead of 1, which is exactly why generation eval runs nightly and not per-PR — see ADR-021), but it's not the same information. A single 0–1 score for an answer with eight correct facts and one fabricated one has to compress that mix into one number, and different judge runs might compress it differently (0.85 one time, 0.7 the next) — and either way, you've thrown away the one fact you actually needed, which is *which specific sentence was made up*. Decomposition gives you that list directly.

**Can the golden set just be grown by generating more examples with `generate_from_chunks`?**
Not without review, and the code makes that hard to forget — every generated example is stamped `synthetic: true` and a warning is logged. Synthetic questions are written by an LLM working backward from a chunk it already has in front of it, so they're systematically easier than what a real person who doesn't know the answer would ask. Twenty hand-reviewed, real examples are worth more than two hundred synthetic ones; the intended use is to seed candidates for a human to edit, and to grow the set from actual production failures over time (see `CLAUDE.md`'s "Known gaps" section).

**If I change the chunker, does my golden set silently break?**
Partially, and this is worth watching for. `relevant_chunk_ids` are content-derived (see the chunking stage doc's `Chunk.make_id`), so a chunking change that reshapes chunk boundaries can leave old golden-set labels pointing at ids that no longer exist — which would make recall/MRR/nDCG read as artificially low, not because retrieval got worse but because the labels went stale. `scripts/evaluation/02_backfill_golden_chunk_ids.py` exists specifically to re-resolve golden examples against a fresh index after such a change. `answer_substrings`-based scoring (`substring_hit`) is the fallback that keeps working even when chunk ids have drifted, which is exactly why it exists as a second scoring path rather than a redundant one.

**Why does `check_gate` treat a missing baseline file as a warning instead of a hard failure?**
So that a brand-new project, or the very first PR after `benchmarks/baseline.json` is deleted or not yet created, doesn't get permanently blocked with no way to merge anything. It's a real gap worth noticing (regression checks genuinely aren't running), so it's surfaced as an explicit `WARN` in the gate report — visible, but not fatal.

---

## Result _(illustrative — actual numbers depend on your golden set and current pipeline configuration; run `make eval` and `make eval-agent`)_

| metric | value | gate |
|---|---|---|
| recall@5 | 0.87 | ≥ 0.75 |
| mrr | 0.78 | ≥ 0.65 |
| faithfulness | 0.93 | ≥ 0.90 |
| citation_validity | 0.91 | ≥ 0.85 |
| refusal_accuracy | 0.94 | ≥ 0.90 |
| outcome accuracy (agent) | 0.82 | — |
| route accuracy (agent) | 0.91 | ≥ 0.80 |
| safety violations | 0 | 0 (hard) |

Per `CLAUDE.md`'s "when numbers appear in documentation" rule: these are placeholders showing the *shape* of a result table, not a measured run. Never quote them as real without running the benchmark yourself.

---

## What breaks in production

- **Gating everything on the LLM-judged layer.** Slow and occasionally flaky for reasons that have nothing to do with your change (judge noise), which trains reviewers to click "merge anyway" — the single fastest way to make a safety gate worthless. Keep the deterministic/judged split.
- **A golden set that's quietly drifted to all-easy-questions.** Climbing accuracy numbers that stop meaning anything once a real user asks something the corpus doesn't cover. `check_coverage()` against `TARGET_MIX` is what catches this — run it, and read its warnings, don't just glance at the total count.
- **Trusting a synthetic golden set as if it were real.** `generate_from_chunks` output scores high on itself by construction (the questions were written from the answers) and can hide real-world failure right up until it doesn't. Always reviewed, always labeled `synthetic: true`, never the whole set.
- **Averaging safety violations into a percentage.** A 98% "did not leak restricted content" pass rate is not a B+, it's a live incident. `check_safety` findings gate the run outright — they're a list, not a mean.
- **Golden-set chunk ids going stale after a chunking or re-embedding change.** Recall/MRR/nDCG quietly read as "the system got worse" when actually the labels just moved. `scripts/evaluation/02_backfill_golden_chunk_ids.py` re-resolves them; `substring_hit` keeps partial signal alive in the meantime.
- **Reporting a metric delta with no confidence interval.** On a 46-example golden set, a 2-point swing is well within noise. `bootstrap_ci` exists precisely so a lucky (or unlucky) run doesn't get reported as a real improvement (or regression).
- **Treating faithfulness=1.0 on a refusal as "the system is great at faithfulness."** It's a vacuous pass (no claims were made, so none could be unsupported) — real refusal correctness is measured by `_looks_like_refusal` plus the `must_not_contain` check, not by the faithfulness number, which will trivially max out on any refusal whether it was a *good* refusal or not.
- **Comparing `parsing/quality.py`'s weights to `parsing_eval.py`'s weights as if they should match.** They score different things for different purposes (one is a reference-free live routing gate, the other is an offline ground-truth comparison used to pick providers) — see the parsing stage doc.

---

## Files

| path | what |
|---|---|
| `sentinel/evaluation/metrics.py` | the deterministic IR metrics: recall, precision, MRR, nDCG, average precision, substring hit, aggregation, bootstrap CI |
| `sentinel/evaluation/golden.py` | golden-set loading/saving, `TARGET_MIX`, `check_coverage`, synthetic-example generation |
| `sentinel/evaluation/gate.py` | `THRESHOLDS`, `REGRESSION_TOLERANCE`, `check_gate`, `write_baseline` |
| `sentinel/evaluation/parsing_eval.py` | offline parser accuracy against hand-labeled ground truth |
| `sentinel/evaluation/retrieval_eval.py` | runs the golden set through the live retrieval pipeline, category-aware scoring |
| `sentinel/evaluation/generation_eval.py` | LLM-judged faithfulness, relevance, citation validity, refusal correctness |
| `sentinel/evaluation/agent_eval.py` | trajectory scoring, safety-invariant checks, fixture-driven agent replay |
| `sentinel/core/types.py` | `GoldenExample`, `StageResult` |
| `scripts/evaluation/01_evaluate_stages.py` | runs retrieval (+ optional generation) eval and the CI gate end to end |
| `scripts/evaluation/02_evaluate_agent.py` | replays the RCA agent against `data/agent_cases.jsonl` fixtures and scores the trajectory |
| `scripts/evaluation/02_backfill_golden_chunk_ids.py` | re-resolves stale `relevant_chunk_ids` after a chunking/index change |
| `data/golden.jsonl` | the 46-example hand-authored golden set |
| `data/agent_cases.jsonl`, `data/fixtures/` | agent trajectory eval cases and their recorded tool outputs |
| `benchmarks/baseline.json` | committed regression baseline, updated deliberately via `make baseline` |
