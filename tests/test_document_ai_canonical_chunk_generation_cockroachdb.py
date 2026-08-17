"""Live CockroachDB coverage for canonical chunk generation replay safety."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.canonical_chunk_generation import (
    CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT,
)
from services.document_ai.app.canonical_chunk_generation import CanonicalChunkGenerationRepository
from services.document_ai.app.config import get_document_ai_s3_bucket
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.migrations.cockroachdb import runner
from tests.test_document_ai_canonical_validation_cockroachdb import _build_real_candidate

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


def test_real_validated_canonical_candidate_generates_durable_chunks_and_continuation(
    cockroach_document_ai_database: str,
) -> None:
    bucket = get_document_ai_s3_bucket()
    if bucket is None or "<" in bucket or ">" in bucket:
        pytest.skip("Real S3 bucket configuration is not available in this workspace.")

    database_url = cockroach_document_ai_database
    tenant_id, canonical_representation_id = _build_real_candidate(database_url=database_url)
    repository = CanonicalChunkGenerationRepository(database_url=database_url)

    first = repository.generate_for_representation(
        tenant_id=tenant_id, canonical_representation_id=canonical_representation_id
    )
    second = repository.generate_for_representation(
        tenant_id=tenant_id, canonical_representation_id=canonical_representation_id
    )

    assert first.state == "generated"
    assert second.state == "replayed"
    assert first.chunk_keys == second.chunk_keys
    assert first.chunk_count == second.chunk_count
    assert first.continuation_event == CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT
    assert first.chunk_count > 0

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT chunk_key, content_hash_sha256, chunking_policy_version,
                       source_location, structural_context
                  FROM document_ai_retrieval_chunks
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                 ORDER BY COALESCE((structural_context->>'chunk_ordinal')::INT, 0) ASC,
                          created_at ASC, chunk_key ASC
                """,
                (tenant_id, canonical_representation_id),
            )
            rows = cursor.fetchall()
            assert len(rows) == first.chunk_count
            first_row = rows[0]
            assert str(first_row[2]) == "v2"
            assert isinstance(first_row[3], dict)
            assert isinstance(first_row[4], dict)
            assert first_row[4]["source_lineage"]["provider_result_id"]
            assert first_row[4]["source_lineage"]["document_version_id"]
            assert first_row[4]["source_lineage"]["source_artifact_id"]

            cursor.execute(
                """
                SELECT event_type, payload
                  FROM document_ai_processing_outbox
                 WHERE tenant_id = %s
                   AND processing_operation_id = (
                       SELECT processing_operation_id
                         FROM document_ai_canonical_representations
                        WHERE tenant_id = %s
                          AND canonical_representation_id = %s
                   )
                """,
                (tenant_id, tenant_id, canonical_representation_id),
            )
            outbox_rows = cursor.fetchall()
            assert CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT in {
                str(row[0]) for row in outbox_rows
            }
            continuation_rows = [
                row for row in outbox_rows if str(row[0]) == CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT
            ]
            assert continuation_rows
            payload = continuation_rows[0][1]
            assert isinstance(payload, dict)
            assert payload["chunk_count"] == first.chunk_count
            assert payload["chunk_keys"] == list(first.chunk_keys)
            assert payload["chunk_generation_identity"] == first.chunk_generation_identity
