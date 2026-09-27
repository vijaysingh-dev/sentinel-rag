"""Configuration.

One settings object, but **no shared `Provider` type any more.** Each stage got its own literal —
`ParseProvider`, `EmbedProvider`, `VectorProvider`, `RerankProvider`, `LLMProvider` — because the
stages stopped sharing a provider set. That was a real design shift (2026-09), not a rename:

* **Parsing stays genuinely multi-cloud.** Azure Document Intelligence and AWS Textract each do
  something a free local library cannot (layout models, OCR on scans), so both are worth keeping
  live and comparable. `ParseProvider = Literal["local", "azure", "aws"]`, unchanged.
* **Embedding, vector storage and reranking are local-first infrastructure**, not a cloud choice.
  Running your own pgvector and a local embedding/reranking model costs nothing per query and is
  not meaningfully worse than the managed equivalents for a corpus this size — the honest
  reasoning behind dropping Azure AI Search / OpenSearch / Bedrock embedding / Bedrock rerank as
  maintained paths (moved to `.archive/providers/`, ADR-029). Embedding keeps one escape hatch
  (`openai`, flag, default `local`); reranking keeps one (`cohere`, flag, default `local`).
* **Generation is its own axis, not a cloud-account choice.** None of OpenAI, Anthropic or Google
  correspond to "the Azure account" or "the AWS account" the way Bedrock and Azure OpenAI used to
  — a model vendor is not the same kind of decision as which parser to hit, so it gets its own
  literal, `LLMProvider = Literal["local", "openai", "anthropic", "google"]`, selected
  independently of `parse_provider`.

Provider selection is still per *stage*: a realistic setup here is parse with AWS Textract (already
have the bucket), embed and rerank locally (no per-query cost, no reason to pay for something
local models already do adequately), and generate with Anthropic (whichever vendor's answers you
trust most that week). Four independent settings express that; one shared enum could not.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ParseProvider = Literal["local", "azure", "aws"]
EmbedProvider = Literal["local", "openai"]
VectorProvider = Literal["local"]
RerankProvider = Literal["local", "cohere"]
LLMProvider = Literal["local", "openai", "anthropic", "google"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_nested_delimiter="__"
    )

    # ---------------------------------------------------------------- stage provider selection
    parse_provider: ParseProvider = "local"
    embed_provider: EmbedProvider = "local"
    vectorstore_provider: VectorProvider = "local"
    rerank_provider: RerankProvider = "local"
    llm_provider: LLMProvider = "openai"

    # Hybrid parsing runs the local parser first and escalates to the cloud only when the local
    # output looks poor. On a corpus of native-text PDFs this avoids paying per page for documents
    # that a free library handled perfectly well.
    parse_strategy: Literal["single", "hybrid_escalate", "hybrid_merge", "race"] = "hybrid_escalate"
    parse_quality_threshold: float = 0.72

    # ------------------------------------------------------------------------- Azure (parsing only)
    # Azure OpenAI, Azure AI Search and Bedrock are gone as maintained paths (ADR-029); what's left
    # here is exactly what parse_provider=azure needs, nothing else.
    azure_doc_intelligence_endpoint: str = ""
    azure_doc_intelligence_key: str = ""
    azure_doc_intelligence_model: str = "prebuilt-layout"
    # In `debug` mode the Azure parser processes the document in batches of this many pages and
    # merges the results, instead of sending it whole. Two reasons: a free-tier (F0) Document
    # Intelligence resource hard-caps analysis at two pages and silently drops the rest, and
    # per-page billing means an accidental full-corpus run during development is a real bill.
    # Set to 0 to disable batching even in debug mode. Ignored entirely when `debug` is false.
    azure_di_dev_page_limit: int = 2

    # ------------------------------------------------------------------------------ AWS (parsing only)
    aws_region: str = "us-east-1"
    aws_profile: str = ""

    textract_s3_bucket: str = ""  # required for async multi-page analysis
    textract_s3_prefix: str = "sentinel/textract/"
    textract_features: list[str] = Field(default_factory=lambda: ["TABLES", "LAYOUT"])

    # ---------------------------------------------------------------------------------- local
    local_embedding_model: str = "BAAI/bge-base-en-v1.5"
    local_embedding_dims: int = 768
    local_rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    local_llm_base_url: str = "http://localhost:11434/v1"
    local_llm_model: str = "qwen2.5:7b-instruct"
    local_llm_cheap_model: str = "llama3.2:3b"

    pgvector_dsn: str = "postgresql+psycopg://sentinelapp:sentinel@localhost:5432/sentinel"
    pgvector_collection: str = "ops_knowledge"

    # -------------------------------------------------------------------------- OpenAI (embed/llm)
    # Plain api.openai.com — not Azure OpenAI, which no longer exists in this codebase (ADR-029).
    # One key, used by whichever of embed_provider/llm_provider is set to "openai"; either, both,
    # or neither can select it independently.
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o"
    openai_cheap_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dims: int = 1536

    # ------------------------------------------------------------------------------- Anthropic (llm)
    anthropic_api_key: str = ""
    anthropic_chat_model: str = "claude-sonnet-5"
    anthropic_cheap_model: str = "claude-haiku-4-5-20251001"

    # ---------------------------------------------------------------------------------- Google (llm)
    google_api_key: str = ""
    # Google retired Pro models from the free tier (2026-05); gemini-2.5-pro now 404s as
    # GoogleModelNotFoundError on a free-tier key. gemini-3.8-flash (current GA flagship Flash
    # model) and gemini-3.1-flash-lite (the genuinely lightweight tier) are both free-tier
    # eligible and keep the chat/cheap capability split meaningful without needing a paid Pro tier.
    google_chat_model: str = "gemini-3.8-flash"
    google_cheap_model: str = "gemini-3.1-flash-lite"

    # ------------------------------------------------------------------------------- Cohere (rerank)
    cohere_api_key: str = ""
    cohere_rerank_model: str = "rerank-v3.5"

    # ----------------------------------------------------------------------------- retrieval
    retrieve_k: int = 50  # candidate pool; wide for recall
    rerank_top_n: int = 6  # what actually reaches the prompt
    hybrid_alpha: float = 0.5  # dense weight when using weighted fusion
    rrf_k: int = 60  # RRF damping constant
    min_rerank_score: float = 0.0  # relevance floor; calibrate per reranker
    mmr_lambda: float = 0.6
    use_mmr: bool = False

    # ------------------------------------------------------------------------------ chunking
    chunk_strategy: str = "structural"
    chunk_size: int = 550
    chunk_overlap: int = 60
    contextual_chunking: bool = False  # LLM-written situating sentence; costs one call/chunk

    # --------------------------------------------------------------------------------- cache
    redis_url: str = "redis://localhost:6379/0"
    # Belt-and-suspenders alongside the logical-DB split (Sentinel on DB 0, otel-demo's
    # valkey-cart on DB 1, see CLAUDE.md): every key this module writes carries this prefix too,
    # so a misconfigured DB index produces a visibly foreign key instead of a silent collision.
    redis_key_prefix: str = "sentinel"
    cache_enabled: bool = True
    embedding_cache_ttl: int | None = None  # None = never expire; keyed by content hash
    retrieval_cache_ttl: int = 900
    answer_cache_ttl: int = 3600
    answer_cache_threshold: float = 0.95

    # ------------------------------------------------------------------------------- budgets
    node_timeout_s: int = 90
    max_steps: int = 12
    max_usd_per_run: float = 2.50
    breaker_threshold: int = 5
    breaker_cooldown_s: int = 60
    sql_row_limit: int = 500  # retained: still enforced if a SQL warehouse tool is wired back in
    promql_max_range_hours: int = 6  # a range query wider than this is rejected before it runs
    prometheus_url: str = "http://localhost:9090"
    # otel-demo's Jaeger runs with base_path /jaeger/ui, which prefixes its query API too — the
    # bare host:port answers every /api/ call with the UI's HTML, not JSON.
    jaeger_url: str = "http://localhost:16686/jaeger/ui"
    flagd_config_path: str = ""  # path to demo.flagd.json; empty -> flag tools return "unconfigured"
    # The tracked, shipped copy of the same file: its defaultVariants are each flag's resting state.
    # The live file above cannot say that on its own — it only knows where a flag is *now*.
    flagd_baseline_path: str = "app/otel-demo/src/flagd/demo.flagd.json"
    incident_log_url: str = "http://localhost:8092"  # app/incident-generator
    system_monitor_url: str = "http://localhost:8091"  # app/system-monitor

    # --------------------------------------------------------------------------- chat mediator
    # sentinel/chat — the router/session layer between web/ and {tool calls, retrieval, the RCA
    # graph}. Ports 8091/8092 are system-monitor/incident-generator (above); 8093 is this service.
    chat_api_port: int = 8093
    chat_default_tenant_id: str = "default"  # no multi-tenant chat concept yet; one fixed tenant
    chat_raw_window: int = 8  # raw turns kept verbatim in context; see chat/session.py:build_context
    chat_resummarize_threshold: int = 20  # messages past the last summary point that trigger one
    chat_router_confidence_floor: float = 0.55  # LLM-fallback classifications below this -> refuse

    # ------------------------------------------------------------------------- observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"
    otel_endpoint: str = ""
    log_level: str = "INFO"
    # Development switch. When true: logging is forced to DEBUG regardless of `log_level`, and
    # cost/quota-sensitive cloud calls take a cheaper development path (see `azure_di_dev_page_limit`).
    # Leave false in any environment where real cost or real corpus size matters.
    debug: bool = False
    online_eval_sample_rate: float = 0.1

    @property
    def effective_log_level(self) -> str:
        """`log_level`, unless `debug` forces DEBUG. One place so every entry point agrees."""
        return "DEBUG" if self.debug else self.log_level

    @property
    def pg_schema(self) -> str:
        """Postgres schema for Sentinel-owned tables (chat sessions/messages, LangGraph checkpoints).

        `debug=True` (e.g. `make chat-server` on a laptop) resolves to a `test` schema instead of
        `public`, same database and role. This is what keeps manual testing out of the real chat
        history: without it, every session created while poking at the chat UI locally lands in the
        exact tables the production UI's sidebar queries, because both share `pgvector_dsn` and
        `chat_default_tenant_id`. A second schema is enough isolation here — a second database would
        also work but means a second DSN, a second set of grants, and a second thing to keep in sync
        with `config/init-sentinel-db.sql`, for no benefit `search_path` doesn't already give us.
        """
        return "test" if self.debug else "public"

    @property
    def redis_key_prefix_effective(self) -> str:
        """`redis_key_prefix`, namespaced under `test` when `settings.debug` is set.

        Same reasoning as `pg_schema`: Redis is shared, global infra (see CLAUDE.md), and without
        this a locally-tested chat session's cache entries are indistinguishable from a real one's
        under the existing `sentinel:` prefix. Namespacing the prefix, not switching DB index, keeps
        this consistent with the DB-per-app / prefix-per-key belt-and-suspenders split already in
        place for otel-demo's Valkey-cart traffic.
        """
        return f"{self.redis_key_prefix}:test" if self.debug else self.redis_key_prefix

    kill_switch: bool = False
    fixtures_path: str = ""  # set by evals to replay recorded tool output

    @property
    def embedding_dims(self) -> int:
        """Dimensionality of whichever embedding provider is active.

        Exposed as one property because the vector store schema must match the embedder exactly;
        a mismatch surfaces as a dimension error at write time if you are lucky and as silently
        garbage retrieval if you are not.
        """
        return {
            "local": self.local_embedding_dims,
            "openai": self.openai_embedding_dims,
        }[self.embed_provider]

    @property
    def embedding_model(self) -> str:
        return {
            "local": self.local_embedding_model,
            "openai": self.openai_embedding_model,
        }[self.embed_provider]

    @property
    def index_version(self) -> str:
        """Fingerprint of everything that invalidates an index.

        Stamped on every chunk and asserted at query time. Querying an index built with a
        different embedding model is the single most common production RAG incident, and it
        presents as a mysterious quality regression rather than as an error.
        """
        import hashlib

        seed = "|".join(
            [
                self.chunk_strategy,
                str(self.chunk_size),
                str(self.chunk_overlap),
                str(self.contextual_chunking),
                self.embed_provider,
                self.embedding_model,
                str(self.embedding_dims),
            ]
        )
        return "v" + hashlib.sha256(seed.encode()).hexdigest()[:10]

    @model_validator(mode="after")
    def _check_provider_prerequisites(self) -> Settings:
        """Fails fast on a provider selected without its credentials.

        Discovering a missing key after a twenty-minute ingestion run is a bad trade for the
        three lines it takes to check up front.
        """
        problems: list[str] = []
        if self.parse_provider == "azure" and not self.azure_doc_intelligence_endpoint:
            problems.append("parse_provider=azure requires azure_doc_intelligence_endpoint")
        # Textract's multi-page path reads from S3 — there is no inline-bytes option. A
        # single-page doc still works bucket-less, but a knowledge corpus is almost never
        # single-page, so treat the missing bucket as a configuration error rather than a
        # surprise ParseFailure partway through ingestion.
        if self.parse_provider == "aws" and not self.textract_s3_bucket:
            problems.append("parse_provider=aws requires textract_s3_bucket (async Textract reads from S3)")
        if self.embed_provider == "openai" and not self.openai_api_key:
            problems.append("embed_provider=openai requires openai_api_key")
        if self.rerank_provider == "cohere" and not self.cohere_api_key:
            problems.append("rerank_provider=cohere requires cohere_api_key")
        if self.llm_provider == "openai" and not self.openai_api_key:
            problems.append("llm_provider=openai requires openai_api_key")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            problems.append("llm_provider=anthropic requires anthropic_api_key")
        if self.llm_provider == "google" and not self.google_api_key:
            problems.append("llm_provider=google requires google_api_key")
        if problems:
            raise ValueError("configuration errors:\n  - " + "\n  - ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
