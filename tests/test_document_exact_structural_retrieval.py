"""Milestone 45 exact retrieval over active canonical chunks."""

from __future__ import annotations

from uuid import uuid4
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.document_ai.app.exact_retrieval import ExactRetrievalRequest
from services.document_ai.app.exact_retrieval import build_exact_retrieval_query


def _query(request: ExactRetrievalRequest) -> tuple[str, list[object]]:
    return build_exact_retrieval_query(
        tenant_id="tenant-a", owner_user_id=uuid4(), request=request
    )


def test_document_id_retrieval_scopes_to_authorized_active_chunks() -> None:
    document_id = uuid4()
    query, parameters = _query(ExactRetrievalRequest(document_ids=[document_id]))
    assert "WITH authorized_documents AS" in query
    assert "document.owner_user_id = %s" in query
    assert "document.state IN ('uploaded', 'processing', 'validated', 'active')" in query
    assert "representation.is_active" in query
    assert "chunk.lifecycle_state = 'active'" in query
    assert "document_ai_chunk_embeddings" not in query
    assert parameters[:2][0] == "tenant-a"
    assert str(document_id) in str(parameters[2])


def test_trashed_and_cross_tenant_documents_cannot_enter_candidate_scope() -> None:
    query, parameters = _query(ExactRetrievalRequest(document_ids=[uuid4()]))
    assert "document.tenant_id = %s" in query
    assert "document.owner_user_id = %s" in query
    assert (
        "'trashed'"
        not in query.split("WITH authorized_documents", maxsplit=1)[1].split("),", maxsplit=1)[0]
    )
    assert parameters[:2][0] == "tenant-a"


def test_current_turn_scope_requires_a_durable_current_turn_binding() -> None:
    query, parameters = _query(
        ExactRetrievalRequest(conversation_id="conversation-1", turn_id="turn-4")
    )
    assert "binding.binding_role = 'current_turn_attachment'" in query
    assert "binding.revoked_at IS NULL" in query
    assert "conversation-1" in parameters
    assert "turn-4" in parameters


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("filename", "p9-2025.pdf", "document.storage_key ILIKE %s"),
        ("display_name", "July payslip", "document.display_name ILIKE %s"),
        ("identifier", "A123", "chunk.embedding_text ILIKE %s"),
        ("amount", "1200.00", "chunk.embedding_text ILIKE %s"),
        ("date", "2025-07-31", "chunk.embedding_text ILIKE %s"),
        ("full_text", "gross pay", "chunk.embedding_text ILIKE %s"),
    ],
)
def test_metadata_and_exact_value_constraints_are_parameterized(
    field: str, value: str, expected: str
) -> None:
    query, parameters = _query(ExactRetrievalRequest.model_validate({field: value}))
    assert expected in query
    assert any(" ".join(value.split()) in str(item) for item in parameters)


def test_page_table_sheet_and_cell_filters_use_chunk_source_coordinates() -> None:
    query, parameters = _query(
        ExactRetrievalRequest(
            page_number=2,
            sheet_name="Payroll",
            table_name="Earnings",
            cell_reference="B7",
        )
    )
    assert query.count("chunk.source_location @> %s::jsonb") >= 4
    assert any('"page_number": 2' in str(item) for item in parameters)
    assert any('"sheet_name": "Payroll"' in str(item) for item in parameters)
    assert any('"table_name": "Earnings"' in str(item) for item in parameters)
    assert any('"cell_reference": "B7"' in str(item) for item in parameters)


def test_exact_retrieval_ranking_is_deterministic_and_avoids_vector_search() -> None:
    query, _ = _query(ExactRetrievalRequest(full_text="gross pay"))
    assert "exact_match_rank" in query
    assert "ORDER BY exact_match_rank ASC, document.document_id, chunk.chunk_key," in query
    assert "document_ai_chunk_embeddings" not in query
    assert "websearch_to_tsquery" not in query


def test_retrieval_request_rejects_ambiguous_empty_and_partial_turn_scope() -> None:
    with pytest.raises(ValidationError, match="at least one exact retrieval constraint"):
        ExactRetrievalRequest()
    with pytest.raises(ValidationError, match="supplied together"):
        ExactRetrievalRequest(conversation_id="conversation-1")


def test_migration_and_contract_define_lexical_structural_retrieval_without_pgvector() -> None:
    migration_path = Path(
        "services/document_ai/migrations/cockroachdb/0014_document_ai_exact_retrieval_indexes.sql"
    )
    migration = migration_path.read_text()
    contract = Path("contracts/openapi/document_ai.yaml").read_text()
    for marker in (
        "idx_document_ai_retrieval_chunks_exact_lexical",
        "idx_document_ai_retrieval_chunks_exact_source_location",
        "idx_document_ai_retrieval_chunks_exact_structural_context",
    ):
        assert marker in migration
    assert "/v1/document-evidence/exact-retrievals" in contract
    assert "ExactRetrievalRequest" in contract
    assert "ExactRetrievalCandidate" in contract
    assert "pgvector" not in migration.lower()
