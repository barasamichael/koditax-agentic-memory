"""Milestone 3 source-artifact authority regression coverage."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from pathlib import Path

import pytest

from services.document_ai.app.document_registry import DocumentRecord
from services.document_ai.app.document_foundation import SourceArtifactCreate
from services.document_ai.app.document_foundation import InMemorySourceArtifactStore

CHECKSUM = "a" * 64


def _record(
    *, tenant_id: str, version_id: UUID | None = None, checksum: str = CHECKSUM
) -> SourceArtifactCreate:
    return SourceArtifactCreate(
        tenant_id=tenant_id,
        document_version_id=version_id or uuid4(),
        storage_key=f"private/{tenant_id}/object.pdf",
        checksum_sha256=checksum,
        content_type="application/pdf",
        size_bytes=7,
    )


def test_source_registration_is_idempotent_and_checksum_is_not_document_identity() -> None:
    store = InMemorySourceArtifactStore()
    document_a, document_b = uuid4(), uuid4()
    first_record = _record(tenant_id="tenant-a")
    first = store.register_source_artifact(
        document_id=document_a, record=first_record, idempotency_key="upload-a"
    )
    retried = store.register_source_artifact(
        document_id=document_a, record=first_record, idempotency_key="upload-a"
    )
    second = store.register_source_artifact(
        document_id=document_b, record=_record(tenant_id="tenant-a"), idempotency_key="upload-b"
    )

    assert retried == first
    assert second.document_id == document_b
    assert second.source_artifact_id != first.source_artifact_id


def test_source_lookup_is_tenant_and_source_artifact_scoped() -> None:
    store = InMemorySourceArtifactStore()
    document_id = uuid4()
    artifact = store.register_source_artifact(
        document_id=document_id, record=_record(tenant_id="tenant-a"), idempotency_key="upload-a"
    )

    assert (
        store.get_source_artifact(
            tenant_id="tenant-a",
            source_artifact_id=artifact.source_artifact_id,
        )
        == artifact
    )
    assert (
        store.get_source_artifact(
            tenant_id="tenant-b",
            source_artifact_id=artifact.source_artifact_id,
        )
        is None
    )
    assert (
        store.get_source_artifact(
            tenant_id="tenant-a",
            source_artifact_id=uuid4(),
        )
        is None
    )


def test_source_artifact_registration_rejects_mutating_idempotent_request() -> None:
    store = InMemorySourceArtifactStore()
    document_id = uuid4()
    store.register_source_artifact(
        document_id=document_id, record=_record(tenant_id="tenant-a"), idempotency_key="upload-a"
    )

    with pytest.raises(ValueError, match="source_artifact_idempotency_key_payload_mismatch"):
        store.register_source_artifact(
            document_id=document_id,
            record=_record(tenant_id="tenant-a", checksum="b" * 64),
            idempotency_key="upload-a",
        )


def test_public_document_contract_does_not_expose_storage_locator() -> None:
    assert "storage_key" not in DocumentRecord.model_fields
    openapi = Path("contracts/openapi/document_ai.yaml").read_text(encoding="utf-8")
    document_schema = openapi.split("    DocumentRecord:\n", 1)[1].split(
        "    DocumentRecordEnvelope:", 1
    )[0]
    assert "storage_key" not in document_schema


def test_integrity_migration_preserves_immutable_verified_metadata_controls() -> None:
    sql = (
        Path("database/migrations/0031_document_ai_source_artifact_integrity.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "checksum_algorithm" in sql
    assert "verified_media_type" in sql
    assert "no integrity" in sql
