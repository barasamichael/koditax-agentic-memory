"""Milestone 9/10/11/12/13/14/15/16 CockroachDB processing persistence regressions."""

from __future__ import annotations

from pathlib import Path


def test_processing_persistence_cockroach_migration_exposes_required_state() -> None:
    migration = (
        Path(
            "services/document_ai/migrations/cockroachdb/0003_document_ai_processing_persistence.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )

    for marker in (
        "create table if not exists document_ai_processing_operations",
        "create table if not exists document_ai_processing_work_items",
        "create table if not exists document_ai_processing_attempts",
        "create table if not exists document_ai_processing_checkpoints",
        "create table if not exists document_ai_processing_outbox",
        "create table if not exists document_ai_processing_outbox_attempts",
        "create table if not exists document_ai_provider_results",
        "create table if not exists document_ai_source_inspections",
        "fk_document_ai_processing_work_items_current_attempt_scope",
        "uq_document_ai_processing_outbox_operation_event",
        "uq_document_ai_processing_outbox_attempt_number",
        "idx_document_ai_processing_work_items_due_priority",
        "idx_document_ai_processing_outbox_reconciliation",
        "idx_document_ai_provider_results_operation",
        "idx_document_ai_source_inspections_gate",
        "chk_document_ai_source_inspections_reason_code",
    ):
        assert marker in migration


def test_processing_persistence_cockroach_migration_avoids_postgresql_trigger_machinery() -> None:
    migration = (
        Path(
            "services/document_ai/migrations/cockroachdb/0003_document_ai_processing_persistence.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "create trigger" not in migration
    assert "create or replace function" not in migration
    assert "deferrable initially deferred" not in migration
