"""Verify Milestone 6 durable-confirmation persistence controls."""

from __future__ import annotations

from pathlib import Path


def test_durable_confirmation_outbox_is_transactionally_bound_to_processing_work() -> None:
    """FR-001/002/018/019: acceptance keeps recoverable work after publish failure."""

    sql = (
        Path("database/migrations/0032_document_ai_durable_upload_confirmation.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    required_markers = (
        "create table if not exists document_ai_processing_outbox",
        "foreign key (tenant_id, processing_operation_id)",
        "unique (tenant_id, processing_operation_id, event_type)",
        "state in ('pending', 'publishing', 'published', 'failed')",
        "idx_document_ai_processing_outbox_pending",
        "uq_document_ai_processing_operations_ingestion",
        "uq_document_ai_processing_work_items_operation_kind",
    )
    for marker in required_markers:
        assert marker in sql


def test_persistent_confirmation_registers_the_complete_durable_graph_before_commit() -> None:
    """Document Policy 7.44 and 10.41 keep queue publication non-authoritative."""

    source = Path("services/document_ai/app/document_registry.py").read_text(encoding="utf-8")
    caller = source[source.index("def register_durable_upload_confirmation") :]
    transaction_start = source.index("def _register_persistent_confirmation_transaction")
    transaction = source[transaction_start:]
    assert "execute_document_ai_database_transaction" in caller
    assert "_reconcile_persistent_confirmation_result" in caller
    for relation in (
        "document_ai_documents",
        "document_ai_document_versions",
        "document_ai_source_artifacts",
        "document_ai_processing_operations",
        "document_ai_processing_work_items",
        "document_ai_processing_outbox",
        "document_ai_completion_idempotency",
    ):
        assert relation in transaction
    assert transaction.index("INSERT INTO document_ai_processing_outbox") < transaction.index(
        "INSERT INTO document_ai_completion_idempotency"
    )
