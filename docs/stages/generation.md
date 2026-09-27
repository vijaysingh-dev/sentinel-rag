# Generation

> Stage 7 of the pipeline. High-level summary in [`../STAGE-OVERVIEW.md`](../STAGE-OVERVIEW.md#7-generation); this file is the deep version.
>
> Code: `sentinel/generation/` · Scripts: `scripts/llm/` · Offline eval: `sentinel/evaluation/generation_eval.py`

---

## What we used

Four LLM adapters behind one interface (`BaseLLM` in `generation/base.py`), each exposing the same three "handles" — `chat`, `cheap`, `vision` — so the rest of the pipeline never needs to know which vendor answered.

| Provider | chat model (default) | cheap model (default) | Notes |
|---|---|---|---|
| local (`local_llm.py`) | `qwen2.5:7b-instruct` | `llama3.2:3b` | Ollama/vLLM/LM Studio via an OpenAI-compatible endpoint. Free, private, needs no account. |
| openai (`openai_llm.py`) | `gpt-4o` | `gpt-4o-mini` | Plain `api.openai.com`, a model id in `model=`. |
| anthropic (`anthropic_llm.py`) | `claude-sonnet-5` | `claude-haiku-4-5-20251001` | `api.anthropic.com` via `langchain-anthropic`. |
| google (`google_llm.py`) | `gemini-3.8-flash` | `gemini-3.1-flash-lite` | The plain Gemini API key, **not** Vertex AI. |

These are the live defaults read out of `sentinel/core/config.py` — if you've changed `.env`, `make doctor` will show what's actually active. Generation is its own vendor axis, independent of `parse_provider`, `embed_provider`, `rerank_provider` and `vectorstore_provider` (ADR-029) — nothing about which cloud parses your documents constrains which cloud answers questions about them.

---

## The big picture

Every other stage in this pipeline has been building toward one moment: a person asks a question, and something has to turn a pile of retrieved text into a sentence a human can act on. That "something" is an LLM — a large language model, a program that has read enormous amounts of text and learned to predict, one piece at a time, what words plausibly come next. Generation is the stage where that prediction machine is pointed at the specific documents Sentinel retrieved and asked to answer *from* them, not from whatever it memorized during training.

That distinction is the whole reason this stage is dangerous if you get it wrong. An LLM does not know the difference between "a fact I was trained on" and "a fact I am inventing because it sounds plausible." Ask it something confidently, with no supporting text, and it will often answer confidently anyway — fluent, specific, grammatically perfect, and simply wrong. This is called *hallucination*, and it is the single biggest reason a raw LLM is not a trustworthy operations assistant on its own. `scripts/llm/01_generate_grounded.py --no-context` exists specifically to let you watch this happen: run the identical question through the identical model with the retrieved documentation stripped out, and read what comes back. It will look just as confident as the grounded answer. That contrast is worth actually running once — it is the clearest possible argument for why the first six stages of this pipeline exist at all.

So generation's job has two halves. First, *build a prompt that makes the grounded answer the easy answer* — hand the model the retrieved chunks, tell it plainly not to invent anything the text doesn't say, and ask it to cite exactly which chunk backed each claim. Second, *treat the model as a vendor-swappable commodity*, because "which company's LLM is smartest this month" changes faster than any of the other five architectural decisions in this repo, and the code should not have to change when it does. Both halves show up directly in `BaseLLM`: the citation discipline lives in the system prompt (`scripts/llm/01_generate_grounded.py`'s `SYSTEM` string, and more fully in `rca/prompts.py` for the agent layer), and the vendor-swappability lives in the fact that every adapter below returns the exact same shape of object.

One more idea worth having before the code: **not every LLM call in this system is the same kind of call.** Deciding *which specialist should handle this question* is a different job from *writing the final paragraph a human reads*, and a model that's excellent at the second job is usually overkill — and needlessly expensive — for the first. That's the reasoning behind the `chat`/`cheap` split explained below, and it's the single biggest lever this stage has over your bill.

---

## The output contract: three handles, one shape

```python
class BaseLLM(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def chat(self) -> Any: ...       # the strong model

    @property
    @abstractmethod
    def cheap(self) -> Any: ...      # the small, fast model

    @property
    def vision(self) -> Any:
        """Defaults to the strong model, which is multimodal on all three providers."""
        return self.chat

    def structured(self, schema: type, cheap: bool = False) -> Any:
        model = self.cheap if cheap else self.chat
        return model.with_structured_output(schema)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}
```

### Why three handles instead of one model

A production RAG/agent pipeline never makes just one kind of LLM call. Over the course of answering a single question, this system might: classify which specialist agent should handle it, rewrite a follow-up question into a standalone one, generate the final grounded answer, verify that answer against the evidence, and describe an image found during ingestion. Those are wildly different jobs in terms of how much reasoning they need — and reasoning is what you pay for, both in dollars and in latency.

- **`chat`** is the strong, expensive model. It is reserved for the calls where getting it right matters most: the final answer a person reads, and — just as important — *verifying* that answer before it ships (more on why the verifier specifically must not be cheap, below).
- **`cheap`** is a small, fast, inexpensive model. It handles the high-volume, low-difficulty work: deciding which of three specialists should look at a question, turning "what about the second one?" into a standalone query, extracting a structured field from text. `docs/STAGE-OVERVIEW.md` estimates this at roughly 65% of all calls in the system — routing happens on every supervisor loop in the agent graph, so it is by far the highest-*volume* call type even though each individual call is trivial.
- **`vision`** defaults to `chat` because, as of the models in this table, the strong chat model on every one of the four providers is already multimodal (it can read an image directly) — there's no separate vision-specific model to wire up. It exists as its own named property anyway so a future provider that *does* need a distinct vision endpoint can override it without changing any calling code. It's used once, at ingest time, by `parsing/figures.py` to describe diagrams the text-only pipeline can't otherwise index.

### `structured(schema, cheap=False)` — validated output, either speed

This is the one piece of logic that lives in the base class rather than being repeated four times per adapter, because it's pure routing: pick `cheap` or `chat` based on the flag, then call LangChain's `with_structured_output(schema)` on it. `with_structured_output` binds a Pydantic schema to the model so its response is validated and parsed into that schema automatically, using the provider's *constrained decoding* where the provider supports it — meaning the model is mechanically prevented from emitting a token that would make the output invalid, rather than being asked nicely in the prompt and having its output parsed (and possibly rejected) afterward. That's a meaningfully stronger guarantee: a model that can only emit valid JSON-for-this-schema cannot forget to.

`cheap=True` matters here specifically because a lot of structured-output calls in this system are exactly the high-volume, low-difficulty kind `cheap` is for — `scripts/llm/02_structured_and_tools.py`'s `Triage` example calls `llm.structured(Triage, cheap=True)` for classifying an incoming alert, which is a routing decision, not a final answer.

### `describe()` — what's actually running

Every adapter overrides this to report its concrete model names (`{"provider": "anthropic", "chat": "claude-sonnet-5", "cheap": "claude-haiku-4-5-20251001"}`), because `provider=anthropic` alone doesn't tell you whether you're running last year's Haiku or this year's, and a cost or quality regression is often a model-name problem, not a provider problem. `scripts/llm/02_structured_and_tools.py` prints it first thing, before running anything, so a comparison run is self-documenting.

---

## Per-provider mechanics

Every one of the four adapters (`local_llm.py`, `openai_llm.py`, `anthropic_llm.py`, `google_llm.py`) has the identical shape, so understanding one is enough to understand all four — which is itself a design decision worth noticing. Here's the shared skeleton, using `AnthropicLLM` as the concrete example:

```python
class AnthropicLLM(BaseLLM):
    name = "anthropic"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._log_ready(chat=settings.anthropic_chat_model, cheap=settings.anthropic_cheap_model)

    def _build(self, model: str, **kwargs: Any) -> Any:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=0,
            timeout=self.timeout,
            max_retries=0,
            **kwargs,
        )

    @cached_property
    def chat(self) -> Any:
        return self._build(settings.anthropic_chat_model, max_tokens=1500)

    @cached_property
    def cheap(self) -> Any:
        return self._build(settings.anthropic_cheap_model, max_tokens=800)
```

### `__init__` — cheap, does no network I/O

The constructor stores a timeout and logs `llm_ready` — it does **not** connect to anything. Connecting happens lazily, the first time `.chat` or `.cheap` is actually accessed. This matters because `get_provider("llm", name)` (the registry function every caller goes through) can construct any adapter cheaply just to inspect it — `make doctor`, for instance, wants to report which providers are configured without paying for a network round trip to every one of them, including ones you don't intend to use this session.

### `_build(model, **kwargs)` — the one place the SDK client gets constructed

Every adapter has exactly one private `_build` method, and every SDK import (`from langchain_anthropic import ChatAnthropic`, `from langchain_openai import ChatOpenAI`, etc.) happens *inside* that method, not at the top of the file. This is the registry's lazy-loading contract (`sentinel/core/registry.py`'s docstring: "lets someone run the whole local pipeline without having either cloud SDK installed") applied at the generation-adapter level too — importing `openai_llm.py` as a Python module must not require `langchain-openai` to be installed, only *calling* `.chat` on an `OpenAILLM` instance does. If your `llm_provider=local` and you never touch `OpenAILLM`, you never pay the import cost or need the package.

### `@cached_property chat` / `cheap` — build once, reuse forever

`cached_property` means the underlying LangChain client object is constructed exactly once per `BaseLLM` instance, the first time it's accessed, and every subsequent access returns the same object. That object holds an HTTP connection pool under the hood; rebuilding it on every call would mean re-establishing connections constantly for no benefit. Since `get_provider` also caches the `BaseLLM` instance itself (keyed by stage/name, see `core/registry.py`), in practice a process builds each provider's `chat` and `cheap` clients at most once, total.

### Two settings applied identically everywhere, and why

Look again at `_build`: `temperature=0` and `max_retries=0` appear in all four adapters, unconditionally. Both are deliberate, both are explained in `base.py`'s module docstring, and both are the kind of thing that's easy to quietly "fix" later by someone who doesn't know why they're there — so here's why.

**`temperature=0`.** Temperature controls how much randomness the model injects when choosing its next word — high temperature makes creative, varied, sometimes surprising text; temperature 0 makes the model always pick its single most-likely next token, so the same prompt tends to produce the same (or nearly the same) answer every time. Every call this stage makes is extraction, routing, or grounded answering — never brainstorming, never creative writing. There is no case in this codebase where you want *a* plausible answer instead of *the* most likely one, so there's no case where nonzero temperature would help and several where it would hurt (a routing decision that flips randomly between runs is a bug, not a feature).

**`max_retries=0`.** Every one of these LangChain chat-model classes has its own built-in retry logic for transient failures (rate limits, timeouts). This codebase turns that off, on purpose, because it has its own retry logic — `core/resilience.py`'s `with_retry` — and running both at once is actively worse than running either alone. Two independent retriers, each backing off exponentially and unaware of the other, compound: SDK layer retries 3 times with its own backoff, and if it eventually gives up, this codebase's own `with_retry` wrapper (used elsewhere, e.g. in `rca/guard.py`'s `structured_with_repair`) retries again on top, each with jitter tuned for a *single* layer of backoff. Under load — say, a provider outage where everyone's calls start failing at once — two uncoordinated retry layers making decisions independently is exactly the kind of amplifying feedback loop that turns a brief provider hiccup into a self-inflicted traffic spike. `with_retry`'s own docstring calls this out directly: jitter exists because "N concurrent callers that fail together retry together and rebuild the exact traffic spike that caused the failure" — and that reasoning only holds if there's one retry layer making the decision, not two.

### local (`local_llm.py`) — the free, private, no-account path

```python
def _build(self, model: str, max_tokens: int) -> Any:
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=model, base_url=self.base_url, api_key="not-needed",
        temperature=0, max_tokens=max_tokens, timeout=self.timeout, max_retries=0,
    )
```

The trick worth noticing: this is `langchain_openai`'s `ChatOpenAI` class — the *OpenAI* client class — pointed at `base_url=http://localhost:11434/v1` instead of `api.openai.com`. Ollama, vLLM and LM Studio all speak the same `/v1/chat/completions` HTTP shape OpenAI popularized, so the exact same client code works against any of them; only the URL changes. `api_key="not-needed"` is there because the client library insists on a non-empty string even though a local server never checks it. This is the entire reason the whole pipeline can run with zero cloud accounts: `local_llm.py`'s module docstring calls this out directly, and it matters for development, for tests that shouldn't depend on network access or a credit card, and for corpora that legally or contractually cannot leave the building.

The one thing to know before trusting a local model in an agent context: `local_llm.py`'s docstring is blunt about it — "small local models are noticeably weaker at *tool calling* and structured output than the hosted frontier models. The agent graph will run, but expect more schema repair rounds and worse routing accuracy." `scripts/llm/02_structured_and_tools.py` exists to measure exactly this gap on your own schemas rather than assume it away — see [Common questions](#common-questions) below.

### openai (`openai_llm.py`) — the direct-API twin of the embedding adapter

Nothing exotic: a model id (`gpt-4o`, `gpt-4o-mini`) goes straight into `model=`, no deployment indirection. The module docstring points at `embedding/openai_embedder.py` as the sibling to compare against and at ADR-029/`.archive/providers/azure_llm.py` for why this is a minimal paid escape hatch rather than a maintained "cloud" path the way Azure OpenAI used to be — see [Why these defaults](#why-these-defaults) below for the short version.

### anthropic (`anthropic_llm.py`) — same shape, `langchain-anthropic`

Nothing this adapter does differs structurally from the OpenAI one; it exists to prove the `BaseLLM` abstraction actually holds across vendors with genuinely different APIs underneath, not just across two vendors that happen to share an SDK shape (as local and OpenAI do).

### google (`google_llm.py`) — Gemini API key, deliberately not Vertex AI

This is the one adapter where a design decision is easy to miss if you don't read the docstring: `ChatGoogleGenerativeAI` is initialized with `google_api_key=settings.google_api_key` — the plain Gemini API (ai.google.dev), a single API key, no GCP project. Google's *other* way to call Gemini is Vertex AI, which requires a GCP project, a service account or workload identity, and bills through GCP rather than through a simple API key. These are materially different auth and billing setups, not two names for the same thing. The plain API path is deliberately the simpler one here, consistent with ADR-029's framing that generation is "pick a model vendor," not "which cloud account do I already have" — Vertex AI would reintroduce exactly the cloud-account entanglement ADR-029 removed from this stage. If you ever need Vertex-specific features (private networking, org-level quota, GCP-native logging), that's a real, separate decision to make explicitly, not something to slip in by changing one constructor argument.

---

## Why these defaults

| constant | value | derivation |
|---|---|---|
| `temperature` | `0` | every call here is extraction, routing, or grounded answering, never creative generation — there is no scenario in this codebase where sampling variety helps, and several (routing, verification) where determinism is required for reproducible behaviour. |
| `max_retries` (SDK-level) | `0` | retry policy is centralised in `core/resilience.py`'s `with_retry`. Leaving the SDK's own retry logic on as well means two independent backoff loops that don't know about each other — under a provider outage that compounds into a self-inflicted traffic spike instead of a controlled backoff. |
| `chat` max_tokens | `1500` | generous enough for a full grounded RCA answer with citations without being large enough to let a runaway generation burn an unbounded amount of the run's token budget (`Budget.max_tokens` in `core/resilience.py` caps the *run*; this caps a single *call* within it). |
| `cheap` max_tokens | `800` | cheap-tier calls are routing decisions, rewrites, and small structured extractions — none of them plausibly need more than a few hundred tokens of output, and capping it tightly is a second, independent guard against a misbehaving prompt generating a long ramble on the high-volume path where cost matters most. |
| local timeout | `120s` | `local_llm.py`'s comment is explicit: "local inference is slower; a tighter timeout just fails." CPU/consumer-GPU inference genuinely takes longer per token than a hosted frontier model's datacenter hardware, so the timeout has to be loose enough to not misclassify normal local latency as a hang. |
| cloud timeout (openai/anthropic/google) | `60s` | hosted frontier APIs reliably respond well inside a minute for these token budgets; a shorter timeout here would risk cutting off a legitimate slow response, and 60s is short enough that `with_retry`'s bounded attempts still complete in a reasonable total wall-clock time. |
| traffic split (chat / cheap / vision) | ~30% / ~65% / ~5% | illustrative, from `docs/STAGE-OVERVIEW.md` — routing happens on every supervisor loop in the agent graph (see `docs/stages/agent.md` once written), which is why `cheap` dominates call *count* even though `chat` dominates call *cost per call*. Measure your own traffic mix before trusting these numbers for a specific deployment; the shape (cheap dominates volume, chat dominates cost-per-call) is the durable claim, the percentages are not. |

---

## The verifier is not a place to save money

This is worth its own section because it's counterintuitive and easy to get backwards under cost pressure. The natural instinct, once you've internalized "cheap handles the high-volume stuff," is to ask: verification runs on every answer, so isn't that high-volume too — shouldn't it use `cheap`?

No, and the reason is about what each call is *for*, not how often it runs. Routing happens on every supervisor loop, sure, but a wrong routing decision usually just costs an extra step — the supervisor notices the wrong specialist didn't have useful tools and re-routes. A wrong *verification* decision means a confidently-wrong answer ships to a human with the verifier's stamp of approval on it, and that human's whole reason for trusting the system is that a check happened. A weak model asked to verify a strong model's answer tends to approve exactly the answers that most need catching — the confident, fluent, plausible-sounding wrong ones — because a weak verifier is judging fluency and plausibility, which is precisely what a hallucination optimizes for. Using `cheap` for verification isn't a savings, it's spending the same money to buy a check that doesn't check anything. `docs/STAGE-OVERVIEW.md`'s framing is blunt about this: "verification runs once and is the last automated check before a human, so using the cheap model there is a false economy."

---

## Common questions

**Why does every adapter reimplement the same `_build`/`chat`/`cheap` pattern instead of one generic class parameterized by vendor?** Because the actual differences between vendors — which SDK class to import, which constructor kwarg carries the API key (`api_key` vs `google_api_key`), whether a `base_url` is meaningful — are small but real, and trying to parameterize them into one generic class would trade four short, readable files for one file full of vendor-conditional branches. Four adapters implementing the same abstract interface is the more maintainable shape here; see `core/registry.py`'s reasoning for the registry pattern generally.

**What happens if I ask for a provider whose API key isn't set?** Config validation (`Settings._check_provider_prerequisites` in `core/config.py`) rejects it at startup: `llm_provider=openai` without `openai_api_key` fails immediately with a clear message, not three calls deep into a conversation. This is the same fail-fast philosophy CLAUDE.md documents for `parse_provider=aws` without an S3 bucket.

**Why is `vision` just `chat` and not its own adapter?** Because every provider in this table already ships a multimodal chat model — there's no separate "vision model" to point at on any of the four vendors as of the models listed here. It's still a distinct named property (not a hardcoded alias) so that if a future provider needs a genuinely separate vision endpoint, only that one adapter's `vision` property needs to change; nothing calling `.vision` has to know.

**Can I mix providers — say, `local` for chat and `openai` for cheap?** Not through this interface as written; one `BaseLLM` instance supplies both `chat` and `cheap` from the same vendor. If you wanted cross-vendor mixing you'd construct two `get_provider("llm", ...)` instances and route calls to each yourself — nothing in `BaseLLM` prevents that, it's just not what the registry's single-instance-per-stage caching optimizes for.

**Where does `structured_with_repair` fit in, and why isn't it in this stage?** It lives in `sentinel/rca/guard.py`, one layer up in the agent code, not in `generation/`. It wraps any `.structured(...)`-bound model with one retry that feeds the Pydantic validation error back to the model as a follow-up message before giving up — `docs/STAGE-OVERVIEW.md`'s "what breaks in production" note ("fixes the large majority of malformed outputs for one cheap call") describes this. It's built on top of this stage's `structured()` method rather than inside it because schema-repair is an agent-graph concern (what do you do when a node's output is unusable) more than a raw-LLM-call concern.

**Why is `citation_validity` a plain string check instead of an LLM judgment?** Because the question it answers — "did the model cite an id that was never actually in the retrieved context?" — is a set-membership question, not a judgment call. `sentinel/evaluation/generation_eval.py`'s `citation_validity(answer, valid_ids)` regexes bracketed ids out of the answer and intersects them with the set of ids that were actually retrieved. No LLM is more reliable at set membership than `set.intersection`, and using one would only add latency, cost, and a new source of nondeterminism to a check that's supposed to be a hard guarantee. It's one of the cheapest, most trustworthy metrics in the whole evaluation suite for exactly this reason.

**What's the actual difference between `citation_validity` and `faithfulness_score`?** They catch different failure modes and live at different costs. `citation_validity` only checks that a cited id was real — it says nothing about whether the *claim* attached to that citation is actually supported by that chunk's text. `faithfulness_score` (same file) does the expensive version: it decomposes the answer into atomic claims with an LLM and checks each one against the context individually, which is what catches "cited a real chunk, but the claim it's attached to isn't actually in it." Run `citation_validity` on every request (it's free); reserve `faithfulness_score` for the nightly evaluation layer (it costs an LLM call per claim). Full detail in the (forthcoming) evaluation stage doc.

**Does `max_tokens` ever silently truncate an answer mid-sentence?** Yes — same failure shape as the embedding stage's silent input truncation, but on the *output* side. A response that hits `max_tokens` stops wherever it happens to be, with no error raised. `chat`'s 1500-token budget is generous for this corpus's typical RCA answer length, but if you extend the system prompt to demand much longer structured output, watch for answers that just stop.

**Why does `_build` accept `**kwargs` instead of named parameters for `max_tokens`?** Because `chat` and `cheap` are the only two callers, and they pass different `max_tokens` values — using `**kwargs` avoids a `max_tokens: int | None = None` parameter that's `None` in one call site and set in the other for no structural reason. It's a small internal method with two call sites, not a public API, so the looser signature is a reasonable trade.

---

## What breaks in production

- **Ungrounded answers.** The default failure mode of any LLM asked a question with no supporting text: fluent, plausible, specific, and wrong. `scripts/llm/01_generate_grounded.py --no-context` runs the identical prompt with retrieval skipped, which is the clearest possible demonstration of why every earlier stage in this pipeline exists.
- **Fabricated citations.** A model inventing a plausible-looking `[chunk_id]` that was never actually retrieved. `citation_validity` in `sentinel/evaluation/generation_eval.py` is a pure string/set check against the real retrieved ids — no LLM needed, cheap enough to run on every request, not just in nightly eval.
- **Structured output failures.** A model returning JSON that doesn't validate against the bound Pydantic schema — more common on smaller/local models than on hosted frontier ones. `structured_with_repair` (`sentinel/rca/guard.py`) feeds the validation error back to the model once before giving up, which the codebase's own experience says "fixes the large majority of malformed outputs for one cheap call."
- **Verifying with the cheap model to save cost.** A weak verifier tends to approve exactly the confident-wrong answers it exists to catch, because it's judging fluency, which is what a hallucination optimizes for. Verification always uses `chat`. See [The verifier is not a place to save money](#the-verifier-is-not-a-place-to-save-money) above.
- **Running both the SDK's retry logic and `with_retry` at once.** Two independent, uncoordinated backoff loops compound under load instead of degrading gracefully — this is why every adapter hardcodes `max_retries=0` and leaves retrying to `core/resilience.py`.
- **Assuming local-model tool-calling parity with hosted frontier models.** Small local models pick the wrong tool, or fail to emit valid structured output, at a meaningfully higher rate. `scripts/llm/02_structured_and_tools.py` measures this on your own schemas rather than assuming it away — run it before trusting a local model in an agent graph that routes based on tool calls.
- **Silent output truncation at `max_tokens`.** A response that hits its token cap stops mid-generation with no error. Watch for answers that end abruptly, especially after prompt changes that ask for longer structured output than the 1500/800-token budgets were sized for.
- **Treating `llm_provider=azure` as a live option.** Since ADR-029, `LLMProvider` is `Literal["local", "openai", "anthropic", "google"]` — there is no Azure or AWS generation path anymore (the old `azure_llm.py`/`aws_llm.py` are archived at `.archive/providers/`). Config validation rejects an unrecognized value at startup, but a stale `.env` written before ADR-029 will fail loudly rather than silently doing the wrong thing — which is the intended failure mode for a breaking config change, per the ADR's own "Cost" section.

---

## Files

| path | what |
|---|---|
| `sentinel/generation/base.py` | `BaseLLM` — the `chat`/`cheap`/`vision` contract, `structured()`, `describe()` |
| `sentinel/generation/local_llm.py` | Ollama/vLLM/LM Studio via an OpenAI-compatible endpoint |
| `sentinel/generation/openai_llm.py` | plain OpenAI (`api.openai.com`), no deployment indirection |
| `sentinel/generation/anthropic_llm.py` | Anthropic (`api.anthropic.com`) via `langchain-anthropic` |
| `sentinel/generation/google_llm.py` | Google Gemini via the plain API key, not Vertex AI |
| `sentinel/core/resilience.py` | `with_retry`, `CircuitBreaker`, `Budget` — the retry/degradation layer this stage deliberately defers to |
| `sentinel/core/registry.py` | `get_provider("llm", name)` — lazy construction and caching of adapters |
| `sentinel/rca/guard.py` | `structured_with_repair` — the one-shot schema-repair wrapper built on top of `.structured()` |
| `sentinel/evaluation/generation_eval.py` | `citation_validity`, `faithfulness_score` — the offline/online checks on this stage's output |
| `scripts/llm/01_generate_grounded.py` | grounded-vs-ungrounded demo, any provider, prints citation validity |
| `scripts/llm/02_structured_and_tools.py` | structured-output and tool-selection comparison across providers |
