"""Models for deterministic storage capability issuance runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UploadCapabilityRequestModel:
    tenant_id: str
    owner_user_id: str
    object_key: str
    content_type: str
    expected_size_bytes: int
    checksum_sha256: str
    document_id: str | None


@dataclass(frozen=True)
class DownloadCapabilityRequestModel:
    tenant_id: str
    owner_user_id: str
    object_key: str
    document_id: str | None


@dataclass(frozen=True)
class StorageCapabilityModel:
    capability_id: str
    object_key: str
    expires_at: str
    method: str
    url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class StorageObjectMetadataModel:
    object_key: str
    tenant_id: str
    owner_user_id: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    created_at: str
    document_id: str | None
