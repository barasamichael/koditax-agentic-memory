"""Milestone 17 canonical candidate validation and readiness coverage."""

from __future__ import annotations

from uuid import UUID
from pathlib import Path

from services.document_ai.app.canonical_assembly import CanonicalGraph
from services.document_ai.app.canonical_assembly import assemble_canonical_graph
from services.document_ai.app.canonical_validation import CanonicalCandidate
from services.document_ai.app.canonical_validation import validate_canonical_candidate

_PROVIDER_ID = UUID("00000000-0000-0000-0000-000000000011")
_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000012")


def _graph(*, pages: list[int] | None = None) -> CanonicalGraph:
    page_numbers = [1, 2] if pages is None else pages
    return assemble_canonical_graph(
        provider_result_id=_PROVIDER_ID,
        source_artifact_id=_ARTIFACT_ID,
        validated_result={
            "result": {
                "schema_version": "v1",
                "warnings": [],
                "pages": [
                    {
                        "page_number": page,
                        "observations": [
                            {
                                "observation_id": f"heading-{page}",
                                "kind": "heading",
                                "order": 0,
                                "text": f"Page {page}",
                                "state": "observed",
                                "source_location": {"page_number": page},
                            }
                        ],
                    }
                    for page in page_numbers
                ],
            }
        },
        source_lineage={
            "provider_result_id": str(_PROVIDER_ID),
            "source_artifact_id": str(_ARTIFACT_ID),
            "document_version_id": "00000000-0000-0000-0000-000000000014",
            "processing_operation_id": "00000000-0000-0000-0000-000000000015",
        },
    )


def _candidate(
    *, graph: CanonicalGraph | None = None, source_artifact_id: UUID = _ARTIFACT_ID
) -> CanonicalCandidate:
    return CanonicalCandidate(
        canonical_representation_id=UUID("00000000-0000-0000-0000-000000000013"),
        provider_result_id=_PROVIDER_ID,
        source_artifact_id=source_artifact_id,
        document_version_id=UUID("00000000-0000-0000-0000-000000000014"),
        canonical_schema_version="v1",
        assembly_policy_version="v1",
        graph=_graph() if graph is None else graph,
    )


def test_complete_candidate_is_validated_and_fully_ready() -> None:
    result = validate_canonical_candidate(
        candidate=_candidate(), expected_source_artifact_id=_ARTIFACT_ID, expected_pages=(1, 2)
    )
    assert result.state == "validated"
    assert result.readiness == "full"
    assert result.missing_pages == ()


def test_missing_page_is_rejected_for_incomplete_coverage() -> None:
    result = validate_canonical_candidate(
        candidate=_candidate(graph=_graph(pages=[1])),
        expected_source_artifact_id=_ARTIFACT_ID,
        expected_pages=(1, 2),
    )
    assert result.state == "rejected"
    assert result.readiness == "none"
    assert result.missing_pages == (2,)
    assert "canonical_structural_coverage_incomplete" in result.reasons


def test_wrong_source_and_missing_provenance_are_rejected() -> None:
    wrong_source = validate_canonical_candidate(
        candidate=_candidate(source_artifact_id=UUID("00000000-0000-0000-0000-000000000099")),
        expected_source_artifact_id=_ARTIFACT_ID,
        expected_pages=(1, 2),
    )
    assert wrong_source.state == "rejected"
    assert "source_artifact_mismatch" in wrong_source.reasons

    graph = _graph()
    element = graph.elements[0]
    object.__setattr__(element, "source_region", {})
    missing_provenance = validate_canonical_candidate(
        candidate=_candidate(graph=graph),
        expected_source_artifact_id=_ARTIFACT_ID,
        expected_pages=(1, 2),
    )
    assert missing_provenance.state == "rejected"
    assert "missing_source_provenance" in missing_provenance.reasons


def test_tampered_candidate_hash_is_rejected() -> None:
    graph = _graph()
    element = graph.elements[0]
    object.__setattr__(
        element,
        "source_region",
        {"page_number": 1, "start_offset": 10, "end_offset": 20},
    )
    result = validate_canonical_candidate(
        candidate=_candidate(graph=graph),
        expected_source_artifact_id=_ARTIFACT_ID,
        expected_pages=(1, 2),
    )
    assert result.state == "rejected"
    assert "canonical_content_hash_mismatch" in result.reasons


def test_validation_migration_keeps_candidate_activation_and_readiness_durable() -> None:
    sql = Path(
        "database/migrations/0043_document_ai_canonical_validation_activation.sql"
    ).read_text()
    for marker in (
        "canonical_validation_version",
        "validation_report",
        "readiness_state",
        "uq_document_ai_active_canonical_representation",
    ):
        assert marker in sql
    source = Path("services/document_ai/app/canonical_activation.py").read_text()
    for marker in (
        "document_ai_retrieval_chunks",
        "document_ai_chunk_embeddings",
        "embedding.content_hash_sha256 = chunk.content_hash_sha256",
        "embedding.embedding_dimensions = %s",
        "embedding.embedding_model = %s",
        "embedding.embedding_version = %s",
        "FOR UPDATE",
    ):
        assert marker in source
