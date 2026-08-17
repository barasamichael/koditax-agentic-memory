"""Deterministic storage capability token and metadata builder."""

from __future__ import annotations

import os
from uuid import uuid5
from uuid import NAMESPACE_URL
import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Lock
from dataclasses import asdict
from dataclasses import dataclass

from services.storage.app.config import get_capability_base_time
from services.storage.app.config import get_upload_capability_ttl_seconds
from services.storage.app.config import get_download_capability_ttl_seconds
from services.storage.app.config import get_metadata_capability_ttl_seconds
from services.storage.app.errors import INVALID_STORAGE_REQUEST
from services.storage.app.errors import STORAGE_CAPABILITY_EXPIRED
from services.storage.app.errors import STORAGE_CAPABILITY_NOT_FOUND
from services.storage.app.models import StorageCapabilityModel
from services.storage.app.models import StorageObjectMetadataModel
from services.storage.app.models import UploadCapabilityRequestModel
from services.storage.app.models import DownloadCapabilityRequestModel
from shared.determinism.input_hash import canonical_json_dumps


@dataclass(frozen=True)
class CapabilityIssueResult:
    status: str
    capability: StorageCapabilityModel


class StorageCapabilityResolutionError(RuntimeError):
    """Represent deterministic capability resolution failures."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class StorageCapabilityService:
    """Provide deterministic capability issuance and metadata retrieval."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._capabilities_by_seed: dict[str, StorageCapabilityModel] = {}
        self._capabilities_by_id: dict[str, StorageCapabilityModel] = {}
        self._metadata_by_object_key: dict[str, StorageObjectMetadataModel] = {}

    def issue_upload_capability(
        self,
        *,
        request_model: UploadCapabilityRequestModel,
        idempotency_key: str,
    ) -> CapabilityIssueResult:
        seed_payload = {
            "capability_type": "upload",
            **asdict(request_model),
            "idempotency_key": idempotency_key,
        }
        seed = canonical_json_dumps(seed_payload)
        capability = self._build_capability(
            seed=seed,
            capability_type="upload",
            object_key=request_model.object_key,
            method="PUT",
            ttl_seconds=get_upload_capability_ttl_seconds(),
            tenant_id=request_model.tenant_id,
        )
        created_at = get_capability_base_time().isoformat()
        metadata = StorageObjectMetadataModel(
            object_key=request_model.object_key,
            tenant_id=request_model.tenant_id,
            owner_user_id=request_model.owner_user_id,
            content_type=request_model.content_type,
            size_bytes=request_model.expected_size_bytes,
            checksum_sha256=request_model.checksum_sha256,
            created_at=created_at,
            document_id=request_model.document_id,
        )
        with self._lock:
            existing = self._capabilities_by_seed.get(seed)
            if existing is None:
                self._capabilities_by_seed[seed] = capability
                self._capabilities_by_id[capability.capability_id] = capability
                self._metadata_by_object_key[request_model.object_key] = metadata
                return CapabilityIssueResult(status="capability_issued", capability=capability)
            self._metadata_by_object_key[request_model.object_key] = metadata
            return CapabilityIssueResult(status="capability_replayed", capability=existing)

    def upsert_object_metadata(self, *, metadata: StorageObjectMetadataModel) -> None:
        """Register object metadata deterministically for capability issuance."""

        with self._lock:
            self._metadata_by_object_key[metadata.object_key] = metadata

    def issue_download_capability(
        self,
        *,
        request_model: DownloadCapabilityRequestModel,
        idempotency_key: str,
    ) -> CapabilityIssueResult | None:
        with self._lock:
            metadata = self._metadata_by_object_key.get(request_model.object_key)
        if metadata is None:
            return None
        seed_payload = {
            "capability_type": "download",
            **asdict(request_model),
            "idempotency_key": idempotency_key,
        }
        seed = canonical_json_dumps(seed_payload)
        capability = self._build_capability(
            seed=seed,
            capability_type="download",
            object_key=request_model.object_key,
            method="GET",
            ttl_seconds=get_download_capability_ttl_seconds(),
            tenant_id=request_model.tenant_id,
        )
        with self._lock:
            existing = self._capabilities_by_seed.get(seed)
            if existing is None:
                self._capabilities_by_seed[seed] = capability
                self._capabilities_by_id[capability.capability_id] = capability
                return CapabilityIssueResult(status="capability_issued", capability=capability)
            return CapabilityIssueResult(status="capability_replayed", capability=existing)

    def get_object_metadata(self, *, object_key: str) -> StorageObjectMetadataModel | None:
        with self._lock:
            metadata = self._metadata_by_object_key.get(object_key)
            if metadata is None:
                return None
            return metadata

    def build_metadata_capability(
        self, *, object_key: str, tenant_id: str
    ) -> StorageCapabilityModel:
        seed_payload = {
            "capability_type": "metadata",
            "object_key": object_key,
            "tenant_id": tenant_id,
        }
        seed = canonical_json_dumps(seed_payload)
        capability = self._build_capability(
            seed=seed,
            capability_type="metadata",
            object_key=object_key,
            method="GET",
            ttl_seconds=get_metadata_capability_ttl_seconds(),
            tenant_id=tenant_id,
        )
        with self._lock:
            self._capabilities_by_id[capability.capability_id] = capability
        return capability

    def resolve_download_capability(self, *, capability_id: str) -> StorageCapabilityModel:
        """Resolve one download capability by id and enforce deterministic expiry."""

        normalized_capability_id = capability_id.strip()
        if normalized_capability_id == "":
            raise StorageCapabilityResolutionError(
                reason_code=INVALID_STORAGE_REQUEST,
                message="Storage capability identifier is invalid.",
            )

        with self._lock:
            capability = self._capabilities_by_id.get(normalized_capability_id)
        if capability is None or capability.method != "GET":
            raise StorageCapabilityResolutionError(
                reason_code=STORAGE_CAPABILITY_NOT_FOUND,
                message="Storage capability was not found.",
            )

        expires_at = _parse_datetime(value=capability.expires_at)
        if expires_at <= _get_storage_reference_time():
            raise StorageCapabilityResolutionError(
                reason_code=STORAGE_CAPABILITY_EXPIRED,
                message="Storage capability has expired.",
            )
        return capability

    def _build_capability(
        self,
        *,
        seed: str,
        capability_type: str,
        object_key: str,
        method: str,
        ttl_seconds: int,
        tenant_id: str,
    ) -> StorageCapabilityModel:
        capability_id = str(uuid5(NAMESPACE_URL, seed))
        expires_at = (get_capability_base_time() + timedelta(seconds=ttl_seconds)).isoformat()
        token_source = f"{capability_type}:{seed}"
        token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()
        return StorageCapabilityModel(
            capability_id=capability_id,
            object_key=object_key,
            expires_at=expires_at,
            method=method,
            url=f"https://storage.local/capabilities/{capability_id}",
            headers={
                "x-storage-capability-token": token,
                "x-storage-tenant-id": tenant_id,
            },
        )


def _parse_datetime(*, value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _get_storage_reference_time() -> datetime:
    raw_value = os.getenv("STORAGE_REFERENCE_TIME", "").strip()
    if raw_value == "":
        return get_capability_base_time()
    return _parse_datetime(value=raw_value)
