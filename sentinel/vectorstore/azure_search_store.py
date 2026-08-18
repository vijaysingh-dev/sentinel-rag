"""Azure AI Search.

The reason to choose this over a plain vector database: **hybrid retrieval and a cross-encoder
reranker are built in.** One call does BM25, does vector search, fuses them with reciprocal rank
fusion, and then re-scores the top candidates with Microsoft's semantic ranker. Assembling that
yourself means three components and a fusion implementation.

Two things to be able to explain, because "the service does it" is not an answer an interviewer
accepts:

* Passing both `search_text` and `vector_queries` is what makes the query hybrid. The service runs
  both arms and fuses by rank — the same RRF that `retrieval/fusion.py` implements by hand.
* `query_type="semantic"` adds the reranker on top. It is a cross-encoder: it reads query and
  document together rather than comparing two independently produced vectors, which is why it
  catches negation and qualifier mismatches that cosine similarity misses. It returns
  `@search.rerankerScore` on roughly a 0-4 scale, unrelated to the cosine score.

The security filter is OData and is applied **inside** the ANN traversal, so a caller without the
right role gets an empty result set rather than a filtered one — the restricted content is never
loaded into the process at all.
"""

from __future__ import annotations

from typing import Any

from sentinel.core.config import settings
from sentinel.core.observability import RETRIEVAL_SCORE, log
from sentinel.core.types import EmbeddedChunk, RetrievedChunk
from sentinel.vectorstore.base import (
    BaseVectorStore,
    SearchFilter,
    chunk_to_record,
    record_to_chunk,
)


def _quote_list(values: list[str]) -> str:
    """Escapes single quotes for an OData search.in() list."""
    return ",".join(sorted(v.replace("'", "''") for v in values))


class AzureSearchStore(BaseVectorStore):
    name = "azure"
    supports_native_hybrid = True
    supports_native_rerank = True

    def __init__(
        self,
        endpoint: str | None = None,
        index_name: str | None = None,
        api_key: str | None = None,
        semantic_config: str | None = None,
        use_managed_identity: bool = True,
    ):
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient

        self.endpoint = endpoint or settings.azure_search_endpoint
        self.index_name = index_name or settings.azure_search_index
        self.semantic_config = semantic_config or settings.azure_search_semantic_config

        key = api_key or settings.azure_search_key
        if use_managed_identity and not key:
            from azure.identity import DefaultAzureCredential

            credential: Any = DefaultAzureCredential()
        else:
            from azure.core.credentials import AzureKeyCredential

            credential = AzureKeyCredential(key)

        self._credential = credential
        self._client = SearchClient(self.endpoint, self.index_name, credential)
        self._index_client = SearchIndexClient(self.endpoint, credential)

    # -------------------------------------------------------------------------- schema
    def ensure_index(self, dims: int, recreate: bool = False) -> None:
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            HnswParameters,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SemanticConfiguration,
            SemanticField,
            SemanticPrioritizedFields,
            SemanticSearch,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )

        if recreate:
            try:
                self._index_client.delete_index(self.index_name)
            except Exception:
                pass

        fields = [
            SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True),
            SimpleField(name="parent_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="content", type=SearchFieldDataType.String),
            SearchableField(name="embed_content", type=SearchFieldDataType.String),
            SearchableField(name="heading_path", type=SearchFieldDataType.String),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=dims,
                vector_search_profile_name="hnsw-profile",
            ),
            SimpleField(name="doc_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="tenant_id", type=SearchFieldDataType.String, filterable=True),
            # Collection fields are what make the ACL filterable inside the ANN traversal.
            SearchField(
                name="allowed_roles",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                filterable=True,
                searchable=False,
            ),
            SearchField(
                name="components",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                filterable=True,
                searchable=False,
            ),
            SimpleField(name="source_uri", type=SearchFieldDataType.String),
            SimpleField(name="page", type=SearchFieldDataType.Int32),
            SimpleField(name="effective_date", type=SearchFieldDataType.String, filterable=True, sortable=True),
            SimpleField(name="strategy", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="token_count", type=SearchFieldDataType.Int32),
            SimpleField(name="content_hash", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="index_version", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="embedding_model", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="embedding_dims", type=SearchFieldDataType.Int32),
        ]

        vector_search = VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name="hnsw-config",
                    parameters=HnswParameters(m=16, ef_construction=400, ef_search=200, metric="cosine"),
                )
            ],
            profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-config")],
        )

        semantic = SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name=self.semantic_config,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="heading_path"),
                        content_fields=[SemanticField(field_name="content")],
                        keywords_fields=[SemanticField(field_name="doc_type")],
                    ),
                )
            ]
        )

        self._index_client.create_or_update_index(
            SearchIndex(
                name=self.index_name,
                fields=fields,
                vector_search=vector_search,
                semantic_search=semantic,
            )
        )
        log.info("index_ready", provider=self.name, index=self.index_name, dims=dims)

    # -------------------------------------------------------------------------- writes
    def upsert(self, embedded: list[EmbeddedChunk]) -> int:
        if not embedded:
            return 0
        documents = []
        for item in embedded:
            record = chunk_to_record(item, settings.index_version)
            record["embedding"] = item.vector
            # The service rejects nulls on non-nullable primitives; normalise before sending.
            record["page"] = record.get("page") or 0
            record["effective_date"] = record.get("effective_date") or ""
            documents.append(record)

        # merge_or_upload is the idempotent verb: insert when absent, update when present.
        results = self._client.merge_or_upload_documents(documents)
        succeeded = sum(1 for r in results if r.succeeded)
        if succeeded != len(documents):
            log.warning("upsert_partial", provider=self.name, failed=len(documents) - succeeded)
        return succeeded

    def delete(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        results = self._client.delete_documents([{"chunk_id": cid} for cid in chunk_ids])
        return sum(1 for r in results if r.succeeded)

    def chunk_ids_for_doc(self, doc_id: str) -> set[str]:
        results = self._client.search(search_text="*", filter=f"doc_id eq '{doc_id}'", select=["chunk_id"], top=1000)
        return {r["chunk_id"] for r in results}

    # -------------------------------------------------------------------------- filter
    @staticmethod
    def build_filter(filters: SearchFilter | None) -> str | None:
        if filters is None:
            return None
        filters.require_scope()

        clauses = [f"allowed_roles/any(r: search.in(r, '{_quote_list(filters.roles)}', ','))"]
        if filters.components:
            clauses.append(
                f"(not components/any() or components/any(c: search.in(c, '{_quote_list(filters.components)}', ',')))"
            )
        if filters.tenant_id:
            clauses.append(f"tenant_id eq '{filters.tenant_id}'")
        if filters.doc_type:
            clauses.append(f"doc_type eq '{filters.doc_type}'")
        if filters.index_version:
            clauses.append(f"index_version eq '{filters.index_version}'")
        if filters.updated_after:
            clauses.append(f"effective_date ge '{filters.updated_after}'")
        return " and ".join(clauses)

    # -------------------------------------------------------------------------- reads
    def _to_results(self, hits: list[dict], retriever: str) -> list[RetrievedChunk]:
        out = []
        for i, hit in enumerate(hits):
            rerank = hit.get("@search.rerankerScore")
            out.append(
                RetrievedChunk(
                    chunk=record_to_chunk(hit),
                    score=float(rerank if rerank is not None else hit.get("@search.score", 0.0)),
                    dense_score=hit.get("@search.score"),
                    rerank_score=rerank,
                    rank=i,
                    retriever=retriever,
                )
            )
        return out

    def dense_search(self, vector: list[float], k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]:
        from azure.search.documents.models import VectorizedQuery

        results = self._client.search(
            search_text=None,
            vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=k, fields="embedding")],
            filter=self.build_filter(filters),
            top=k,
        )
        return self._to_results(list(results), "azure_dense")

    def sparse_search(self, query: str, k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]:
        results = self._client.search(search_text=query, filter=self.build_filter(filters), top=k)
        return self._to_results(list(results), "azure_bm25")

    def hybrid_search(
        self, query: str, vector: list[float], k: int, filters: SearchFilter | None = None, semantic: bool = True
    ) -> list[RetrievedChunk]:
        """Hybrid plus the semantic reranker in one round trip.

        `k_nearest_neighbors` is the candidate pool for the vector arm and is deliberately wider
        than `top`: the reranker needs room to reorder. Reranking a pool of five is close to
        pointless.
        """
        from azure.search.documents.models import VectorizedQuery

        kwargs: dict[str, Any] = {
            "search_text": query,
            "vector_queries": [
                VectorizedQuery(
                    vector=vector,
                    k_nearest_neighbors=max(k, settings.retrieve_k),
                    fields="embedding",
                )
            ],
            "filter": self.build_filter(filters),
            "top": k,
        }
        if semantic:
            kwargs["query_type"] = "semantic"
            kwargs["semantic_configuration_name"] = self.semantic_config

        results = self._to_results(list(self._client.search(**kwargs)), "azure_hybrid_semantic")
        if results:
            RETRIEVAL_SCORE.labels(self.name).observe(results[0].score)
        return results

    def health(self) -> dict[str, Any]:
        try:
            count = self._client.get_document_count()
            return {"provider": self.name, "ok": True, "chunks": count, "index": self.index_name}
        except Exception as exc:
            return {"provider": self.name, "ok": False, "error": str(exc)[:200]}
