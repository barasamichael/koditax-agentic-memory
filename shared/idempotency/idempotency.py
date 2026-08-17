"""Validate and parse idempotency key headers."""

from __future__ import annotations

from fastapi import Header
from fastapi import Request

from shared.errors import codes
from shared.errors.envelope import create_request_http_error

IDEMPOTENCY_HEADER_NAME = "Idempotency-Key"
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class InvalidIdempotencyKeyError(ValueError):
    """Represent an invalid idempotency key value.

    :param error_code: Stable machine-readable error code.
    :param message: Human-readable error description.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def validate_idempotency_key(
    idempotency_key: str,
    max_length: int = MAX_IDEMPOTENCY_KEY_LENGTH,
) -> str:
    """Validate and normalize an idempotency key value.

    :param idempotency_key: Raw idempotency key value.
    :param max_length: Maximum allowed key length.
    :return: Normalized idempotency key.
    :raises InvalidIdempotencyKeyError: If key is empty or too long.
    """

    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise InvalidIdempotencyKeyError(
            error_code=codes.INVALID_IDEMPOTENCY_KEY,
            message="Idempotency-Key must not be empty or whitespace.",
        )

    if len(normalized_key) > max_length:
        raise InvalidIdempotencyKeyError(
            error_code=codes.INVALID_IDEMPOTENCY_KEY,
            message=f"Idempotency-Key must be at most {max_length} characters.",
        )

    return normalized_key


def parse_idempotency_key_header(idempotency_key: str | None) -> str:
    """Parse and validate the Idempotency-Key header.

    :param idempotency_key: Raw Idempotency-Key header value.
    :return: Normalized idempotency key.
    :raises InvalidIdempotencyKeyError: If header is missing or invalid.
    """

    if idempotency_key is None:
        raise InvalidIdempotencyKeyError(
            error_code=codes.MISSING_IDEMPOTENCY_KEY,
            message="Idempotency-Key header is required.",
        )

    return validate_idempotency_key(idempotency_key)


def require_idempotency_key(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER_NAME),
) -> str:
    """FastAPI dependency enforcing a valid Idempotency-Key header.

    :param request: Active HTTP request.
    :param idempotency_key: Raw Idempotency-Key header value.
    :return: Normalized idempotency key.
    :raises HTTPException: If header is missing or invalid.
    """

    try:
        return parse_idempotency_key_header(idempotency_key)
    except InvalidIdempotencyKeyError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=error.error_code,
            message=str(error),
            details={"header": IDEMPOTENCY_HEADER_NAME},
        ) from error
