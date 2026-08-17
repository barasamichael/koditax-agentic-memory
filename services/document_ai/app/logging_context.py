"""Structured logging utilities for document_ai traceable operation logs."""

from __future__ import annotations

from uuid import UUID
from typing import Literal
from typing import TypedDict
from typing import cast
import logging
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.document_ai.app.redaction import redact_sensitive_fields

LOGGER = logging.getLogger("document_ai.structured")


class StructuredDocumentLogEvent(TypedDict):
    """Represent canonical structured log envelope for document_ai flows."""

    event_name: str
    action: str
    status: str
    trace_id: str
    correlation_id: str
    document_id: str | None
    reason_code: str | None
    payload: dict[str, object]


class InMemoryStructuredLogStore:
    """Collect structured log events in deterministic in-memory storage."""

    def __init__(self) -> None:
        self._events: list[StructuredDocumentLogEvent] = []

    def append(self, event: StructuredDocumentLogEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> tuple[StructuredDocumentLogEvent, ...]:
        return tuple(self._events)

    def clear(self) -> None:
        self._events.clear()


_DEFAULT_STRUCTURED_LOG_STORE = InMemoryStructuredLogStore()


def get_default_structured_log_store() -> InMemoryStructuredLogStore:
    """Return default deterministic structured log store."""

    return _DEFAULT_STRUCTURED_LOG_STORE


def reset_default_structured_log_store() -> None:
    """Reset default deterministic structured log store for tests."""

    _DEFAULT_STRUCTURED_LOG_STORE.clear()


def emit_document_structured_log(
    *,
    event_name: str,
    action: str,
    status: str,
    trace_id: str,
    correlation_id: str,
    document_id: UUID | str | None = None,
    reason_code: str | None = None,
    payload: Mapping[str, object] | None = None,
    structured_log_store: InMemoryStructuredLogStore | None = None,
) -> None:
    """Emit one structured document log with deterministic redacted payload shape."""

    resolved_store = (
        get_default_structured_log_store() if structured_log_store is None else structured_log_store
    )
    event = StructuredDocumentLogEvent(
        event_name=event_name,
        action=action,
        status=status,
        trace_id=trace_id,
        correlation_id=correlation_id,
        document_id=str(document_id) if document_id is not None else None,
        reason_code=reason_code,
        payload=_normalize_payload(payload=payload),
    )
    try:
        resolved_store.append(event)
    except Exception:
        return
    try:
        LOGGER.info(canonical_json_dumps(event))
    except Exception:
        return


def emit_document_dead_letter_log(
    *,
    dead_letter_id: str,
    extraction_job_id: UUID,
    document_id: UUID,
    failure_class: Literal["retry_exhausted", "non_retryable_failure"],
    reason_code: str,
    attempt_count: int,
    trace_id: str,
    correlation_id: str,
    audit_evidence_id: str,
    structured_log_store: InMemoryStructuredLogStore | None = None,
) -> None:
    """Emit canonical structured log event for extraction dead-letter placement."""

    emit_document_structured_log(
        event_name="document_extraction_job_dead_lettered",
        action="dead_letter_enqueue",
        status=failure_class,
        trace_id=trace_id,
        correlation_id=correlation_id,
        document_id=document_id,
        reason_code=reason_code,
        payload={
            "dead_letter_id": dead_letter_id,
            "extraction_job_id": str(extraction_job_id),
            "attempt_count": attempt_count,
            "audit_evidence_id": audit_evidence_id,
        },
        structured_log_store=structured_log_store,
    )


def _normalize_payload(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {}
    redacted = redact_sensitive_fields(dict(payload))
    if not isinstance(redacted, dict):
        return {}
    redacted_map = cast(dict[str, object], redacted)
    normalized: dict[str, object] = {}
    for key in sorted(redacted_map):
        normalized[key] = redacted_map[key]
    return normalized
