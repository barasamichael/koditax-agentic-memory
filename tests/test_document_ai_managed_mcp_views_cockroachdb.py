"""Live CockroachDB checks for sanitized Document AI managed-MCP views."""

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


def test_managed_mcp_views_expose_only_sanitized_columns(
    cockroach_document_ai_database: str,
) -> None:
    expected_columns = {
        "document_ai_mcp_document_summary": {
            "tenant_id",
            "document_id",
            "owner_user_id",
            "state",
            "uploaded_at",
            "display_name",
            "category",
            "revision",
            "registry_revision",
            "active_document_version_id",
            "active_version_number",
            "active_version_state",
            "purge_eligible_at",
            "purged_at",
            "compliance_lock_until",
        },
        "document_ai_mcp_processing_status": {
            "tenant_id",
            "processing_operation_id",
            "document_version_id",
            "document_id",
            "operation_kind",
            "operation_state",
            "requested_at",
            "completed_at",
            "failure_category",
            "cancellation_requested_at",
            "processing_work_item_id",
            "work_kind",
            "work_state",
            "priority",
            "retry_count",
            "max_attempts",
            "next_retry_at",
            "leased_until",
            "dead_lettered_at",
        },
        "document_ai_mcp_evidence_lineage": {
            "tenant_id",
            "evidence_item_id",
            "document_version_id",
            "semantic_meaning",
            "derivation_type",
            "assurance_state",
            "completeness_state",
            "correction_state",
            "conflict_state",
            "evidence_created_at",
            "evidence_requirement_id",
            "requirement_source",
            "expected_value_type",
            "multiplicity",
            "evidence_source_id",
            "canonical_element_id",
            "source_region_id",
            "source_artifact_id",
        },
        "document_ai_mcp_correction_status": {
            "tenant_id",
            "correction_id",
            "document_version_id",
            "canonical_element_id",
            "evidence_item_id",
            "correction_state",
            "policy_version",
            "supersedes_correction_id",
            "reversal_of_correction_id",
            "created_at",
            "updated_at",
            "active_correction_id",
            "effective_correction_state",
            "remapping_state",
        },
        "document_ai_mcp_unresolved_evidence_conflicts": {
            "tenant_id",
            "evidence_conflict_id",
            "evidence_item_id",
            "conflicting_evidence_item_id",
            "state",
            "resolved_at",
            "created_at",
            "updated_at",
        },
    }
    forbidden_columns = {
        "document_ai_mcp_document_summary": {"storage_key", "checksum_sha256"},
        "document_ai_mcp_processing_status": {"payload", "diagnostic_payload"},
        "document_ai_mcp_evidence_lineage": {"value_payload"},
        "document_ai_mcp_correction_status": {"corrected_value", "source_observed_value"},
        "document_ai_mcp_unresolved_evidence_conflicts": {"detail"},
    }

    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            for view_name, required_columns in expected_columns.items():
                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (view_name,),
                )
                columns = [row[0] for row in cursor.fetchall()]
                assert required_columns.issubset(set(columns))
                assert forbidden_columns[view_name].isdisjoint(columns)


def test_managed_mcp_views_reject_mutation_attempts(
    cockroach_document_ai_database: str,
) -> None:
    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.Error):
                cursor.execute(
                    """
                    INSERT INTO document_ai_mcp_document_summary (
                        tenant_id,
                        document_id,
                        owner_user_id,
                        state,
                        uploaded_at,
                        display_name,
                        category,
                        revision,
                        registry_revision,
                        active_document_version_id,
                        active_version_number,
                        active_version_state,
                        purge_eligible_at,
                        purged_at,
                        compliance_lock_until
                    )
                    VALUES (
                        'tenant',
                        gen_random_uuid(),
                        gen_random_uuid(),
                        'active',
                        now(),
                        'example',
                        'example',
                        0,
                        0,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        NULL
                    )
                    """,
                )
