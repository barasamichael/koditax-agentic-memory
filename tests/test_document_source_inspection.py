"""Milestone 13 technical preflight and durable processing-gate regressions."""

from pathlib import Path

from services.document_ai.app.source_inspection import inspect_source_bytes


def test_empty_unknown_and_archive_sources_have_explicit_quarantine_outcomes() -> None:
    assert inspect_source_bytes(b"", declared_media_type="application/pdf").reason == "source_empty"
    assert (
        inspect_source_bytes(b"not a document", declared_media_type="application/pdf").reason
        == "unsupported_format"
    )
    assert (
        inspect_source_bytes(b"PK\x03\x04archive", declared_media_type="application/pdf").reason
        == "archive_not_permitted"
    )


def test_inspection_does_not_trust_declared_media_type_when_pdf_signature_disagrees() -> None:
    result = inspect_source_bytes(b"%PDF-1.7\n", declared_media_type="text/plain")
    assert result.reason == "declared_media_type_mismatch"
    assert result.disposition == "quarantined"
    assert result.observed_source_family == "pdf"
    assert result.observed_source_format == "pdf"
    assert result.source_size_bytes == len(b"%PDF-1.7\n")


def test_inspection_records_family_and_format_for_structured_text() -> None:
    payload = b'{"a": 1}'
    result = inspect_source_bytes(payload, declared_media_type="application/json")
    assert result.disposition == "accepted"
    assert result.reason == "accepted"
    assert result.observed_source_family == "text"
    assert result.observed_source_format == "json"
    assert result.source_size_bytes == len(payload)
    assert result.diagnostic_payload["observed_source_family"] == "text"
    assert result.diagnostic_payload["observed_source_format"] == "json"


def test_migration_requires_accepted_inspection_before_semantic_work() -> None:
    migration = Path("database/migrations/0039_document_ai_safe_source_inspection.sql").read_text()
    assert "document_ai_source_inspections" in migration
    assert "source_inspection_required_before_general_processing" in migration
    assert "policy_version = 'v1' AND disposition = 'accepted'" in migration


def test_upload_confirmation_queues_inspection_and_not_semantic_processing() -> None:
    source = Path("services/document_ai/app/document_registry.py").read_text()
    transaction = source[source.index("def _register_persistent_confirmation_transaction") :]
    assert "'source_inspection'" in transaction
    assert "'source_inspection_requested'" in transaction
    assert "'general_document_understanding_requested'" not in transaction
