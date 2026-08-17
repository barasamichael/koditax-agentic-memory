"""Authorized semantic retrieval over active canonical chunk embeddings."""

from __future__ import annotations

import re
from math import isfinite
from uuid import UUID
from typing import Any
from typing import cast
from typing import Literal
from typing import LiteralString
from collections.abc import Sequence

import psycopg
from psycopg import sql
from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import model_validator

from services.document_ai.app.config import get_document_ai_embedding_model
from services.document_ai.app.openai_embeddings import vector_literal
from services.document_ai.app.openai_embeddings import EMBEDDING_VERSION
from services.document_ai.app.openai_embeddings import EmbeddingProviderError
from services.document_ai.app.openai_embeddings import EmbeddingClientProtocol
from services.document_ai.app.openai_embeddings import GovernedOpenAIEmbeddingClient
from services.document_ai.app.openai_embeddings import DOCUMENT_AI_EMBEDDING_DIMENSIONS
from services.document_ai.app.persistence_support import connect_document_ai_database

_NORMALIZE_WHITESPACE_PATTERN = re.compile(r"\s+")
_MAX_RETRIEVAL_LIMIT = 100


class SemanticRetrievalRequest(BaseModel):
    """Bounded semantic discovery request with explicit retrieval controls."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    document_ids: list[UUID] = Field(default_factory=lambda: list[UUID](), max_length=100)
    document_version_id: UUID | None = None
    conversation_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)
    canonical_element_types: list[str] = Field(default_factory=list, max_length=20)
    page_number: int | None = Field(default=None, ge=1)
    limit: int = Field(default=25, ge=1, le=_MAX_RETRIEVAL_LIMIT)

    @model_validator(mode="after")
    def validate_semantic_scope(self) -> SemanticRetrievalRequest:
        if (self.conversation_id is None) != (self.turn_id is None):
            raise ValueError("conversation_id and turn_id must be supplied together")
        normalized_query = _normalize_query_text(self.query)
        if not normalized_query:
            raise ValueError("query must not be empty")
        object.__setattr__(self, "query", normalized_query)
        return self


class SemanticRetrievalCandidate(BaseModel):
    """Represent one deterministic semantic retrieval candidate chunk."""

    retrieval_chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    chunk_key: str
    content_hash_sha256: str
    chunking_policy_version: str
    semantic_distance: float
    semantic_score: float
    canonical_element_keys: tuple[str, ...]
    source_location: dict[str, object]
    structural_context: dict[str, object]
    source_filename: str
    display_name: str | None = None


class SemanticRetrievalEnvelope(BaseModel):
    """Expose semantic candidates only, never evidence or lexical fallbacks."""

    status: Literal["ok"] = "ok"
    candidates: list[SemanticRetrievalCandidate]


class SemanticRetrievalRepository:
    """Search only active eligible chunks with CockroachDB native vector search."""

    def __init__(
        self,
        *,
        database_url: str,
        embedding_client: EmbeddingClientProtocol | None = None,
    ) -> None:
        self._database_url = database_url
        self._embedding_client = embedding_client

    def retrieve(
        self,
        *,
        tenant_id: str,
        owner_user_id: UUID,
        request: SemanticRetrievalRequest,
    ) -> SemanticRetrievalEnvelope:
        query_embedding = self._embed_query(query=request.query)
        query, parameters = build_semantic_retrieval_query(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            request=request,
            query_vector=vector_literal(query_embedding),
        )
        rows = self._execute(query=query, parameters=parameters)
        return SemanticRetrievalEnvelope(candidates=[_row_to_candidate(row) for row in rows])

    def _embed_query(self, *, query: str) -> tuple[float, ...]:
        client = self._embedding_client or GovernedOpenAIEmbeddingClient.from_environment()
        vectors = client.embed(texts=(query,))
        if len(vectors) != 1:
            raise EmbeddingProviderError("openai_embedding_count_mismatch", retryable=True)
        return _validate_embedding_vector(
            vectors[0],
            expected_dimensions=DOCUMENT_AI_EMBEDDING_DIMENSIONS,
        )

    def _execute(self, *, query: str, parameters: list[object]) -> list[Sequence[Any]]:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL(cast(LiteralString, query)), parameters)
                    return cast(list[Sequence[Any]], cursor.fetchall())
        except psycopg.Error as error:
            raise RuntimeError("document_ai_semantic_retrieval_unavailable") from error


def build_semantic_retrieval_query(
    *,
    tenant_id: str,
    owner_user_id: UUID,
    request: SemanticRetrievalRequest,
    query_vector: str,
) -> tuple[str, list[object]]:
    """Build the authorized semantic retrieval query using native vector search."""

    scope_conditions = [
        "document.tenant_id = %s",
        "document.owner_user_id = %s",
        "document.state IN ('uploaded', 'processing', 'validated', 'active')",
    ]
    parameters: list[object] = [tenant_id, owner_user_id]
    if request.document_ids:
        scope_conditions.append("document.document_id = ANY(%s::uuid[])")
        parameters.append([str(item) for item in request.document_ids])
    if request.document_version_id is not None:
        scope_conditions.append("document.active_document_version_id = %s")
        parameters.append(request.document_version_id)
    if request.conversation_id is not None:
        scope_conditions.append(
            """EXISTS (
                   SELECT 1
                     FROM document_ai_document_bindings AS binding
                    WHERE binding.tenant_id = document.tenant_id
                      AND binding.document_id = document.document_id
                      AND binding.bound_by_user_id = %s
                      AND binding.conversation_id = %s
                      AND binding.turn_id = %s
                      AND binding.binding_role = 'current_turn_attachment'
                      AND binding.revoked_at IS NULL
               )"""
        )
        parameters.extend([owner_user_id, request.conversation_id, request.turn_id])

    chunk_conditions: list[str] = [
        "chunk.lifecycle_state = 'active'",
    ]
    chunk_parameters: list[object] = []
    if request.canonical_element_types:
        chunk_conditions.append(
            """EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements_text(chunk.canonical_element_keys) AS key(
                         stable_key
                     )
                     JOIN document_ai_canonical_elements AS element
                       ON element.tenant_id = chunk.tenant_id
                      AND element.canonical_representation_id =
                          chunk.canonical_representation_id
                      AND element.stable_key = key.stable_key
                    WHERE element.element_type = ANY(%s::text[])
               )"""
        )
        chunk_parameters.append(request.canonical_element_types)
    if request.page_number is not None:
        chunk_conditions.append(
            "COALESCE((chunk.structural_context->>'page_number')::int, "
            "(chunk.source_location->>'page_number')::int) = %s"
        )
        chunk_parameters.append(request.page_number)

    parameters.extend(
        [
            query_vector,
            query_vector,
            get_document_ai_embedding_model(),
            EMBEDDING_VERSION,
            DOCUMENT_AI_EMBEDDING_DIMENSIONS,
        ]
    )
    parameters.extend(chunk_parameters)
    parameters.append(request.limit)
    query = f"""
        WITH authorized_documents AS (
            SELECT document.tenant_id, document.document_id,
                   document.active_document_version_id, document.display_name,
                   document.storage_key
              FROM document_ai_documents AS document
             WHERE {" AND ".join(scope_conditions)}
        )
        SELECT document.document_id, document.active_document_version_id,
               chunk.retrieval_chunk_id, chunk.canonical_representation_id, chunk.chunk_key,
               chunk.content_hash_sha256, chunk.chunking_policy_version,
               chunk.canonical_element_keys, chunk.source_location, chunk.structural_context,
               (embedding.embedding <=> %s::vector) AS semantic_distance,
               GREATEST(
                   0.0,
                   LEAST(1.0, 1.0 - ((embedding.embedding <=> %s::vector) / 2.0))
               ) AS semantic_score,
               document.display_name, document.storage_key
          FROM authorized_documents AS document
          JOIN document_ai_retrieval_chunks AS chunk
            ON chunk.tenant_id = document.tenant_id
           AND chunk.document_id = document.document_id
           AND chunk.document_version_id = document.active_document_version_id
           AND chunk.lifecycle_state = 'active'
          JOIN document_ai_chunk_embeddings AS embedding
            ON embedding.tenant_id = chunk.tenant_id
           AND embedding.retrieval_chunk_id = chunk.retrieval_chunk_id
           AND embedding.document_version_id = chunk.document_version_id
           AND embedding.canonical_representation_id = chunk.canonical_representation_id
           AND embedding.content_hash_sha256 = chunk.content_hash_sha256
           AND embedding.chunking_policy_version = chunk.chunking_policy_version
           AND embedding.embedding_model = %s
           AND embedding.embedding_version = %s
           AND embedding.embedding_dimensions = %s
           AND embedding.index_state = 'active'
         WHERE {" AND ".join(chunk_conditions)}
         ORDER BY semantic_distance ASC, document.document_id, chunk.chunk_key,
                  chunk.retrieval_chunk_id
         LIMIT %s
    """
    return query, parameters


def _row_to_candidate(row: Sequence[Any]) -> SemanticRetrievalCandidate:
    return SemanticRetrievalCandidate(
        document_id=UUID(str(row[0])),
        document_version_id=UUID(str(row[1])),
        retrieval_chunk_id=UUID(str(row[2])),
        canonical_representation_id=UUID(str(row[3])),
        chunk_key=str(row[4]),
        content_hash_sha256=str(row[5]),
        chunking_policy_version=str(row[6]),
        canonical_element_keys=tuple(str(value) for value in cast(Sequence[object], row[7])),
        source_location=cast(dict[str, object], row[8]),
        structural_context=cast(dict[str, object], row[9]),
        semantic_distance=float(row[10]),
        semantic_score=float(row[11]),
        display_name=str(row[12]) if row[12] is not None else None,
        source_filename=str(row[13]),
    )


def _normalize_query_text(value: str) -> str:
    return _NORMALIZE_WHITESPACE_PATTERN.sub(" ", value).strip()


def _validate_embedding_vector(
    vector: Sequence[float],
    *,
    expected_dimensions: int | None = None,
) -> tuple[float, ...]:
    if not vector:
        raise EmbeddingProviderError("openai_embedding_empty_vector", retryable=False)
    normalized = tuple(float(value) for value in vector)
    if expected_dimensions is not None and len(normalized) != expected_dimensions:
        raise EmbeddingProviderError("openai_embedding_dimension_mismatch", retryable=False)
    if any(not isfinite(value) for value in normalized):
        raise EmbeddingProviderError("openai_embedding_non_finite_value", retryable=False)
    return normalized


# Compatibility aliases for the current hybrid endpoint and older tests.
HybridRetrievalRequest = SemanticRetrievalRequest
HybridRetrievalCandidate = SemanticRetrievalCandidate
HybridRetrievalEnvelope = SemanticRetrievalEnvelope
HybridRetrievalRepository = SemanticRetrievalRepository
CanonicalDiscoveryCandidate = SemanticRetrievalCandidate
