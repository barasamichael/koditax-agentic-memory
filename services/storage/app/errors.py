"""Canonical storage runtime error helpers."""

from __future__ import annotations

from typing import Final
from typing import TypedDict
from typing import NotRequired
import hashlib

from fastapi import Request
from fastapi import HTTPException

from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id

INVALID_STORAGE_REQUEST: Final[str] = "invalid_storage_request"
UNSUPPORTED_STORAGE_SCOPE: Final[str] = "unsupported_storage_scope"
STORAGE_CAPABILITY_EXPIRED: Final[str] = "storage_capability_expired"
STORAGE_CAPABILITY_NOT_FOUND: Final[str] = "storage_capability_not_found"
RETENTION_POLICY_VIOLATION: Final[str] = "retention_policy_violation"
CLEANUP_NOT_ELIGIBLE: Final[str] = "cleanup_not_eligible"
STORAGE_CLEANUP_FAILED: Final[str] = "storage_cleanup_failed"
STORAGE_CONTRACT_VIOLATION: Final[str] = "storage_contract_violation"

STORAGE_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        INVALID_STORAGE_REQUEST,
        UNSUPPORTED_STORAGE_SCOPE,
        STORAGE_CAPABILITY_EXPIRED,
        STORAGE_CAPABILITY_NOT_FOUND,
        RETENTION_POLICY_VIOLATION,
        CLEANUP_NOT_ELIGIBLE,
        STORAGE_CLEANUP_FAILED,
        STORAGE_CONTRACT_VIOLATION,
    }
)


class StorageErrorEnvelope(TypedDict):
    """Represent deterministic storage runtime error envelope."""

    error_code: str
    message: str
    reason: str
    reason_code: str
    trace_id: NotRequired[str]
    correlation_id: NotRequired[str]


def build_storage_error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
) -> StorageErrorEnvelope:
    """Build canonical deterministic storage error envelope."""

    normalized_reason = _normalize_reason(reason)
    normalized_error_code = error_code.strip() or normalized_reason
    normalized_message = message.strip() or "Storage request failed."

    return {
        "error_code": normalized_error_code,
        "message": normalized_message,
        "reason": normalized_reason,
        "reason_code": normalized_reason,
        "trace_id": _hash_to_hex64(get_trace_id(request)),
        "correlation_id": _hash_to_hex64(get_correlation_id(request)),
    }


def create_storage_http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
) -> HTTPException:
    """Create deterministic HTTPException containing canonical error detail."""

    envelope = build_storage_error_envelope(
        request=request,
        error_code=error_code,
        message=message,
        reason=reason,
    )
    return HTTPException(status_code=status_code, detail=envelope)


def _normalize_reason(value: str) -> str:
    candidate = value.strip()
    if candidate in STORAGE_REASON_CODES:
        return candidate
    return STORAGE_CONTRACT_VIOLATION


def _hash_to_hex64(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
