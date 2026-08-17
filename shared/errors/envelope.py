"""Build a standard API error envelope and HTTP exception helpers."""

from __future__ import annotations

from typing import TypedDict
from typing import NotRequired

from fastapi import Request
from fastapi import HTTPException

from shared.tracing.correlation import get_correlation_id


class ErrorEnvelope(TypedDict):
    """Represent a standard API error payload.

    :param error_code: Stable machine-readable error code.
    :param message: Human-readable error description.
    :param correlation_id: Correlation identifier for traceability.
    :param details: Optional structured metadata for the error.
    """

    error_code: str
    message: str
    correlation_id: str
    details: NotRequired[dict[str, object]]


def build_error_envelope(
    error_code: str,
    message: str,
    correlation_id: str,
    details: dict[str, object] | None = None,
) -> ErrorEnvelope:
    """Build a standard error envelope payload.

    :param error_code: Stable machine-readable error code.
    :param message: Human-readable error description.
    :param correlation_id: Correlation identifier for traceability.
    :param details: Optional structured metadata for the error.
    :return: Error payload dictionary.
    """

    envelope: ErrorEnvelope = {
        "error_code": error_code,
        "message": message,
        "correlation_id": correlation_id,
    }
    if details is not None:
        envelope["details"] = details
    return envelope


def create_http_error(
    status_code: int,
    error_code: str,
    message: str,
    correlation_id: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    """Create an HTTPException containing the standard error envelope.

    :param status_code: HTTP status code.
    :param error_code: Stable machine-readable error code.
    :param message: Human-readable error description.
    :param correlation_id: Correlation identifier for traceability.
    :param details: Optional structured metadata for the error.
    :return: Configured HTTPException.
    """

    return HTTPException(
        status_code=status_code,
        detail=build_error_envelope(
            error_code=error_code,
            message=message,
            correlation_id=correlation_id,
            details=details,
        ),
    )


def create_request_http_error(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    """Create an HTTPException from a request using its correlation ID.

    :param request: Active request.
    :param status_code: HTTP status code.
    :param error_code: Stable machine-readable error code.
    :param message: Human-readable error description.
    :param details: Optional structured metadata for the error.
    :return: Configured HTTPException.
    """

    return create_http_error(
        status_code=status_code,
        error_code=error_code,
        message=message,
        correlation_id=get_correlation_id(request),
        details=details,
    )
