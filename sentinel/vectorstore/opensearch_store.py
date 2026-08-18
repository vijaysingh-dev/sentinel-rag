"""Amazon OpenSearch (Serverless or managed) with the k-NN vector engine.

The AWS counterpart to Azure AI Search, with one structural difference that matters: **OpenSearch
gives you the primitives, not the pipeline.** BM25 and k-NN are both first-class, but fusing them
is your job — either client-side, or via a server-side *search pipeline* with a normalization
processor. This adapter implements both paths and defaults to client-side RRF, because it behaves
identically to the other backends and is therefore comparable in a benchmark.

Authentication is the other AWS-specific wrinkle. OpenSearch Serverless requires SigV4-signed
requests against the `aoss` service name (managed domains use `es`), so the client is constructed
with `AWSV4SignerAuth` rather than a key. Getting the service name wrong produces a 403 that looks
like a permissions problem and is really a signing problem.

The ACL is enforced with `terms` filters inside a `bool` query's `filter` clause. Filter context
is the right place: it does not contribute to scoring and it is cacheable, so the security check is
effectively free per query.
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

INDEX_BODY: dict[str, Any] = {
    "settings": {
        "index": {
            "knn": True,
            # ef_search is the runtime recall/latency dial, the same knob as pgvector's
            # hnsw.ef_search and Azure's efSearch.
            "knn.algo_param.ef_search": 200,
        }
    },
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "parent_id": {"type": "keyword"},
            "doc_id": {"type": "keyword"},
            "content": {"type": "text"},
            "embed_content": {"type": "text", "analyzer": "english"},
            "heading_path": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
            "doc_type": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "allowed_roles": {"type": "keyword"},
            "components": {"type": "keyword"},
            "source_uri": {"type": "keyword"},
            "page": {"type": "integer"},
            "effective_date": {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time"},
            "strategy": {"type": "keyword"},
            "token_count": {"type": "integer"},
            "content_hash": {"type": "keyword"},
            "index_version": {"type": "keyword"},
            "embedding_model": {"type": "keyword"},
            "embedding_dims": {"type": "integer"},
        }
    },
}


class OpenSearchStore(BaseVectorStore):
    name = "aws"
    supports_native_hybrid = True  # via search pipeline, when enabled

    def __init__(
        self,
        endpoint: str | None = None,
        index_name: str | None = None,
        region: str | None = None,
        serverless: bool | None = None,
        use_search_pipeline: bool = False,
    ):
        """
        Args:
            use_search_pipeline: Fuse server-side with a normalization processor instead of
                client-side RRF. Server-side fusion is one round trip and scales better; the
                client-side default keeps behaviour identical across backends for benchmarking.
        """
        import boto3
        from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

        self.endpoint = (endpoint or settings.opensearch_endpoint).replace("https://", "")
        self.index_name = index_name or settings.opensearch_index
        self.region = region or settings.aws_region
        self.serverless = settings.opensearch_serverless if serverless is None else serverless
        self.use_search_pipeline = use_search_pipeline
        self.pipeline_name = f"{self.index_name}-hybrid"

        session = boto3.Session(profile_name=settings.aws_profile or None, region_name=self.region)
        # 'aoss' for Serverless, 'es' for managed domains. Mixing these up yields a 403.
        service = "aoss" if self.serverless else "es"
        auth = AWSV4SignerAuth(session.get_credentials(), self.region, service)

        self._client = OpenSearch(
            hosts=[{"host": self.endpoint, "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            pool_maxsize=20,
            timeout=30,
        )

    # -------------------------------------------------------------------------- schema
    def ensure_index(self, dims: int, recreate: bool = False) -> None:
        if recreate and self._client.indices.exists(self.index_name):
            self._client.indices.delete(self.index_name)

        if not self._client.indices.exists(self.index_name):
            body = {**INDEX_BODY}
            body["mappings"]["properties"]["embedding"] = {
                "type": "knn_vector",
                "dimension": dims,
                "method": {
                    "name": "hnsw",
                    "engine": "faiss",  # faiss supports filtered k-NN efficiently
                    "space_type": "cosinesimil",
                    "parameters": {"ef_construction": 400, "m": 16},
                },
            }
            self._client.indices.create(self.index_name, body=body)
            log.info("index_ready", provider=self.name, index=self.index_name, dims=dims)

        if self.use_search_pipeline:
            self._ensure_search_pipeline()

    def _ensure_search_pipeline(self) -> None:
        """Server-side hybrid fusion.

        The normalization processor rescales the two arms onto a comparable range before combining
        them — the alternative to RRF when you want weighted control over the dense/sparse balance.
        """
        pipeline = {
            "description": "hybrid normalization for sentinel knowledge",
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": "min_max"},
                        "combination": {
                            "technique": "arithmetic_mean",
                            "parameters": {"weights": [settings.hybrid_alpha, 1 - settings.hybrid_alpha]},
                        },
                    }
                }
            ],
        }
        try:
            self._client.transport.perform_request("PUT", f"/_search/pipeline/{self.pipeline_name}", body=pipeline)
        except Exception as exc:
            log.warning("search_pipeline_failed", error=str(exc)[:200])
            self.use_search_pipeline = False

    # -------------------------------------------------------------------------- writes
    def upsert(self, embedded: list[EmbeddedChunk]) -> int:
        """Bulk index, using chunk_id as the document _id so writes are idempotent."""
        from opensearchpy.helpers import bulk

        if not embedded:
            return 0

        actions = []
        for item in embedded:
            record = chunk_to_record(item, settings.index_version)
            record["embedding"] = item.vector
            action: dict[str, Any] = {
                "_index": self.index_name,
                "_source": record,
            }
            # Serverless does not permit a client-supplied _id on the bulk API; managed does, and
            # supplying it is what makes re-ingestion idempotent rather than duplicating.
            if not self.serverless:
                action["_id"] = item.chunk.chunk_id
            actions.append(action)

        success, errors = bulk(self._client, actions, refresh=True, raise_on_error=False)
        if errors:
            log.warning("upsert_partial", provider=self.name, errors=len(errors))
        return success

    def delete(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        response = self._client.delete_by_query(
            index=self.index_name,
            body={"query": {"terms": {"chunk_id": chunk_ids}}},
            refresh=True,
        )
        return response.get("deleted", 0)

    def chunk_ids_for_doc(self, doc_id: str) -> set[str]:
        response = self._client.search(
            index=self.index_name,
            body={"query": {"term": {"doc_id": doc_id}}, "_source": ["chunk_id"], "size": 1000},
        )
        return {h["_source"]["chunk_id"] for h in response["hits"]["hits"]}

    # -------------------------------------------------------------------------- filter
    @staticmethod
    def build_filter(filters: SearchFilter | None) -> list[dict]:
        """Builds `filter`-context clauses.

        Filter context rather than query context because these are boolean constraints, not
        relevance signals: they must not influence scoring, and OpenSearch can cache them.
        """
        if filters is None:
            return []
        filters.require_scope()

        clauses: list[dict] = [{"terms": {"allowed_roles": filters.roles}}]

        if filters.components:
            # Untagged chunks are platform-wide; the bool/should expresses "no tag OR overlap".
            clauses.append(
                {
                    "bool": {
                        "should": [
                            {"terms": {"components": filters.components}},
                            {"bool": {"must_not": {"exists": {"field": "components"}}}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )

        if filters.tenant_id:
            clauses.append({"term": {"tenant_id": filters.tenant_id}})
        if filters.doc_type:
            clauses.append({"term": {"doc_type": filters.doc_type}})
        if filters.index_version:
            clauses.append({"term": {"index_version": filters.index_version}})
        if filters.updated_after:
            clauses.append({"range": {"effective_date": {"gte": filters.updated_after}}})
        return clauses

    # -------------------------------------------------------------------------- reads
    def _to_results(self, hits: list[dict], retriever: str, score_key: str) -> list[RetrievedChunk]:
        out = []
        for i, hit in enumerate(hits):
            score = float(hit.get("_score", 0.0))
            chunk = record_to_chunk(hit["_source"])
            out.append(
                RetrievedChunk(
                    chunk=chunk,
                    score=score,
                    rank=i,
                    retriever=retriever,
                    **{score_key: score},
                )
            )
        return out

    def dense_search(self, vector: list[float], k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]:
        body = {
            "size": k,
            "query": {
                "bool": {
                    "must": [{"knn": {"embedding": {"vector": vector, "k": k}}}],
                    "filter": self.build_filter(filters),
                }
            },
            "_source": {"excludes": ["embedding"]},  # never ship vectors back over the wire
        }
        hits = self._client.search(index=self.index_name, body=body)["hits"]["hits"]
        return self._to_results(hits, "opensearch_knn", "dense_score")

    def sparse_search(self, query: str, k: int, filters: SearchFilter | None = None) -> list[RetrievedChunk]:
        body = {
            "size": k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["embed_content^2", "heading_path^3", "content"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": self.build_filter(filters),
                }
            },
            "_source": {"excludes": ["embedding"]},
        }
        hits = self._client.search(index=self.index_name, body=body)["hits"]["hits"]
        return self._to_results(hits, "opensearch_bm25", "sparse_score")

    def hybrid_search(
        self, query: str, vector: list[float], k: int, filters: SearchFilter | None = None
    ) -> list[RetrievedChunk]:
        if not self.use_search_pipeline:
            # Client-side RRF keeps behaviour identical to the other backends.
            results = super().hybrid_search(query, vector, k, filters)
            if results:
                RETRIEVAL_SCORE.labels(self.name).observe(results[0].score)
            return results

        # Server-side: the `hybrid` query type is what the normalization processor operates on.
        body = {
            "size": k,
            "query": {
                "hybrid": {
                    "queries": [
                        {
                            "bool": {
                                "must": [
                                    {
                                        "multi_match": {
                                            "query": query,
                                            "fields": ["embed_content^2", "heading_path^3", "content"],
                                        }
                                    }
                                ],
                                "filter": self.build_filter(filters),
                            }
                        },
                        {
                            "bool": {
                                "must": [{"knn": {"embedding": {"vector": vector, "k": k}}}],
                                "filter": self.build_filter(filters),
                            }
                        },
                    ]
                }
            },
            "_source": {"excludes": ["embedding"]},
        }
        hits = self._client.search(
            index=self.index_name,
            body=body,
            params={"search_pipeline": self.pipeline_name},
        )["hits"]["hits"]

        results = self._to_results(hits, "opensearch_hybrid", "dense_score")
        if results:
            RETRIEVAL_SCORE.labels(self.name).observe(results[0].score)
        return results

    def health(self) -> dict[str, Any]:
        try:
            count = self._client.count(index=self.index_name)["count"]
            return {"provider": self.name, "ok": True, "chunks": count, "index": self.index_name}
        except Exception as exc:
            return {"provider": self.name, "ok": False, "error": str(exc)[:200]}
