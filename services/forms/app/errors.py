"""Canonical error helpers for forms service runtime boundaries."""

from __future__ import annotations

from typing import Final
from typing import TypedDict
from typing import NotRequired

from fastapi import Request
from fastapi import HTTPException

from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id


class FormsErrorEnvelope(TypedDict):
    """Represent deterministic forms-service error envelope payload."""

    error_code: str
    message: str
    reason: str
    trace_id: str
    correlation_id: str
    details: NotRequired[dict[str, object]]


FORMS_OPERATION_NOT_IMPLEMENTED: Final[str] = "forms_operation_not_implemented"
FORMS_SCOPE_NOT_SUPPORTED: Final[str] = "forms_scope_not_supported"
FORMS_REQUEST_INVALID: Final[str] = "forms_request_invalid"
FORMS_CONTRACT_VIOLATION: Final[str] = "forms_contract_violation"
FORMS_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        FORMS_OPERATION_NOT_IMPLEMENTED,
        FORMS_SCOPE_NOT_SUPPORTED,
        FORMS_REQUEST_INVALID,
        FORMS_CONTRACT_VIOLATION,
    }
)


def build_forms_error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
    details: dict[str, object] | None = None,
) -> FormsErrorEnvelope:
    """Build deterministic forms-service error envelope payload."""

    normalized_error_code = error_code.strip()
    normalized_message = message.strip()
    normalized_reason = reason.strip()
    if not normalized_error_code:
        normalized_error_code = FORMS_CONTRACT_VIOLATION
    if not normalized_message:
        normalized_message = "Forms request failed."
    if not normalized_reason:
        normalized_reason = FORMS_CONTRACT_VIOLATION

    envelope: FormsErrorEnvelope = {
        "error_code": normalized_error_code,
        "message": normalized_message,
        "reason": normalized_reason,
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }
    if details is not None:
        envelope["details"] = details
    return envelope


def create_forms_http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    """Create deterministic HTTPException with canonical forms error envelope."""

    envelope = build_forms_error_envelope(
        request=request,
        error_code=error_code,
        message=message,
        reason=reason,
        details=details,
    )
    return HTTPException(status_code=status_code, detail=envelope)
