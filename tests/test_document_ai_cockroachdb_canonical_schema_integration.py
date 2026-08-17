"""Live CockroachDB schema checks for Document AI canonical persistence."""

from __future__ import annotations

import pytest
import psycopg

from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url


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


def test_canonical_persistence_tables_and_indexes_exist(
    cockroach_document_ai_database: str,
) -> None:
    wanted_tables = (
        "document_ai_canonical_representations",
        "document_ai_canonical_elements",
        "document_ai_canonical_relationships",
        "document_ai_source_regions",
        "document_ai_retrieval_chunks",
        "document_ai_chunk_embeddings",
        "document_ai_embedding_records",
    )
    wanted_indexes = (
        "uq_document_ai_active_canonical_representation",
        "uq_document_ai_canonical_representation_provider_result",
        "idx_document_ai_canonical_validation_readiness",
        "uq_document_ai_canonical_elements_stable_key",
        "uq_document_ai_canonical_elements_reading_order",
        "idx_document_ai_canonical_elements_representation",
        "idx_document_ai_source_regions_structural_lookup",
        "idx_document_ai_retrieval_chunks_scope",
        "idx_document_ai_retrieval_chunks_active_canonical_scope",
        "idx_document_ai_retrieval_chunks_exact_lexical",
        "idx_document_ai_retrieval_chunks_exact_source_location",
        "idx_document_ai_retrieval_chunks_exact_structural_context",
        "idx_document_ai_chunk_embeddings_scope",
        "idx_document_ai_chunk_embeddings_vector_search",
    )
    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """,
            )
            tables = [row[0] for row in cursor.fetchall()]
            assert sorted(table for table in tables if table in wanted_tables) == sorted(
                wanted_tables
            )
            cursor.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname IN (
                      'uq_document_ai_active_canonical_representation',
                      'uq_document_ai_canonical_representation_provider_result',
                      'idx_document_ai_canonical_validation_readiness',
                      'uq_document_ai_canonical_elements_stable_key',
                      'uq_document_ai_canonical_elements_reading_order',
                      'idx_document_ai_canonical_elements_representation',
                      'idx_document_ai_source_regions_structural_lookup',
                      'idx_document_ai_retrieval_chunks_scope',
                      'idx_document_ai_retrieval_chunks_active_canonical_scope',
                      'idx_document_ai_retrieval_chunks_exact_lexical',
                      'idx_document_ai_retrieval_chunks_exact_source_location',
                      'idx_document_ai_retrieval_chunks_exact_structural_context',
                      'idx_document_ai_chunk_embeddings_scope',
                      'idx_document_ai_chunk_embeddings_vector_search'
                  )
                ORDER BY indexname
                """,
            )
            indexes = [row[0] for row in cursor.fetchall()]
            assert sorted(index for index in indexes if index in wanted_indexes) == sorted(
                wanted_indexes
                )
            cursor.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = 'idx_document_ai_chunk_embeddings_cosine_active'
                """,
            )
            assert cursor.fetchall() == []
