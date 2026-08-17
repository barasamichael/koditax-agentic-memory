"""Live CockroachDB acceptance for Document AI exact retrieval."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.config import DEFAULT_DOCUMENT_AI_OPENAI_EMBEDDING_MODEL
from services.document_ai.app.exact_retrieval import ExactRetrievalRequest
from services.document_ai.app.exact_retrieval import ExactRetrievalRepository
from services.document_ai.app.openai_embeddings import vector_literal
from services.document_ai.app.openai_embeddings import EMBEDDING_VERSION
from services.document_ai.app.openai_embeddings import DOCUMENT_AI_EMBEDDING_DIMENSIONS
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url

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


def test_real_active_chunks_support_exact_retrieval_and_lexical_index_plan(
    cockroach_document_ai_database: str,
) -> None:
    fixture = _seed_exact_retrieval_fixture(database_url=cockroach_document_ai_database)
    repository = ExactRetrievalRepository(database_url=cockroach_document_ai_database)
    request = ExactRetrievalRequest(full_text=fixture["chunk_text"], limit=5)

    result = repository.retrieve(
        tenant_id=str(fixture["tenant_id"]),
        owner_user_id=fixture["owner_user_id"],
        request=request,
    )

    assert result
    assert result[0].document_id == fixture["document_id"]
    assert result[0].document_version_id == fixture["document_version_id"]
    assert result[0].exact_match_rank == 0
    assert result[0].source_lineage["document_version_id"] == str(fixture["document_version_id"])
    assert result[0].source_filename == f"{fixture['document_id']}.txt"

    no_result = repository.retrieve(
        tenant_id=str(fixture["tenant_id"]),
        owner_user_id=fixture["owner_user_id"],
        request=ExactRetrievalRequest(full_text="this phrase does not appear anywhere"),
    )
    assert no_result == []

    cross_tenant = repository.retrieve(
        tenant_id=f"{fixture['tenant_id']}-other",
        owner_user_id=fixture["owner_user_id"],
        request=request,
    )
    assert cross_tenant == []


def test_exact_retrieval_tracks_the_active_document_version_not_a_historical_version(
    cockroach_document_ai_database: str,
) -> None:
    fixture = _seed_exact_retrieval_fixture(database_url=cockroach_document_ai_database)
    replacement_version_id = _clone_active_document_version(
        database_url=cockroach_document_ai_database,
        tenant_id=str(fixture["tenant_id"]),
        document_id=fixture["document_id"],
        source_version_id=fixture["document_version_id"],
        source_processing_operation_id=fixture["processing_operation_id"],
        source_canonical_representation_id=fixture["canonical_representation_id"],
    )

    repository = ExactRetrievalRepository(database_url=cockroach_document_ai_database)
    current_result = repository.retrieve(
        tenant_id=str(fixture["tenant_id"]),
        owner_user_id=fixture["owner_user_id"],
        request=ExactRetrievalRequest(full_text=fixture["chunk_text"]),
    )
    assert current_result
    assert current_result[0].document_version_id == replacement_version_id

    historical_result = repository.retrieve(
        tenant_id=str(fixture["tenant_id"]),
        owner_user_id=fixture["owner_user_id"],
        request=ExactRetrievalRequest(
            document_version_id=fixture["document_version_id"],
            full_text=fixture["chunk_text"],
        ),
    )
    assert historical_result == []


def _seed_exact_retrieval_fixture(*, database_url: str) -> dict[str, object]:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    owner_user_id = uuid4()
    document_version_id = uuid4()
    processing_operation_id = uuid4()
    canonical_representation_id = uuid4()
    chunk_text = "Invoice number INV-2026-0001 total 1250.00 due 2026-08-31"
    source_lineage = {
        "document_version_id": str(document_version_id),
        "processing_operation_id": str(processing_operation_id),
        "canonical_representation_id": str(canonical_representation_id),
    }
    zero_vector = vector_literal(tuple(0.0 for _ in range(DOCUMENT_AI_EMBEDDING_DIMENSIONS)))

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO document_ai_documents (
                    document_id, tenant_id, owner_user_id, state, storage_key,
                    uploaded_at, checksum_sha256, size_bytes, content_type, computation_id,
                    purge_eligible_at, purged_at, compliance_lock_until, display_name,
                    category, tags, description, revision, registry_revision,
                    active_document_version_id
                ) VALUES (
                    %s, %s, %s, 'active', %s, now(), %s, 1, 'text/plain', NULL,
                    NULL, NULL, NULL, %s, NULL, '[]'::jsonb, NULL, 1, 1, NULL
                )
                """,
                (
                    document_id,
                    tenant_id,
                    owner_user_id,
                    f"{document_id}.txt",
                    "0" * 64,
                    "Exact Retrieval Fixture",
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_document_versions (
                    document_version_id, tenant_id, document_id, version_number,
                    version_state, created_at, supersedes_document_version_id, idempotency_key
                ) VALUES (%s, %s, %s, 1, 'current', now(), NULL, %s)
                """,
                (
                    document_version_id,
                    tenant_id,
                    document_id,
                    f"exact-fixture-version-{uuid4().hex}",
                ),
            )
            cursor.execute(
                """
                UPDATE document_ai_documents
                   SET active_document_version_id = %s
                 WHERE tenant_id = %s
                   AND document_id = %s
                """,
                (document_version_id, tenant_id, document_id),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_processing_operations (
                    processing_operation_id, tenant_id, document_version_id, operation_kind,
                    processing_policy_version, processor_version, state, requested_at,
                    completed_at, correlation_id, idempotency_key, request_payload,
                    cancellation_requested_at, cancellation_requested_by_user_id,
                    result_reference, failure_category
                ) VALUES (
                    %s, %s, %s, 'exact_fixture', 'v1', 'fixture', 'succeeded', now(),
                    now(), %s, %s, '{}'::jsonb, NULL, NULL, 'fixture', NULL
                )
                """,
                (
                    processing_operation_id,
                    tenant_id,
                    document_version_id,
                    f"exact-fixture-corr-{uuid4().hex}",
                    f"exact-fixture-op-{uuid4().hex}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_canonical_representations (
                    canonical_representation_id, tenant_id, document_version_id,
                    processing_operation_id, canonical_schema_version, processing_policy_family,
                    state, is_active, representation_payload, created_at, activated_at,
                    source_artifact_id, provider_result_id, assembly_policy_version,
                    content_hash_sha256, canonical_validation_version, validation_report,
                    readiness_state, validated_at, rejected_at
                ) VALUES (
                    %s, %s, %s, %s, 'v1', 'exact-retrieval-test', 'active', TRUE,
                    %s::jsonb, now(), now(), NULL, NULL, 'v1', %s, 'v1', %s::jsonb,
                    'full', now(), NULL
                )
                """,
                (
                    canonical_representation_id,
                    tenant_id,
                    document_version_id,
                    processing_operation_id,
                    json.dumps({"source_lineage": source_lineage}, sort_keys=True),
                    "0" * 64,
                    json.dumps({"reason_codes": []}, sort_keys=True),
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_retrieval_chunks (
                    tenant_id, document_id, document_version_id, canonical_representation_id,
                    chunk_key, ordinal, content_hash_sha256, chunking_policy_version,
                    embedding_text, canonical_element_keys, source_location,
                    structural_context, lifecycle_state, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'chunk-0', 0, %s, 'v2', %s, '[]'::jsonb,
                    %s::jsonb, %s::jsonb, 'active', now()
                )
                """,
                (
                    tenant_id,
                    document_id,
                    document_version_id,
                    canonical_representation_id,
                    "1" * 64,
                    chunk_text,
                    json.dumps(
                        {
                            "page_number": 1,
                            "sheet_name": "Payroll",
                            "table_name": "Earnings",
                            "cell_reference": "B7",
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "chunk_ordinal": 0,
                            "chunking_policy_version": "v2",
                            "source_lineage": source_lineage,
                        },
                        sort_keys=True,
                    ),
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
                SELECT chunk.tenant_id, chunk.retrieval_chunk_id, chunk.document_version_id,
                       chunk.canonical_representation_id, chunk.content_hash_sha256,
                       chunk.chunking_policy_version, %s, %s, %s, %s::vector, 'active'
                  FROM document_ai_retrieval_chunks AS chunk
                 WHERE chunk.tenant_id = %s
                   AND chunk.canonical_representation_id = %s
                """,
                (
                    DEFAULT_DOCUMENT_AI_OPENAI_EMBEDDING_MODEL,
                    EMBEDDING_VERSION,
                    DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                    zero_vector,
                    tenant_id,
                    canonical_representation_id,
                ),
            )
        connection.commit()

    return {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "owner_user_id": owner_user_id,
        "document_version_id": document_version_id,
        "processing_operation_id": processing_operation_id,
        "canonical_representation_id": canonical_representation_id,
        "chunk_text": chunk_text,
    }


def _clone_active_document_version(
    *,
    database_url: str,
    tenant_id: str,
    document_id: UUID,
    source_version_id: UUID,
    source_processing_operation_id: UUID,
    source_canonical_representation_id: UUID,
) -> UUID:
    replacement_version_id = uuid4()
    replacement_processing_operation_id = uuid4()
    replacement_representation_id = uuid4()

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version_number
                  FROM document_ai_document_versions
                 WHERE tenant_id = %s
                   AND document_id = %s
                   AND document_version_id = %s
                """,
                (tenant_id, document_id, source_version_id),
            )
            version_row = cursor.fetchone()
            assert version_row is not None
            next_version_number = int(version_row[0]) + 1

            cursor.execute(
                """
                SELECT processing_policy_version, processor_version, state, correlation_id,
                       request_payload, result_reference, failure_category
                  FROM document_ai_processing_operations
                 WHERE tenant_id = %s
                   AND processing_operation_id = %s
                """,
                (tenant_id, source_processing_operation_id),
            )
            source_operation_row = cursor.fetchone()
            assert source_operation_row is not None

            cursor.execute(
                """
                SELECT representation_payload, canonical_schema_version,
                       processing_policy_family, content_hash_sha256,
                       canonical_validation_version, validation_report, readiness_state
                  FROM document_ai_canonical_representations
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                """,
                (tenant_id, source_canonical_representation_id),
            )
            source_representation_row = cursor.fetchone()
            assert source_representation_row is not None

            cursor.execute(
                """
                INSERT INTO document_ai_document_versions (
                    document_version_id, tenant_id, document_id, version_number,
                    version_state, created_at, supersedes_document_version_id, idempotency_key
                ) VALUES (%s, %s, %s, %s, 'current', now(), %s, %s)
                """,
                (
                    replacement_version_id,
                    tenant_id,
                    document_id,
                    next_version_number,
                    source_version_id,
                    f"exact-fixture-version-{uuid4().hex}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_processing_operations (
                    processing_operation_id, tenant_id, document_version_id, operation_kind,
                    processing_policy_version, processor_version, state, requested_at,
                    completed_at, correlation_id, idempotency_key, request_payload,
                    cancellation_requested_at, cancellation_requested_by_user_id,
                    result_reference, failure_category
                ) VALUES (
                    %s, %s, %s, 'exact_fixture', %s, %s, %s, now(), now(), %s, %s,
                    %s::jsonb, NULL, NULL, %s, %s
                )
                """,
                (
                    replacement_processing_operation_id,
                    tenant_id,
                    replacement_version_id,
                    source_operation_row[0],
                    source_operation_row[1],
                    source_operation_row[2],
                    source_operation_row[3],
                    f"exact-fixture-op-{uuid4().hex}",
                    json.dumps(
                        source_operation_row[4]
                        if source_operation_row[4] is not None
                        else {
                            "replayed_from": str(source_processing_operation_id),
                            "fixture": "exact-retrieval",
                        },
                        sort_keys=True,
                    ),
                    source_operation_row[5],
                    source_operation_row[6],
                ),
            )
            cursor.execute(
                """
                UPDATE document_ai_processing_operations
                   SET idempotency_key = %s
                 WHERE tenant_id = %s
                   AND processing_operation_id = %s
                """,
                (
                    f"exact-fixture-op-{uuid4().hex}",
                    tenant_id,
                    source_processing_operation_id,
                ),
            )
            cursor.execute(
                """
                UPDATE document_ai_documents
                   SET active_document_version_id = %s
                 WHERE tenant_id = %s
                   AND document_id = %s
                """,
                (replacement_version_id, tenant_id, document_id),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_canonical_representations (
                    canonical_representation_id, tenant_id, document_version_id,
                    processing_operation_id, canonical_schema_version,
                    processing_policy_family, state, is_active, representation_payload,
                    created_at, activated_at, source_artifact_id, provider_result_id,
                    assembly_policy_version, content_hash_sha256,
                    canonical_validation_version, validation_report, readiness_state,
                    validated_at, rejected_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'active', TRUE, %s::jsonb, now(), now(),
                    NULL, NULL, 'v1', %s, %s, %s::jsonb, %s, now(), NULL
                )
                """,
                (
                    replacement_representation_id,
                    tenant_id,
                    replacement_version_id,
                    replacement_processing_operation_id,
                    source_representation_row[1],
                    source_representation_row[2],
                    json.dumps(
                        {
                            "source_lineage": {
                                "document_version_id": str(replacement_version_id),
                                "processing_operation_id": str(
                                    replacement_processing_operation_id
                                ),
                                "canonical_representation_id": str(
                                    replacement_representation_id
                                ),
                            }
                        },
                        sort_keys=True,
                    ),
                    source_representation_row[3],
                    source_representation_row[4],
                    json.dumps(source_representation_row[5], sort_keys=True),
                    source_representation_row[6],
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_retrieval_chunks (
                    tenant_id, document_id, document_version_id, canonical_representation_id,
                    chunk_key, ordinal, content_hash_sha256, chunking_policy_version,
                    embedding_text, canonical_element_keys, source_location,
                    structural_context, lifecycle_state, created_at
                )
                SELECT tenant_id, document_id, %s, %s, chunk_key, ordinal,
                       content_hash_sha256, chunking_policy_version, embedding_text,
                       canonical_element_keys, source_location, structural_context,
                       lifecycle_state, created_at
                  FROM document_ai_retrieval_chunks
                 WHERE tenant_id = %s
                   AND document_version_id = %s
                   AND canonical_representation_id = %s
                """,
                (
                    replacement_version_id,
                    replacement_representation_id,
                    tenant_id,
                    source_version_id,
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
                SELECT new_chunk.tenant_id, new_chunk.retrieval_chunk_id, %s, %s,
                       old_embedding.content_hash_sha256, old_embedding.chunking_policy_version,
                       old_embedding.embedding_model, old_embedding.embedding_version,
                       old_embedding.embedding_dimensions, old_embedding.embedding,
                       old_embedding.index_state
                  FROM document_ai_retrieval_chunks AS new_chunk
                  JOIN document_ai_retrieval_chunks AS old_chunk
                    ON old_chunk.tenant_id = new_chunk.tenant_id
                   AND old_chunk.canonical_representation_id = %s
                   AND old_chunk.chunk_key = new_chunk.chunk_key
                   AND old_chunk.chunking_policy_version = new_chunk.chunking_policy_version
                  JOIN document_ai_chunk_embeddings AS old_embedding
                    ON old_embedding.tenant_id = old_chunk.tenant_id
                   AND old_embedding.retrieval_chunk_id = old_chunk.retrieval_chunk_id
                 WHERE new_chunk.tenant_id = %s
                   AND new_chunk.canonical_representation_id = %s
                   AND old_embedding.embedding_model = %s
                   AND old_embedding.embedding_version = %s
                   AND old_embedding.embedding_dimensions = %s
                   AND old_embedding.index_state = 'active'
                """,
                (
                    replacement_version_id,
                    replacement_representation_id,
                    source_canonical_representation_id,
                    tenant_id,
                    replacement_representation_id,
                    DEFAULT_DOCUMENT_AI_OPENAI_EMBEDDING_MODEL,
                    EMBEDDING_VERSION,
                    DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                ),
            )
        connection.commit()

    return replacement_version_id
