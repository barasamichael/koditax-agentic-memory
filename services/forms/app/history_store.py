"""Append-safe deterministic history persistence for generated form artifacts."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from threading import Lock
from collections.abc import Mapping

from services.forms.app.retention_policy import FormsRetentionPolicyError
from services.forms.app.retention_policy import normalize_forms_retention_metadata


class FormArtifactHistoryRecord(TypedDict):
    """Represent one immutable persisted form-artifact history record."""

    user_id: str
    artifact_id: str
    form_type: str
    form_version_id: str
    tax_year: int
    historical_version_id: str | None
    lineage_reference: dict[str, object]
    artifact_hash: str
    created_at: str
    status: str
    pre_population_source_fields: dict[str, object]


class FormArtifactRetentionMetadata(TypedDict):
    """Represent persisted forms-retention metadata for one artifact identity."""

    retention_policy_id: str
    retention_expires_at: str
    download_expires_at: str | None
    retention_status: str


class FormsHistoryStoreError(RuntimeError):
    """Represent deterministic forms history-store persistence failures."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured persistence error details."""

        return {"reason": self.reason, **self._details}


_STORE_LOCK = Lock()
_HISTORY_RECORDS_BY_ARTIFACT_ID: dict[str, FormArtifactHistoryRecord] = {}
_STORAGE_METADATA_BY_ARTIFACT_ID: dict[str, dict[str, object]] = {}
_RETENTION_METADATA_BY_ARTIFACT_ID: dict[str, FormArtifactRetentionMetadata] = {}
_failure_mode_enabled = False
_failure_mode_reason = "store_unavailable"


def persist_form_artifact_history_record(
    record: Mapping[str, object],
) -> FormArtifactHistoryRecord:
    """Persist one form-artifact history record with append-safe semantics."""

    normalized_record = _normalize_form_artifact_history_record(record)
    normalized_storage_metadata = _extract_optional_storage_metadata(
        record,
        artifact_hash=normalized_record["artifact_hash"],
    )
    normalized_retention_metadata = _extract_required_retention_metadata(record)
    with _STORE_LOCK:
        if _failure_mode_enabled:
            raise FormsHistoryStoreError(
                reason="forms_history_persistence_failed",
                message="Forms history persistence failed.",
                details={"failure_mode": _failure_mode_reason},
            )

        artifact_id = normalized_record["artifact_id"]
        existing_record = _HISTORY_RECORDS_BY_ARTIFACT_ID.get(artifact_id)
        if existing_record is not None:
            if (
                normalized_storage_metadata is not None
                and artifact_id not in _STORAGE_METADATA_BY_ARTIFACT_ID
            ):
                _STORAGE_METADATA_BY_ARTIFACT_ID[artifact_id] = normalized_storage_metadata
            if artifact_id not in _RETENTION_METADATA_BY_ARTIFACT_ID:
                _RETENTION_METADATA_BY_ARTIFACT_ID[artifact_id] = normalized_retention_metadata
            return _copy_record(existing_record)

        _HISTORY_RECORDS_BY_ARTIFACT_ID[artifact_id] = normalized_record
        if normalized_storage_metadata is not None:
            _STORAGE_METADATA_BY_ARTIFACT_ID[artifact_id] = normalized_storage_metadata
        _RETENTION_METADATA_BY_ARTIFACT_ID[artifact_id] = normalized_retention_metadata
        return _copy_record(normalized_record)


def list_form_artifact_history_records() -> list[FormArtifactHistoryRecord]:
    """List persisted history records in deterministic insertion order."""

    with _STORE_LOCK:
        return [_copy_record(record) for record in _HISTORY_RECORDS_BY_ARTIFACT_ID.values()]


def list_form_artifact_history_records_by_filter(
    *,
    user_id: str,
    tax_year: int,
    form_type: str,
) -> list[FormArtifactHistoryRecord]:
    """List history records by exact deterministic user/year/form filter."""

    with _STORE_LOCK:
        matching_records = [
            _copy_record(record)
            for record in _HISTORY_RECORDS_BY_ARTIFACT_ID.values()
            if record["user_id"] == user_id
            and record["tax_year"] == tax_year
            and record["form_type"] == form_type
        ]
    return sorted(
        matching_records,
        key=lambda record: (record["created_at"], record["artifact_id"]),
        reverse=True,
    )


def get_form_artifact_history_record_by_identity(
    *,
    artifact_id: str,
    form_version_id: str,
) -> FormArtifactHistoryRecord | None:
    """Get one history record by immutable artifact and version identity."""

    with _STORE_LOCK:
        record = _HISTORY_RECORDS_BY_ARTIFACT_ID.get(artifact_id)
        if record is None:
            return None
        if record["form_version_id"] != form_version_id:
            return None
        return _copy_record(record)


def get_form_artifact_storage_metadata(artifact_id: str) -> dict[str, object] | None:
    """Get deterministic storage metadata persisted for one artifact identity."""

    normalized_artifact_id = artifact_id.strip().lower()
    with _STORE_LOCK:
        persisted = _STORAGE_METADATA_BY_ARTIFACT_ID.get(normalized_artifact_id)
        if persisted is None:
            return None
        return _copy_storage_metadata(persisted)


def get_form_artifact_retention_metadata(artifact_id: str) -> FormArtifactRetentionMetadata | None:
    """Get deterministic retention metadata persisted for one artifact identity."""

    normalized_artifact_id = artifact_id.strip().lower()
    with _STORE_LOCK:
        persisted = _RETENTION_METADATA_BY_ARTIFACT_ID.get(normalized_artifact_id)
        if persisted is None:
            return None
        return _copy_retention_metadata(persisted)


def set_form_artifact_download_expiry(
    *,
    artifact_id: str,
    download_expires_at: str,
) -> FormArtifactRetentionMetadata:
    """Persist deterministic download-expiry metadata for one artifact identity."""

    normalized_artifact_id = artifact_id.strip().lower()
    normalized_download_expires_at = download_expires_at.strip()
    if not normalized_artifact_id:
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={"field": "artifact_id", "constraint": "non_empty_string"},
        )
    if not normalized_download_expires_at:
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={"field": "download_expires_at", "constraint": "date_time_string"},
        )
    with _STORE_LOCK:
        if normalized_artifact_id not in _HISTORY_RECORDS_BY_ARTIFACT_ID:
            raise FormsHistoryStoreError(
                reason="forms_contract_violation",
                message="Forms history record violates persistence contract.",
                details={"field": "artifact_id", "constraint": "must_exist_in_history"},
            )
        existing_retention_metadata = _RETENTION_METADATA_BY_ARTIFACT_ID.get(normalized_artifact_id)
        if existing_retention_metadata is None:
            raise FormsHistoryStoreError(
                reason="forms_contract_violation",
                message="Forms history record violates persistence contract.",
                details={
                    "field": "retention_metadata",
                    "constraint": "must_exist_for_artifact",
                },
            )
        updated_retention_metadata = _copy_retention_metadata(existing_retention_metadata)
        updated_retention_metadata["download_expires_at"] = normalized_download_expires_at
        _RETENTION_METADATA_BY_ARTIFACT_ID[normalized_artifact_id] = updated_retention_metadata
        return _copy_retention_metadata(updated_retention_metadata)


def reset_form_artifact_history_store() -> None:
    """Reset in-memory history store for deterministic test isolation."""

    with _STORE_LOCK:
        _HISTORY_RECORDS_BY_ARTIFACT_ID.clear()
        _STORAGE_METADATA_BY_ARTIFACT_ID.clear()
        _RETENTION_METADATA_BY_ARTIFACT_ID.clear()


def set_form_artifact_history_store_failure_mode(
    *,
    enabled: bool,
    reason: str = "store_unavailable",
) -> None:
    """Enable deterministic failure mode for persistence tests."""

    normalized_reason = reason.strip() or "store_unavailable"
    global _failure_mode_enabled
    global _failure_mode_reason
    with _STORE_LOCK:
        _failure_mode_enabled = enabled
        _failure_mode_reason = normalized_reason


def _normalize_form_artifact_history_record(
    record: Mapping[str, object],
) -> FormArtifactHistoryRecord:
    source = _as_object(record)
    user_id = _required_string(source, "user_id")
    artifact_id = _required_string(source, "artifact_id").lower()
    form_type = _required_string(source, "form_type")
    form_version_id = _required_string(source, "form_version_id")
    tax_year = _required_int(source, "tax_year")
    artifact_hash = _required_string(source, "artifact_hash").lower()
    created_at = _required_string(source, "created_at")
    status = _required_string(source, "status")
    lineage_reference = _required_object(source, "lineage_reference")
    pre_population_source_fields = _optional_object(source, "pre_population_source_fields")

    historical_version_id_value = source.get("historical_version_id")
    historical_version_id: str | None
    if historical_version_id_value is None:
        historical_version_id = None
    elif isinstance(historical_version_id_value, str) and historical_version_id_value.strip():
        historical_version_id = historical_version_id_value
    else:
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={"field": "historical_version_id", "constraint": "string_or_null"},
        )

    if status not in {"current", "superseded"}:
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={"field": "status", "constraint": "current_or_superseded"},
        )

    return {
        "user_id": user_id,
        "artifact_id": artifact_id,
        "form_type": form_type,
        "form_version_id": form_version_id,
        "tax_year": tax_year,
        "historical_version_id": historical_version_id,
        "lineage_reference": lineage_reference,
        "artifact_hash": artifact_hash,
        "created_at": created_at,
        "status": status,
        "pre_population_source_fields": pre_population_source_fields,
    }


def _extract_optional_storage_metadata(
    source_record: Mapping[str, object],
    *,
    artifact_hash: str,
) -> dict[str, object] | None:
    source = _as_object(source_record)
    raw_storage_metadata = source.get("storage_metadata")
    if raw_storage_metadata is None:
        return None
    if not isinstance(raw_storage_metadata, Mapping):
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={"field": "storage_metadata", "constraint": "object"},
        )
    storage_source = _as_object(cast(Mapping[str, object], raw_storage_metadata))
    storage_object_id = _required_string(storage_source, "storage_object_id")
    storage_backend = _required_string(storage_source, "storage_backend")
    content_type = _required_string(storage_source, "content_type")
    size_bytes = _required_non_negative_int(storage_source, "size_bytes")
    storage_artifact_hash = _required_string(storage_source, "artifact_hash").lower()
    if storage_artifact_hash != artifact_hash:
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={
                "field": "storage_metadata.artifact_hash",
                "constraint": "must_match_artifact_hash",
            },
        )
    return {
        "storage_object_id": storage_object_id,
        "storage_backend": storage_backend,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "artifact_hash": storage_artifact_hash,
    }


def _extract_required_retention_metadata(
    source_record: Mapping[str, object],
) -> FormArtifactRetentionMetadata:
    source = _as_object(source_record)
    raw_retention_metadata = source.get("retention_metadata")
    if not isinstance(raw_retention_metadata, Mapping):
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={"field": "retention_metadata", "constraint": "object"},
        )
    try:
        normalized = normalize_forms_retention_metadata(
            cast(Mapping[str, object], raw_retention_metadata)
        )
    except FormsRetentionPolicyError as error:
        raise FormsHistoryStoreError(
            reason="forms_contract_violation",
            message="Forms history record violates persistence contract.",
            details={
                "field": "retention_metadata",
                "constraint": "valid_forms_retention_metadata",
                "upstream_reason": error.reason,
            },
        ) from error
    return {
        "retention_policy_id": normalized["retention_policy_id"],
        "retention_expires_at": normalized["retention_expires_at"],
        "download_expires_at": normalized["download_expires_at"],
        "retention_status": normalized["retention_status"],
    }


def _as_object(value: Mapping[str, object]) -> dict[str, object]:
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _required_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    raise FormsHistoryStoreError(
        reason="forms_contract_violation",
        message="Forms history record violates persistence contract.",
        details={"field": field_name, "constraint": "non_empty_string"},
    )


def _required_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if isinstance(value, int):
        return value
    raise FormsHistoryStoreError(
        reason="forms_contract_violation",
        message="Forms history record violates persistence contract.",
        details={"field": field_name, "constraint": "integer"},
    )


def _required_non_negative_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if isinstance(value, int) and value >= 0:
        return value
    raise FormsHistoryStoreError(
        reason="forms_contract_violation",
        message="Forms history record violates persistence contract.",
        details={"field": field_name, "constraint": "non_negative_integer"},
    )


def _required_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if isinstance(value, Mapping):
        return _as_object(cast(Mapping[str, object], value))
    raise FormsHistoryStoreError(
        reason="forms_contract_violation",
        message="Forms history record violates persistence contract.",
        details={"field": field_name, "constraint": "object"},
    )


def _optional_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return _as_object(cast(Mapping[str, object], value))
    raise FormsHistoryStoreError(
        reason="forms_contract_violation",
        message="Forms history record violates persistence contract.",
        details={"field": field_name, "constraint": "object_or_null"},
    )


def _copy_record(record: FormArtifactHistoryRecord) -> FormArtifactHistoryRecord:
    return {
        "user_id": record["user_id"],
        "artifact_id": record["artifact_id"],
        "form_type": record["form_type"],
        "form_version_id": record["form_version_id"],
        "tax_year": record["tax_year"],
        "historical_version_id": record["historical_version_id"],
        "lineage_reference": dict(record["lineage_reference"]),
        "artifact_hash": record["artifact_hash"],
        "created_at": record["created_at"],
        "status": record["status"],
        "pre_population_source_fields": dict(record["pre_population_source_fields"]),
    }


def _copy_storage_metadata(storage_metadata: Mapping[str, object]) -> dict[str, object]:
    return {
        "storage_object_id": storage_metadata["storage_object_id"],
        "storage_backend": storage_metadata["storage_backend"],
        "content_type": storage_metadata["content_type"],
        "size_bytes": storage_metadata["size_bytes"],
        "artifact_hash": storage_metadata["artifact_hash"],
    }


def _copy_retention_metadata(
    retention_metadata: FormArtifactRetentionMetadata,
) -> FormArtifactRetentionMetadata:
    return {
        "retention_policy_id": retention_metadata["retention_policy_id"],
        "retention_expires_at": retention_metadata["retention_expires_at"],
        "download_expires_at": retention_metadata["download_expires_at"],
        "retention_status": retention_metadata["retention_status"],
    }
