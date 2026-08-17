from __future__ import annotations

import pytest
import psycopg

from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.migrations.cockroachdb import runner


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


def test_lifecycle_compliance_and_purge_persistence_tables_exist(
    cockroach_document_ai_database: str,
) -> None:
    wanted_tables = (
        "document_ai_compliance_overrides",
        "document_ai_lifecycle_audit_evidence",
        "document_ai_compliance_override_audit_evidence",
        "document_ai_purge_operations",
        "document_ai_purge_targets",
        "document_ai_purge_attempts",
    )
    wanted_indexes = (
        "idx_document_ai_compliance_overrides_document_id",
        "idx_document_ai_lifecycle_audit_document_id",
        "idx_document_ai_lifecycle_audit_correlation_id",
        "idx_document_ai_compliance_override_audit_document_id",
        "idx_document_ai_compliance_override_audit_override_id",
        "idx_document_ai_compliance_override_audit_correlation_id",
        "idx_document_ai_purge_operations_scope",
        "uq_document_ai_purge_operations_idempotency",
        "idx_document_ai_purge_unresolved",
        "idx_document_ai_purge_attempts_operation_scope",
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
            tables = {row[0] for row in cursor.fetchall()}
            assert set(wanted_tables).issubset(tables)

            cursor.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = ANY(%s)
                ORDER BY indexname
                """,
                (list(wanted_indexes),),
            )
            indexes = {row[0] for row in cursor.fetchall()}
            assert set(wanted_indexes).issubset(indexes)


def test_lifecycle_compliance_and_purge_columns_are_present(
    cockroach_document_ai_database: str,
) -> None:
    wanted_columns = {
        "document_ai_compliance_overrides": {
            "override_id",
            "tenant_id",
            "document_id",
            "requested_action",
            "requested_by_user_id",
            "requested_by_role",
            "justification",
            "status",
            "created_at",
            "expires_at",
            "response_payload",
            "updated_at",
        },
        "document_ai_lifecycle_audit_evidence": {
            "audit_evidence_id",
            "tenant_id",
            "document_id",
            "action",
            "action_status",
            "previous_state",
            "new_state",
            "user_id",
            "reason_code",
            "trace_id",
            "correlation_id",
            "event_time",
            "payload",
            "created_at",
            "updated_at",
        },
        "document_ai_purge_operations": {
            "purge_operation_id",
            "tenant_id",
            "document_id",
            "state",
            "requested_by_user_id",
            "requested_at",
            "completed_at",
            "correlation_id",
            "idempotency_key",
            "request_fingerprint",
            "payload_fingerprint",
            "manifest_version",
            "replay_count",
            "last_reconciled_at",
        },
        "document_ai_purge_targets": {
            "purge_target_id",
            "tenant_id",
            "purge_operation_id",
            "target_kind",
            "target_reference",
            "state",
            "completed_at",
            "failure_detail",
            "attempt_count",
            "verified_at",
            "required",
        },
        "document_ai_purge_attempts": {
            "purge_attempt_id",
            "tenant_id",
            "purge_operation_id",
            "attempt_number",
            "state",
            "correlation_id",
            "started_at",
            "completed_at",
            "failure_detail",
        },
    }
    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            for table_name, required_columns in wanted_columns.items():
                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = %s
                    """,
                    (table_name,),
                )
                columns = {row[0] for row in cursor.fetchall()}
                assert required_columns.issubset(columns)
