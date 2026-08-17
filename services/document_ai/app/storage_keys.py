"""Canonical storage-key policy helpers for Document AI object locations."""

from __future__ import annotations

from uuid import UUID


class StorageKeyError(ValueError):
    """Reject a storage key that cannot be represented in the governed policy."""


def _validate_tenant_id(tenant_id: str) -> None:
    if not tenant_id or tenant_id in {".", ".."}:
        raise StorageKeyError("tenant_id_invalid")
    if "/" in tenant_id or "\\" in tenant_id or ".." in tenant_id:
        raise StorageKeyError("tenant_id_invalid")


def build_tenant_document_object_key(tenant_id: str, document_id: UUID) -> str:
    """Return the canonical upload-object key for one tenant-scoped document."""

    _validate_tenant_id(tenant_id)
    return f"{tenant_id}/docs/{document_id}"


def build_tenant_document_download_object_key(tenant_id: str, document_id: UUID) -> str:
    """Return the canonical download-object key for one tenant-scoped document."""

    return f"{build_tenant_document_object_key(tenant_id, document_id)}.pdf"


def is_tenant_scoped_object_key(object_key: str, tenant_id: str) -> bool:
    """Return whether an object key remains safely within one tenant namespace."""

    if not object_key.startswith(f"{tenant_id}/"):
        return False
    if "\\" in object_key or ".." in object_key:
        return False
    normalized = object_key.strip().lstrip("/\\")
    if normalized != object_key:
        return False
    return True


def validate_tenant_document_object_key(object_key: str, tenant_id: str, document_id: UUID) -> None:
    """Require one exact tenant/document upload-object key."""

    expected = build_tenant_document_object_key(tenant_id, document_id)
    if object_key != expected:
        raise StorageKeyError("storage_object_key_mismatch")


def validate_tenant_document_download_object_key(
    object_key: str, tenant_id: str, document_id: UUID
) -> None:
    """Require one exact tenant/document download-object key."""

    expected = build_tenant_document_download_object_key(tenant_id, document_id)
    if object_key != expected:
        raise StorageKeyError("storage_object_key_mismatch")
