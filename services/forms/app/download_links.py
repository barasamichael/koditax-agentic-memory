"""Deterministic download-link issuance helpers for forms artifacts."""

from __future__ import annotations

from hashlib import sha256
from secrets import token_hex
from datetime import UTC
from datetime import datetime
from datetime import timedelta

DEFAULT_DOWNLOAD_TTL_SECONDS = 900
MAX_DOWNLOAD_TTL_SECONDS = 86400


class FormsDownloadLinkIssuanceError(RuntimeError):
    """Represent deterministic download-link issuance failures."""

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
        """Return stable structured download-link issuance details."""

        return {"reason": self.reason, **self._details}


def issue_forms_artifact_download_token(
    *,
    artifact_id: str,
    form_version_id: str,
    owner_user_id: str,
    ttl_seconds: int = DEFAULT_DOWNLOAD_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, object]:
    """Issue deterministic-format, opaque download token with explicit expiry metadata."""

    normalized_artifact_id = artifact_id.strip().lower()
    normalized_form_version_id = form_version_id.strip()
    normalized_owner_user_id = owner_user_id.strip()

    if not normalized_artifact_id or not normalized_form_version_id or not normalized_owner_user_id:
        raise FormsDownloadLinkIssuanceError(
            reason="forms_download_link_issuance_failed",
            message="Forms download link issuance failed.",
            details={"constraint": "artifact_version_owner_required"},
        )
    if ttl_seconds <= 0 or ttl_seconds > MAX_DOWNLOAD_TTL_SECONDS:
        raise FormsDownloadLinkIssuanceError(
            reason="forms_download_link_issuance_failed",
            message="Forms download link issuance failed.",
            details={
                "field": "ttl_seconds",
                "constraint": f"between_1_and_{MAX_DOWNLOAD_TTL_SECONDS}",
            },
        )

    issued_at_dt = _normalize_now(now)
    expires_at_dt = issued_at_dt + timedelta(seconds=ttl_seconds)
    issued_at = issued_at_dt.isoformat()
    expires_at = expires_at_dt.isoformat()

    nonce = token_hex(16)
    token_seed = (
        f"forms-download-token:{normalized_artifact_id}:{normalized_form_version_id}:"
        f"{normalized_owner_user_id}:{issued_at}:{expires_at}:{nonce}"
    )
    download_token = sha256(token_seed.encode("utf-8")).hexdigest()

    audit_event_seed = (
        f"forms-download-issued:{normalized_artifact_id}:{normalized_form_version_id}:"
        f"{normalized_owner_user_id}:{issued_at}"
    )
    audit_event_id = sha256(audit_event_seed.encode("utf-8")).hexdigest()

    return {
        "artifact_id": normalized_artifact_id,
        "download_token": download_token,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "ttl_seconds": ttl_seconds,
        "audit_event_id": audit_event_id,
    }


def is_forms_download_token_expired(
    *,
    expires_at: str,
    now: datetime,
) -> bool:
    """Evaluate download-token expiry deterministically for one timestamp pair."""

    expires_at_dt = _parse_timestamp(expires_at)
    reference_now = _normalize_now(now)
    return reference_now >= expires_at_dt


def _normalize_now(now: datetime | None) -> datetime:
    reference = datetime.now(UTC) if now is None else now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return reference.astimezone(UTC).replace(microsecond=0)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise FormsDownloadLinkIssuanceError(
            reason="forms_download_link_issuance_failed",
            message="Forms download link issuance failed.",
            details={"field": "expires_at", "constraint": "date_time_string"},
        ) from error
    return _normalize_now(parsed)
