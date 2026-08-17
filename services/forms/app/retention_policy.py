"""Deterministic retention-policy metadata and expiry enforcement for forms artifacts."""

from __future__ import annotations

from typing import TypedDict
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping

DEFAULT_RETENTION_POLICY_ID = "forms_retention_policy_v1"
DEFAULT_ARTIFACT_RETENTION_TTL_SECONDS = 31536000
RETENTION_STATUS_ACTIVE = "active"
RETENTION_STATUS_EXPIRED = "expired"
RETENTION_STATUS_RESTRICTED = "restricted"
ALLOWED_RETENTION_STATUSES = frozenset(
    {RETENTION_STATUS_ACTIVE, RETENTION_STATUS_EXPIRED, RETENTION_STATUS_RESTRICTED}
)
_now_override: datetime | None = None


class FormsRetentionMetadata(TypedDict):
    """Represent canonical forms-retention metadata persisted per artifact."""

    retention_policy_id: str
    retention_expires_at: str
    download_expires_at: str | None
    retention_status: str


class FormsRetentionPolicyError(RuntimeError):
    """Represent deterministic retention-policy failures."""

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
        """Return stable structured retention-policy details."""

        return {"reason": self.reason, **self._details}


def build_forms_artifact_retention_metadata(
    *,
    created_at: str,
    retention_policy_id: str = DEFAULT_RETENTION_POLICY_ID,
    retention_ttl_seconds: int = DEFAULT_ARTIFACT_RETENTION_TTL_SECONDS,
) -> FormsRetentionMetadata:
    """Build deterministic retention metadata for one generated artifact."""

    normalized_policy_id = retention_policy_id.strip()
    if not normalized_policy_id:
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={"field": "retention_policy_id", "constraint": "non_empty_string"},
        )
    if retention_ttl_seconds <= 0:
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={"field": "retention_ttl_seconds", "constraint": "positive_integer"},
        )
    created_at_dt = _parse_timestamp(created_at, field_name="created_at")
    retention_expires_at = (created_at_dt + timedelta(seconds=retention_ttl_seconds)).isoformat()
    return {
        "retention_policy_id": normalized_policy_id,
        "retention_expires_at": retention_expires_at,
        "download_expires_at": None,
        "retention_status": RETENTION_STATUS_ACTIVE,
    }


def evaluate_forms_artifact_retention_access(
    *,
    retention_metadata: Mapping[str, object],
    now: datetime | None = None,
) -> FormsRetentionMetadata:
    """Validate artifact-retention access against canonical metadata and policy."""

    normalized_metadata = normalize_forms_retention_metadata(retention_metadata)
    reference_time = get_forms_retention_reference_time(now=now)
    retention_status = normalized_metadata["retention_status"]
    if retention_status == RETENTION_STATUS_RESTRICTED:
        raise FormsRetentionPolicyError(
            reason="forms_artifact_access_restricted",
            message="Forms artifact access is restricted by retention policy.",
        )
    if retention_status == RETENTION_STATUS_EXPIRED:
        raise FormsRetentionPolicyError(
            reason="forms_artifact_retention_expired",
            message="Forms artifact retention policy has expired.",
        )

    retention_expires_at = _parse_timestamp(
        normalized_metadata["retention_expires_at"],
        field_name="retention_expires_at",
    )
    if reference_time >= retention_expires_at:
        raise FormsRetentionPolicyError(
            reason="forms_artifact_retention_expired",
            message="Forms artifact retention policy has expired.",
        )
    return normalized_metadata


def evaluate_forms_download_access(
    *,
    retention_metadata: Mapping[str, object],
    now: datetime | None = None,
) -> FormsRetentionMetadata:
    """Validate download-access expiry for one artifact retention context."""

    normalized_metadata = evaluate_forms_artifact_retention_access(
        retention_metadata=retention_metadata,
        now=now,
    )
    reference_time = get_forms_retention_reference_time(now=now)
    download_expires_at = normalized_metadata["download_expires_at"]
    if isinstance(download_expires_at, str):
        download_expires_at_dt = _parse_timestamp(
            download_expires_at,
            field_name="download_expires_at",
        )
        if reference_time >= download_expires_at_dt:
            raise FormsRetentionPolicyError(
                reason="forms_download_link_expired",
                message="Forms download link has expired.",
            )
    return normalized_metadata


def normalize_forms_retention_metadata(
    retention_metadata: Mapping[str, object],
) -> FormsRetentionMetadata:
    """Normalize and validate retention metadata shape deterministically."""

    policy_id_value = retention_metadata.get("retention_policy_id")
    if not isinstance(policy_id_value, str) or not policy_id_value.strip():
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={"field": "retention_policy_id", "constraint": "non_empty_string"},
        )

    retention_expires_at_value = retention_metadata.get("retention_expires_at")
    if not isinstance(retention_expires_at_value, str) or not retention_expires_at_value.strip():
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={"field": "retention_expires_at", "constraint": "date_time_string"},
        )
    _parse_timestamp(retention_expires_at_value, field_name="retention_expires_at")

    download_expires_at_value = retention_metadata.get("download_expires_at")
    if download_expires_at_value is None:
        normalized_download_expires_at = None
    elif isinstance(download_expires_at_value, str) and download_expires_at_value.strip():
        _parse_timestamp(download_expires_at_value, field_name="download_expires_at")
        normalized_download_expires_at = download_expires_at_value
    else:
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={"field": "download_expires_at", "constraint": "date_time_or_null"},
        )

    retention_status_value = retention_metadata.get("retention_status")
    if (
        not isinstance(retention_status_value, str)
        or retention_status_value not in ALLOWED_RETENTION_STATUSES
    ):
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={
                "field": "retention_status",
                "constraint": "active_or_expired_or_restricted",
            },
        )
    return {
        "retention_policy_id": policy_id_value,
        "retention_expires_at": retention_expires_at_value,
        "download_expires_at": normalized_download_expires_at,
        "retention_status": retention_status_value,
    }


def get_forms_retention_reference_time(*, now: datetime | None = None) -> datetime:
    """Return deterministic retention-policy reference time in UTC seconds resolution."""

    if now is not None:
        return _normalize_datetime(now)
    if _now_override is not None:
        return _normalize_datetime(_now_override)
    return _normalize_datetime(datetime.now(UTC))


def set_forms_retention_policy_now_override(now: datetime | None) -> None:
    """Set deterministic clock override for retention-policy tests."""

    global _now_override
    _now_override = _normalize_datetime(now) if now is not None else None


def reset_forms_retention_policy_now_override() -> None:
    """Reset retention-policy deterministic clock override."""

    set_forms_retention_policy_now_override(None)


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    raw_value = value.strip()
    normalized_value = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise FormsRetentionPolicyError(
            reason="forms_request_invalid",
            message="Forms request payload is invalid.",
            details={"field": field_name, "constraint": "date_time_string"},
        ) from error
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).replace(microsecond=0)
