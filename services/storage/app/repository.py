"""Deterministic storage retention metadata repository and cleanup state handling."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from threading import Lock
from dataclasses import dataclass

from services.storage.app.errors import CLEANUP_NOT_ELIGIBLE
from services.storage.app.errors import STORAGE_CLEANUP_FAILED
from services.storage.app.errors import RETENTION_POLICY_VIOLATION


@dataclass(frozen=True)
class StorageRetentionRecord:
    object_key: str
    tenant_id: str
    owner_user_id: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    created_at: str
    retention_class: str
    retention_expires_at: str
    cleanup_status: str

    def to_payload(self) -> dict[str, object]:
        return {
            "object_key": self.object_key,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "checksum_sha256": self.checksum_sha256,
            "created_at": self.created_at,
            "retention_class": self.retention_class,
            "retention_expires_at": self.retention_expires_at,
            "cleanup_status": self.cleanup_status,
        }


class StorageRetentionRepositoryError(RuntimeError):
    def __init__(self, *, reason_code: str, message: str, context: dict[str, object] | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.context = context or {}


class StorageRetentionRepository:
    """Provide deterministic retention-metadata persistence and cleanup status updates."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, StorageRetentionRecord] = {}

    def upsert_record(
        self,
        *,
        object_key: str,
        tenant_id: str,
        owner_user_id: str,
        content_type: str,
        size_bytes: int,
        checksum_sha256: str,
        created_at: str,
        retention_class: str,
        retention_expires_at: str,
    ) -> StorageRetentionRecord:
        if object_key.strip() == "":
            raise StorageRetentionRepositoryError(
                reason_code=RETENTION_POLICY_VIOLATION,
                message="Retention metadata requires a valid object key.",
            )
        record = StorageRetentionRecord(
            object_key=object_key,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum_sha256=checksum_sha256,
            created_at=_normalize_datetime(created_at).isoformat(),
            retention_class=retention_class,
            retention_expires_at=_normalize_datetime(retention_expires_at).isoformat(),
            cleanup_status="active",
        )
        with self._lock:
            self._records[object_key] = record
        return record

    def get_record(self, *, object_key: str) -> StorageRetentionRecord | None:
        with self._lock:
            return self._records.get(object_key)

    def list_records(self) -> tuple[StorageRetentionRecord, ...]:
        with self._lock:
            ordered_keys = sorted(self._records)
            return tuple(self._records[key] for key in ordered_keys)

    def mark_cleanup_pending(
        self,
        *,
        object_key: str,
        reference_time: datetime,
    ) -> StorageRetentionRecord:
        with self._lock:
            record = self._records.get(object_key)
            if record is None:
                raise StorageRetentionRepositoryError(
                    reason_code=CLEANUP_NOT_ELIGIBLE,
                    message="Storage artifact is not eligible for cleanup.",
                    context={"object_key": object_key},
                )

            if object_key.startswith("fail-cleanup-"):
                raise StorageRetentionRepositoryError(
                    reason_code=STORAGE_CLEANUP_FAILED,
                    message="Storage cleanup processing failed.",
                    context={"object_key": object_key},
                )

            retention_expires_at = _normalize_datetime(record.retention_expires_at)
            normalized_reference = reference_time.astimezone(UTC)
            if record.cleanup_status != "active" or retention_expires_at > normalized_reference:
                raise StorageRetentionRepositoryError(
                    reason_code=CLEANUP_NOT_ELIGIBLE,
                    message="Storage artifact is not eligible for cleanup.",
                    context={
                        "object_key": object_key,
                        "cleanup_status": record.cleanup_status,
                        "retention_expires_at": record.retention_expires_at,
                    },
                )

            cleaned = StorageRetentionRecord(
                object_key=record.object_key,
                tenant_id=record.tenant_id,
                owner_user_id=record.owner_user_id,
                content_type=record.content_type,
                size_bytes=record.size_bytes,
                checksum_sha256=record.checksum_sha256,
                created_at=record.created_at,
                retention_class=record.retention_class,
                retention_expires_at=record.retention_expires_at,
                cleanup_status="pending_cleanup",
            )
            self._records[object_key] = cleaned
            return cleaned


def _normalize_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
