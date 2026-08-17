"""Real-database schema tests for the Document AI CockroachDB migration lane."""

from __future__ import annotations

from typing import Any
from datetime import datetime
from collections.abc import Iterator

import pytest
import psycopg

from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url

EXPECTED_COCKROACH_TABLES = {
    "document_ai_cockroachdb_schema_migrations",
    "document_ai_upload_sessions",
    "document_ai_documents",
    "document_ai_completion_idempotency",
    "document_ai_signed_access_usage",
    "document_ai_document_versions",
    "document_ai_source_artifacts",
    "document_ai_document_bindings",
    "document_ai_evidence_requirements",
    "document_ai_evidence_items",
    "document_ai_evidence_sources",
    "document_ai_evidence_conflicts",
    "document_ai_corrections",
    "document_ai_effective_values",
    "document_ai_correction_invalidations",
    "document_ai_reprocessing_candidates",
    "document_ai_correction_remappings",
    "document_ai_workflow_projections",
    "document_ai_migration_mappings",
}


@pytest.fixture(scope="session")
def cockroach_document_ai_database() -> Iterator[
    tuple[str, set[str], set[str], list[tuple[Any, ...]]]
]:
    database_url = load_document_ai_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for Document AI CockroachDB migration tests.")

    with psycopg.connect(database_url) as connection:
        try:
            runner._validate_target_database(connection)  # type: ignore[arg-type]
        except runner.DocumentAITargetError:
            pytest.skip(
                "DATABASE_URL does not target the expected CockroachDB kodi_dev database."
            )
        before_tables = _load_tables(connection)

    assert runner.main() == 0
    assert runner.main() == 0

    with psycopg.connect(database_url) as connection:
        after_tables = _load_tables(connection)
        ledger_rows = _load_ledger_rows(connection)

    yield database_url, before_tables, after_tables, ledger_rows


def test_document_ai_cockroachdb_schema_isolated_table_changes(
    cockroach_document_ai_database: tuple[str, set[str], set[str], list[tuple[Any, ...]]]
) -> None:
    _, before_tables, after_tables, ledger_rows = cockroach_document_ai_database
    created_tables = after_tables - before_tables
    assert created_tables == EXPECTED_COCKROACH_TABLES - before_tables

    migration_names = [row[0] for row in ledger_rows]
    assert migration_names == [path.name for path in runner.discover_migration_files()]
    assert len({row[1] for row in ledger_rows}) == len(ledger_rows)
    for _, checksum_sha256, applied_at in ledger_rows:
        assert len(str(checksum_sha256)) == 64
        assert isinstance(applied_at, datetime)
        assert applied_at.tzinfo is not None

    assert EXPECTED_COCKROACH_TABLES <= after_tables


def test_document_ai_cockroachdb_lane_repeated_runs_keep_history_stable(
    cockroach_document_ai_database: tuple[str, set[str], set[str], list[tuple[Any, ...]]]
) -> None:
    database_url, _, _, ledger_rows = cockroach_document_ai_database
    with psycopg.connect(database_url) as connection:
        repeated_rows = _load_ledger_rows(connection)

    assert repeated_rows == ledger_rows


def _load_tables(connection: psycopg.Connection[Any]) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )
        rows = cursor.fetchall()
    return {str(row[0]) for row in rows}


def _load_ledger_rows(connection: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT migration_name, checksum_sha256, applied_at
            FROM document_ai_cockroachdb_schema_migrations
            ORDER BY migration_name
            """
        )
        rows = cursor.fetchall()
    return list(rows)
