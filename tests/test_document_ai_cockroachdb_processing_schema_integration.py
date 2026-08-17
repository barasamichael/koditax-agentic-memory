"""Live CockroachDB schema checks for Document AI processing persistence."""

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


def test_processing_persistence_tables_and_indexes_exist(
    cockroach_document_ai_database: str,
) -> None:
    wanted_tables = (
        "document_ai_processing_operations",
        "document_ai_processing_work_items",
        "document_ai_processing_attempts",
        "document_ai_processing_checkpoints",
        "document_ai_processing_outbox",
        "document_ai_processing_outbox_attempts",
        "document_ai_provider_results",
        "document_ai_source_inspections",
        "document_ai_structural_scopes",
    )
    wanted_indexes = (
        "idx_document_ai_processing_work_items_due_priority",
        "idx_document_ai_processing_work_items_tenant_due",
        "idx_document_ai_processing_work_items_discovery",
        "idx_document_ai_processing_outbox_reconciliation",
        "idx_document_ai_processing_outbox_stale_claim",
        "idx_document_ai_provider_results_operation",
        "idx_document_ai_source_inspections_gate",
        "idx_document_ai_structural_scopes_lookup",
        "idx_document_ai_structural_scopes_inspection",
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
                      'idx_document_ai_processing_work_items_due_priority',
                      'idx_document_ai_processing_work_items_tenant_due',
                      'idx_document_ai_processing_work_items_discovery',
                      'idx_document_ai_processing_outbox_reconciliation',
                      'idx_document_ai_processing_outbox_stale_claim',
                      'idx_document_ai_provider_results_operation',
                      'idx_document_ai_source_inspections_gate',
                      'idx_document_ai_structural_scopes_lookup',
                      'idx_document_ai_structural_scopes_inspection'
                  )
                ORDER BY indexname
                """,
            )
            indexes = [row[0] for row in cursor.fetchall()]
            assert sorted(index for index in indexes if index in wanted_indexes) == sorted(
                wanted_indexes
            )
