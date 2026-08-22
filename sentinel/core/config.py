"""Configuration.

One settings object covers all three providers. The alternative — a settings class per cloud —
looks tidier but forces provider knowledge into every call site, which is exactly what the adapter
pattern exists to prevent.

Provider selection is per *stage*, not global. That is deliberate: a realistic deployment might
parse with Azure Document Intelligence (best layout model), embed with Bedrock Titan (already in
the AWS account), and rerank locally (no per-query cost). Forcing one cloud for the whole pipeline
would make that impossible to express, and it is a genuinely common shape in regulated
environments where different data classes have different residency rules.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["local", "azure", "aws"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_nested_delimiter="__"
    )

    # ---------------------------------------------------------------- stage provider selection
    parse_provider: Provider = "local"
    embed_provider: Provider = "local"
    vectorstore_provider: Provider = "local"
    rerank_provider: Provider = "local"
    llm_provider: Provider = "local"

    # Hybrid parsing runs the local parser first and escalates to the cloud only when the local
    # output looks poor. On a corpus of native-text PDFs this avoids paying per page for documents
    # that a free library handled perfectly well.
    parse_strategy: Literal["single", "hybrid_escalate", "hybrid_merge", "race"] = "hybrid_escalate"
    parse_quality_threshold: float = 0.72

    # ---------------------------------------------------------------------------------- Azure
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_chat_deployment: str = "gpt-4o"
    azure_chat_cheap_deployment: str = "gpt-4o-mini"
    azure_embedding_deployment: str = "text-embedding-3-large"
    azure_embedding_dims: int = 3072

    azure_doc_intelligence_endpoint: str = ""
    azure_doc_intelligence_key: str = ""
    azure_doc_intelligence_model: str = "prebuilt-layout"

    azure_search_endpoint: str = ""
    azure_search_key: str = ""
    azure_search_index: str = "sentinel-knowledge"
    azure_search_semantic_config: str = "semantic-config"

    # ------------------------------------------------------------------------------------ AWS
    aws_region: str = "us-east-1"
    aws_profile: str = ""

    bedrock_chat_model: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    bedrock_chat_cheap_model: str = "anthropic.claude-3-5-haiku-20241022-v1:0"
    bedrock_embedding_model: str = "amazon.titan-embed-text-v2:0"
    bedrock_embedding_dims: int = 1024
    bedrock_rerank_model: str = "amazon.rerank-v1:0"

    textract_s3_bucket: str = ""  # required for async multi-page analysis
    textract_s3_prefix: str = "sentinel/textract/"
    textract_features: list[str] = Field(default_factory=lambda: ["TABLES", "LAYOUT"])

    opensearch_endpoint: str = ""
    opensearch_index: str = "sentinel-knowledge"
    opensearch_serverless: bool = True

    # ---------------------------------------------------------------------------------- local
    local_embedding_model: str = "BAAI/bge-base-en-v1.5"
    local_embedding_dims: int = 768
    local_rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    local_llm_base_url: str = "http://localhost:11434/v1"
    local_llm_model: str = "qwen2.5:7b-instruct"
    local_llm_cheap_model: str = "llama3.2:3b"

    pgvector_dsn: str = "postgresql+psycopg://sentinel:sentinel@localhost:5432/sentinel"
    pgvector_collection: str = "ops_knowledge"

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
    sql_row_limit: int = 500

    # ------------------------------------------------------------------------- observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"
    otel_endpoint: str = ""
    log_level: str = "INFO"
    online_eval_sample_rate: float = 0.1

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
            "azure": self.azure_embedding_dims,
            "aws": self.bedrock_embedding_dims,
        }[self.embed_provider]

    @property
    def embedding_model(self) -> str:
        return {
            "local": self.local_embedding_model,
            "azure": self.azure_embedding_deployment,
            "aws": self.bedrock_embedding_model,
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

        Discovering a missing endpoint after a twenty-minute ingestion run is a bad trade for the
        three lines it takes to check up front.
        """
        problems: list[str] = []
        if self.parse_provider == "azure" and not self.azure_doc_intelligence_endpoint:
            problems.append("parse_provider=azure requires azure_doc_intelligence_endpoint")
        if self.embed_provider == "azure" and not self.azure_openai_endpoint:
            problems.append("embed_provider=azure requires azure_openai_endpoint")
        if self.vectorstore_provider == "azure" and not self.azure_search_endpoint:
            problems.append("vectorstore_provider=azure requires azure_search_endpoint")
        if self.vectorstore_provider == "aws" and not self.opensearch_endpoint:
            problems.append("vectorstore_provider=aws requires opensearch_endpoint")
        if problems:
            raise ValueError("configuration errors:\n  - " + "\n  - ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
