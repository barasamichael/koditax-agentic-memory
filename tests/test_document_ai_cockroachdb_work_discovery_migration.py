"""Regression coverage for the CockroachDB work-discovery migration lane."""

from __future__ import annotations

from pathlib import Path


def test_work_discovery_cockroach_migration_exposes_the_discovery_index() -> None:
    migration = (
        Path(
            "services/document_ai/migrations/cockroachdb/0008_document_ai_processing_work_discovery.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )

    for marker in (
        "create index if not exists idx_document_ai_processing_work_items_discovery",
        "available_at",
        "priority desc",
        "created_at",
        "processing_work_item_id",
        "storing (",
        "tenant_id",
        "processing_operation_id",
        "work_kind",
        "current_processing_attempt_id",
        "dead_lettered_at",
        "where state = 'queued'",
    ):
        assert marker in migration


def test_work_discovery_cockroach_migration_is_not_wrapped_in_an_extra_transaction() -> None:
    migration = (
        Path(
            "services/document_ai/migrations/cockroachdb/0008_document_ai_processing_work_discovery.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "begin;" not in migration
    assert "commit;" not in migration
