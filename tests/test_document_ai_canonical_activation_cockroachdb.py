"""Live CockroachDB coverage for safe canonical activation."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.config import get_document_ai_embedding_model
from services.document_ai.app.openai_embeddings import EMBEDDING_VERSION
from services.document_ai.app.openai_embeddings import CanonicalEmbeddingRepository
from services.document_ai.app.openai_embeddings import DOCUMENT_AI_EMBEDDING_DIMENSIONS
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.canonical_activation import CanonicalActivationRepository
from services.document_ai.app.canonical_validation import CanonicalValidationRepository
from services.document_ai.app.canonical_chunk_generation import CanonicalChunkGenerationRepository
from tests.test_document_ai_canonical_validation_cockroachdb import _build_real_candidate
from tests.test_document_ai_canonical_validation_cockroachdb import (
    _skip_if_live_dependencies_unavailable,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@pytest.fixture(scope="session")
def cockroach_document_ai_database() -> str:
    database_url = load_document_ai_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for Document AI CockroachDB tests.")

    with psycopg.connect(database_url) as connection:
        try:
            runner._validate_target_database(connection)  # type: ignore[arg-type]
        except runner.DocumentAITargetError:
            pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")

    assert runner.main() == 0
    return database_url


def test_activation_replaces_the_prior_active_candidate_only_after_every_prerequisite_is_ready(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_live_dependencies_unavailable()
    database_url = cockroach_document_ai_database
    tenant_id, first_canonical_representation_id = _build_real_candidate(
        database_url=database_url
    )

    validation_repository = CanonicalValidationRepository(database_url=database_url)
    validation_result = validation_repository.validate_canonical_representation(
        tenant_id=tenant_id, canonical_representation_id=first_canonical_representation_id
    )
    assert validation_result.state == "validated"

    chunk_repository = CanonicalChunkGenerationRepository(database_url=database_url)
    chunk_result = chunk_repository.generate_for_representation(
        tenant_id=tenant_id, canonical_representation_id=first_canonical_representation_id
    )
    assert chunk_result.chunk_count > 0

    embedding_repository = CanonicalEmbeddingRepository(database_url=database_url)
    embedding_result = embedding_repository.index_validated_candidate(
        tenant_id=tenant_id, canonical_representation_id=first_canonical_representation_id
    )
    assert embedding_result.chunk_count == chunk_result.chunk_count
    assert embedding_result.current_embedding_count == chunk_result.chunk_count

    activation_repository = CanonicalActivationRepository(database_url=database_url)
    first_activation = activation_repository.activate_validated_candidate(
        tenant_id=tenant_id, canonical_representation_id=first_canonical_representation_id
    )
    assert first_activation.state == "activated"
    assert first_activation.previous_active_canonical_representation_id is None

    second_canonical_representation_id = _clone_ready_competing_candidate(
        database_url=database_url,
        tenant_id=tenant_id,
        source_canonical_representation_id=first_canonical_representation_id,
    )

    second_activation = activation_repository.activate_validated_candidate(
        tenant_id=tenant_id, canonical_representation_id=second_canonical_representation_id
    )
    assert second_activation.state == "activated"
    assert (
        second_activation.previous_active_canonical_representation_id
        == first_canonical_representation_id
    )

    replayed_activation = activation_repository.activate_validated_candidate(
        tenant_id=tenant_id, canonical_representation_id=second_canonical_representation_id
    )
    assert replayed_activation.state == "replayed"

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT representation.canonical_representation_id, representation.state,
                       representation.is_active, representation.activated_at
                  FROM document_ai_canonical_representations AS representation
                 WHERE representation.tenant_id = %s
                   AND representation.document_version_id = (
                       SELECT document_version_id
                         FROM document_ai_canonical_representations
                        WHERE tenant_id = %s
                          AND canonical_representation_id = %s
                   )
                   AND representation.processing_policy_family = 'general-document-understanding'
                 ORDER BY representation.created_at ASC
                """,
                (tenant_id, tenant_id, first_canonical_representation_id),
            )
            rows = cursor.fetchall()

    assert len(rows) == 2
    first_row = rows[0]
    second_row = rows[1]
    assert UUID(str(first_row[0])) == first_canonical_representation_id
    assert str(first_row[1]) == "superseded"
    assert bool(first_row[2]) is False
    assert first_row[3] is not None
    assert UUID(str(second_row[0])) == second_canonical_representation_id
    assert str(second_row[1]) == "active"
    assert bool(second_row[2]) is True
    assert second_row[3] is not None

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                  FROM document_ai_canonical_representations AS representation
                 WHERE representation.tenant_id = %s
                   AND representation.document_version_id = (
                       SELECT document_version_id
                         FROM document_ai_canonical_representations
                        WHERE tenant_id = %s
                          AND canonical_representation_id = %s
                   )
                   AND representation.is_active
                """,
                (tenant_id, tenant_id, first_canonical_representation_id),
            )
            active_count_row = cursor.fetchone()

    assert active_count_row is not None
    assert int(active_count_row[0]) == 1


def _clone_ready_competing_candidate(
    *,
    database_url: str,
    tenant_id: str,
    source_canonical_representation_id: UUID,
) -> UUID:
    competing_canonical_representation_id = uuid4()
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_id, document_version_id, processing_operation_id,
                       canonical_schema_version, processing_policy_family, representation_payload,
                       source_artifact_id, assembly_policy_version, content_hash_sha256,
                       canonical_validation_version, validation_report
                  FROM document_ai_canonical_representations
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                """,
                (tenant_id, source_canonical_representation_id),
            )
            row = cursor.fetchone()
            assert row is not None
            cursor.execute(
                """
                INSERT INTO document_ai_canonical_representations (
                    canonical_representation_id, tenant_id, document_version_id,
                    processing_operation_id, canonical_schema_version, processing_policy_family,
                    state, is_active, representation_payload, source_artifact_id,
                    assembly_policy_version, content_hash_sha256, canonical_validation_version,
                    validation_report, readiness_state, validated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'validated', FALSE, %s::jsonb, %s,
                    %s, %s, %s, %s::jsonb, 'full', now()
                )
                """,
                (
                    competing_canonical_representation_id,
                    tenant_id,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_retrieval_chunks (
                    tenant_id, document_id, document_version_id, canonical_representation_id,
                    chunk_key, ordinal, content_hash_sha256, chunking_policy_version,
                    embedding_text, canonical_element_keys, source_location,
                    structural_context, lifecycle_state
                )
                SELECT tenant_id, document_id, document_version_id, %s, chunk_key, ordinal,
                       content_hash_sha256, chunking_policy_version, embedding_text,
                       canonical_element_keys, source_location, structural_context,
                       lifecycle_state
                  FROM document_ai_retrieval_chunks
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                """,
                (
                    competing_canonical_representation_id,
                    tenant_id,
                    source_canonical_representation_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_chunk_embeddings (
                    tenant_id, retrieval_chunk_id, document_version_id,
                    canonical_representation_id, content_hash_sha256, chunking_policy_version,
                    embedding_model, embedding_version, embedding_dimensions, embedding,
                    index_state
                )
                SELECT new_chunk.tenant_id, new_chunk.retrieval_chunk_id,
                       new_chunk.document_version_id, %s, new_chunk.content_hash_sha256,
                       new_chunk.chunking_policy_version, embedding.embedding_model,
                       embedding.embedding_version, embedding.embedding_dimensions,
                       embedding.embedding, embedding.index_state
                  FROM document_ai_retrieval_chunks AS new_chunk
                  JOIN document_ai_retrieval_chunks AS old_chunk
                    ON old_chunk.tenant_id = new_chunk.tenant_id
                   AND old_chunk.canonical_representation_id = %s
                   AND old_chunk.chunk_key = new_chunk.chunk_key
                   AND old_chunk.chunking_policy_version = new_chunk.chunking_policy_version
                  JOIN document_ai_chunk_embeddings AS embedding
                    ON embedding.tenant_id = old_chunk.tenant_id
                   AND embedding.retrieval_chunk_id = old_chunk.retrieval_chunk_id
                 WHERE new_chunk.tenant_id = %s
                   AND new_chunk.canonical_representation_id = %s
                   AND embedding.embedding_model = %s
                   AND embedding.embedding_version = %s
                   AND embedding.embedding_dimensions = %s
                   AND embedding.index_state = 'active'
                """,
                (
                    competing_canonical_representation_id,
                    source_canonical_representation_id,
                    tenant_id,
                    competing_canonical_representation_id,
                    get_document_ai_embedding_model(),
                    EMBEDDING_VERSION,
                    DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                ),
            )
        connection.commit()
    return competing_canonical_representation_id
