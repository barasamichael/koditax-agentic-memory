"""Canonical orchestration error taxonomy and envelope helpers."""

from __future__ import annotations

from typing import TypedDict


class OrchestrationErrorEnvelope(TypedDict):
    """Represent one canonical orchestration runtime error envelope."""

    error_code: str
    message: str
    reason: str
    reason_code: str
    correlation_id: str
    trace_id: str
    context: dict[str, object] | None


class OrchestrationRuntimeError(RuntimeError):
    """Represent one typed fail-closed orchestration runtime error."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
        reason_code: str | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.reason_code = reason if reason_code is None else reason_code
        self.context = context


def build_orchestration_error_envelope(
    *,
    correlation_id: str,
    trace_id: str,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> OrchestrationErrorEnvelope:
    """Build one canonical orchestration error envelope."""

    return {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "reason_code": reason_code,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "context": context,
    }
