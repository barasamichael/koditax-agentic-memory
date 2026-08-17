"""Milestone 44 semantic retrieval over active retrieval chunks."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from services.document_ai.app.openai_embeddings import EmbeddingProviderError
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRequest
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRepository
from services.document_ai.app.semantic_retrieval import build_semantic_retrieval_query


class _SemanticRepository(SemanticRetrievalRepository):
    def __init__(self) -> None:
        super().__init__(database_url="postgresql://not-used")
        self.executed_query = ""
        self.executed_parameters: list[object] = []

    def _execute(self, *, query: str, parameters: list[object]) -> list[Sequence[Any]]:
        self.executed_query = query
        self.executed_parameters = list(parameters)
        return []


class _UnavailableEmbeddingClient:
    def embed(self, *, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        raise EmbeddingProviderError("openai_embedding_timeout", retryable=True)


def _query(request: SemanticRetrievalRequest) -> tuple[str, list[object]]:
    return build_semantic_retrieval_query(
        tenant_id="tenant-a",
        owner_user_id=uuid4(),
        request=request,
        query_vector="[0.1,0.2]",
    )


def test_authorization_and_active_version_scope_precede_chunk_search() -> None:
    query, parameters = _query(SemanticRetrievalRequest(query="  employment    income  "))
    assert "WITH authorized_documents AS" in query
    assert "document.tenant_id = %s" in query
    assert "document.owner_user_id = %s" in query
    assert "document.active_document_version_id = %s" not in query
    assert "document.state IN ('uploaded', 'processing', 'validated', 'active')" in query
    assert "chunk.lifecycle_state = 'active'" in query
    assert "embedding.index_state = 'active'" in query
    assert "websearch_to_tsquery" not in query
    assert parameters[0] == "tenant-a"
    assert isinstance(parameters[1], UUID)


def test_semantic_query_uses_native_vector_search_and_deterministic_ties() -> None:
    query, _ = _query(SemanticRetrievalRequest(query="salary"))
    assert "document_ai_chunk_embeddings" in query
    assert "embedding.embedding <=> %s::vector" in query
    assert "semantic_distance" in query
    assert "semantic_score" in query
    assert "ORDER BY semantic_distance ASC, document.document_id, chunk.chunk_key," in query
    assert "chunk.retrieval_chunk_id" in query


def test_chunk_filters_apply_to_the_semantic_scope() -> None:
    query, parameters = _query(
        SemanticRetrievalRequest(
            query="earnings",
            document_ids=[uuid4()],
            canonical_element_types=["amount"],
            page_number=2,
        )
    )
    assert "document.document_id = ANY(%s::uuid[])" in query
    assert "jsonb_array_elements_text(chunk.canonical_element_keys)" in query
    assert "element.element_type = ANY(%s::text[])" in query
    assert "COALESCE((chunk.structural_context->>'page_number')::int" in query
    assert any(isinstance(value, list) and value == ["amount"] for value in parameters)
    assert 2 in parameters


def test_current_turn_scope_requires_a_durable_binding() -> None:
    query, parameters = _query(
        SemanticRetrievalRequest(query="attached payslip", conversation_id="c1", turn_id="t4")
    )
    assert "binding.binding_role = 'current_turn_attachment'" in query
    assert "binding.revoked_at IS NULL" in query
    assert {"c1", "t4"}.issubset(set(parameters))


def test_retrieval_request_rejects_empty_and_partial_turn_scope() -> None:
    with pytest.raises(ValidationError, match="query must not be empty"):
        SemanticRetrievalRequest(query="   ")
    with pytest.raises(ValidationError, match="supplied together"):
        SemanticRetrievalRequest(query="income", conversation_id="conversation-1")


def test_repository_returns_semantic_candidates_without_lexical_fallback() -> None:
    repository = _SemanticRepository()
    result = repository.retrieve(
        tenant_id="tenant-a",
        owner_user_id=uuid4(),
        request=SemanticRetrievalRequest(query="earnings"),
    )
    assert result.status == "ok"
    assert result.candidates == []
    assert "semantic_distance" in repository.executed_query
    assert "websearch_to_tsquery" not in repository.executed_query


def test_embedding_provider_errors_surface_without_fallback() -> None:
    repository = SemanticRetrievalRepository(
        database_url="postgresql://not-used",
        embedding_client=_UnavailableEmbeddingClient(),
    )
    with pytest.raises(EmbeddingProviderError, match="openai_embedding_timeout"):
        repository.retrieve(
            tenant_id="tenant-a",
            owner_user_id=uuid4(),
            request=SemanticRetrievalRequest(query="earnings"),
        )
