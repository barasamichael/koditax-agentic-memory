"""FR-001/002/018/020 and SR-012 regression coverage for Milestone 2."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from services.document_ai.app.document_registry import PersistedDocumentRecord
from services.document_ai.app.document_versioning import DocumentVersioningError
from services.document_ai.app.document_versioning import InMemoryDocumentVersioningStore
from services.document_ai.app.document_versioning import SourceArtifactCommand


TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
OWNER_A = uuid4()
OWNER_B = uuid4()
CHECKSUM_A = "a" * 64


def _document(*, tenant_id: str = TENANT_A, owner_user_id: UUID = OWNER_A) -> PersistedDocumentRecord:
    return PersistedDocumentRecord(
        document_id=uuid4(), tenant_id=tenant_id, owner_user_id=owner_user_id,
        state="processing", storage_key="private/ignored.pdf", uploaded_at="2026-01-01T00:00:00Z",
        checksum_sha256=CHECKSUM_A, size_bytes=1, content_type="application/pdf",
    )


def _artifact(key: str, idempotency_key: str) -> SourceArtifactCommand:
    return SourceArtifactCommand(
        storage_key=key, checksum_sha256=CHECKSUM_A, content_type="application/pdf",
        size_bytes=1, idempotency_key=idempotency_key,
    )


def test_rename_preserves_stable_identity_active_version_and_source_history() -> None:
    store = InMemoryDocumentVersioningStore()
    document = _document()
    store.register_document(document, "before")
    first = store.add_source_version(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
        artifact=_artifact("private/a.pdf", "first"), expected_revision=0,
    )

    renamed = store.rename_document(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
        display_name="after", expected_revision=1,
    )

    assert renamed.document_id == document.document_id
    assert renamed.active_document_version_id == first.document_version_id
    assert [item.document_version_id for item in store.list_versions(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A
    )] == [first.document_version_id]


def test_source_replacement_is_idempotent_preserves_history_and_changes_active_version() -> None:
    store = InMemoryDocumentVersioningStore()
    document = _document()
    store.register_document(document)
    first = store.add_source_version(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
        artifact=_artifact("private/a.pdf", "first"), expected_revision=0,
    )
    second = store.add_source_version(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
        artifact=_artifact("private/b.pdf", "replace"), expected_revision=1,
    )
    retried = store.add_source_version(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
        artifact=_artifact("private/b.pdf", "replace"), expected_revision=999,
    )

    assert retried == second
    assert [item.version_number for item in store.list_versions(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A
    )] == [1, 2]
    assert store.get_active_version(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A
    ) == second
    assert first.source_artifact_id != second.source_artifact_id


def test_tenant_scope_stale_updates_and_lifecycle_are_rejected() -> None:
    store = InMemoryDocumentVersioningStore()
    document = _document()
    store.register_document(document)
    store.add_source_version(
        document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
        artifact=_artifact("private/a.pdf", "first"), expected_revision=0,
    )

    with pytest.raises(DocumentVersioningError, match="document_not_found"):
        store.list_versions(document_id=document.document_id, tenant_id=TENANT_B, owner_user_id=OWNER_B)
    with pytest.raises(DocumentVersioningError, match="stale_document_revision"):
        store.rename_document(
            document_id=document.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
            display_name="stale", expected_revision=0,
        )

    purged = _document()
    store.register_document(purged.model_copy(update={"state": "purged"}))
    with pytest.raises(DocumentVersioningError, match="document_lifecycle_blocks_mutation"):
        store.add_source_version(
            document_id=purged.document_id, tenant_id=TENANT_A, owner_user_id=OWNER_A,
            artifact=_artifact("private/purged.pdf", "purged"), expected_revision=0,
        )


def test_identity_migration_enforces_one_active_version_authority_and_artifact_per_version() -> None:
    sql = Path("database/migrations/0030_document_ai_identity_versioning.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "active_document_version_id" in sql
    assert "fk_document_ai_documents_active_version_scope" in sql
    assert "active document version must belong to the same document and tenant" in sql
    assert "uq_document_ai_document_versions_idempotency" in sql
    assert "uq_document_ai_source_artifacts_version" in sql
