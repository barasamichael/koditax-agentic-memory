"""Deterministic retention-policy helpers and cleanup hooks for storage artifacts."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from services.storage.app.config import get_capability_base_time
from services.storage.app.errors import CLEANUP_NOT_ELIGIBLE
from services.storage.app.repository import StorageRetentionRecord
from services.storage.app.repository import StorageRetentionRepository
from services.storage.app.repository import StorageRetentionRepositoryError

RETENTION_DAYS_BY_CLASS: dict[str, int] = {
    "tax_summary": 2555,
    "worksheet": 2555,
    "comparative_view": 2555,
    "audit_package": 3650,
    "export_bundle": 365,
}
DEFAULT_RETENTION_CLASS = "export_bundle"


def retention_class_for_object_key(*, object_key: str) -> str:
    normalized_key = object_key.strip().lower()
    if "tax_summary" in normalized_key:
        return "tax_summary"
    if "worksheet" in normalized_key:
        return "worksheet"
    if "comparative" in normalized_key:
        return "comparative_view"
    if "audit_package" in normalized_key:
        return "audit_package"
    return DEFAULT_RETENTION_CLASS


def compute_retention_expires_at(*, created_at: str, retention_class: str) -> str:
    if retention_class not in RETENTION_DAYS_BY_CLASS:
        raise StorageRetentionRepositoryError(
            reason_code="retention_policy_violation",
            message="Retention class is not governed.",
            context={"retention_class": retention_class},
        )
    created_at_dt = _parse_datetime(created_at)
    retention_days = RETENTION_DAYS_BY_CLASS[retention_class]
    return (created_at_dt + timedelta(days=retention_days)).isoformat()


def cleanup_reference_time() -> datetime:
    raw_value = os.getenv("STORAGE_REFERENCE_TIME", "").strip()
    if raw_value == "":
        return get_capability_base_time()
    return _parse_datetime(raw_value)


def run_retention_cleanup_hook(
    *,
    repository: StorageRetentionRepository,
    limit: int,
    reference_time: datetime,
) -> dict[str, object]:
    normalized_limit = limit if limit > 0 else 100
    records = repository.list_records()
    ordered_records = sorted(records, key=lambda record: record.object_key)
    processed_items: list[dict[str, object]] = []
    skipped_items: list[dict[str, object]] = []
    failed_items: list[dict[str, object]] = []
    processed_count = 0
    for record in ordered_records:
        if processed_count >= normalized_limit:
            skipped_items.append(
                {
                    "object_key": record.object_key,
                    "reason_code": CLEANUP_NOT_ELIGIBLE,
                }
            )
            continue
        try:
            cleaned = repository.mark_cleanup_pending(
                object_key=record.object_key,
                reference_time=reference_time,
            )
            processed_items.append(
                {"object_key": cleaned.object_key, "cleanup_status": cleaned.cleanup_status}
            )
            processed_count += 1
        except StorageRetentionRepositoryError as error:
            target = skipped_items if error.reason_code == CLEANUP_NOT_ELIGIBLE else failed_items
            target.append(
                {
                    "object_key": record.object_key,
                    "reason_code": error.reason_code,
                }
            )

    return {
        "processed": len(processed_items),
        "skipped": len(skipped_items),
        "failed": len(failed_items),
        "processed_items": processed_items,
        "skipped_items": skipped_items,
        "failed_items": failed_items,
    }


def cleanup_one_record(
    *,
    repository: StorageRetentionRepository,
    object_key: str,
    reference_time: datetime,
) -> StorageRetentionRecord:
    return repository.mark_cleanup_pending(object_key=object_key, reference_time=reference_time)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
