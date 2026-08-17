"""Implement correlation ID middleware and helpers."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.base import RequestResponseEndpoint

CORRELATION_ID_HEADER_NAME = "X-Correlation-ID"
TRACE_ID_HEADER_NAME = "X-Trace-ID"
_CORRELATION_STATE_FIELD = "correlation_id"
_TRACE_ID_STATE_FIELD = "trace_id"
_TRACE_CONTEXT_REASON_STATE_FIELD = "trace_context_reason"
_TRACE_CONTEXT_REASON_MISSING = "trace_context_missing"
_TRACE_CONTEXT_REASON_INVALID = "trace_context_invalid"
_TRACE_CONTEXT_REASON_OK = "trace_context_ok"
_VALID_CONTEXT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Propagate a correlation ID across request and response.

    :param app: Wrapped ASGI application.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Attach and propagate the correlation ID for one request.

        :param request: Incoming HTTP request.
        :param call_next: Next request handler callable.
        :return: Outgoing HTTP response.
        """

        correlation_id, trace_id, trace_context_reason = _resolve_trace_correlation_context(
            correlation_raw=request.headers.get(CORRELATION_ID_HEADER_NAME),
            trace_raw=request.headers.get(TRACE_ID_HEADER_NAME),
        )
        setattr(request.state, _CORRELATION_STATE_FIELD, correlation_id)
        setattr(request.state, _TRACE_ID_STATE_FIELD, trace_id)
        setattr(request.state, _TRACE_CONTEXT_REASON_STATE_FIELD, trace_context_reason)

        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER_NAME] = correlation_id
        response.headers[TRACE_ID_HEADER_NAME] = trace_id
        return response


def get_correlation_id(request: Request) -> str:
    """Retrieve the correlation ID from request state or headers.

    :param request: Active HTTP request.
    :return: Correlation ID string.
    """

    state_value = getattr(request.state, _CORRELATION_STATE_FIELD, None)
    if isinstance(state_value, str) and state_value.strip():
        return state_value

    correlation_id, _, _ = _resolve_trace_correlation_context(
        correlation_raw=request.headers.get(CORRELATION_ID_HEADER_NAME),
        trace_raw=request.headers.get(TRACE_ID_HEADER_NAME),
    )
    return correlation_id


def get_trace_id(request: Request) -> str:
    """Retrieve the trace ID from request state or derive deterministically."""

    state_value = getattr(request.state, _TRACE_ID_STATE_FIELD, None)
    if isinstance(state_value, str) and state_value.strip():
        return state_value

    _, trace_id, _ = _resolve_trace_correlation_context(
        correlation_raw=request.headers.get(CORRELATION_ID_HEADER_NAME),
        trace_raw=request.headers.get(TRACE_ID_HEADER_NAME),
    )
    return trace_id


def get_trace_context_reason(request: Request) -> str:
    """Return deterministic classification of inbound trace context quality."""

    state_value = getattr(request.state, _TRACE_CONTEXT_REASON_STATE_FIELD, None)
    if isinstance(state_value, str) and state_value.strip():
        return state_value
    correlation_raw = request.headers.get(CORRELATION_ID_HEADER_NAME)
    trace_raw = request.headers.get(TRACE_ID_HEADER_NAME)
    _, _, reason = _resolve_trace_correlation_context(
        correlation_raw=correlation_raw,
        trace_raw=trace_raw,
    )
    return reason


def _resolve_trace_correlation_context(
    *,
    correlation_raw: str | None,
    trace_raw: str | None,
) -> tuple[str, str, str]:
    normalized_correlation = "" if correlation_raw is None else correlation_raw.strip()
    normalized_trace = "" if trace_raw is None else trace_raw.strip()

    correlation_valid = _is_valid_context_id(normalized_correlation)
    trace_valid = _is_valid_context_id(normalized_trace)

    if correlation_valid:
        correlation_id = normalized_correlation
        trace_context_reason = _TRACE_CONTEXT_REASON_OK
    else:
        correlation_id = str(uuid4())
        trace_context_reason = (
            _TRACE_CONTEXT_REASON_MISSING
            if not normalized_correlation
            else _TRACE_CONTEXT_REASON_INVALID
        )

    if trace_valid:
        trace_id = normalized_trace
    else:
        trace_id = correlation_id
        if normalized_trace and trace_context_reason == _TRACE_CONTEXT_REASON_OK:
            trace_context_reason = _TRACE_CONTEXT_REASON_INVALID

    return correlation_id, trace_id, trace_context_reason


def _is_valid_context_id(value: str) -> bool:
    if not value:
        return False
    return _VALID_CONTEXT_ID_PATTERN.fullmatch(value) is not None
