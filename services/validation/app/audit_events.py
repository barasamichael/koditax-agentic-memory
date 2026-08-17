"""Deterministic audit primitives for the validation service boundary."""

from __future__ import annotations

from typing import Protocol
from typing import TypedDict
import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.validation.app.validation_store import ValidationExecutionRecord
from services.validation.app.validation_outcomes import ValidationAuditEvidence

_AUDIT_TIME_BASE = datetime(2026, 1, 1, tzinfo=UTC)

VALIDATION_AUDIT_EVENT_EXECUTION_ACCEPTED = "validation_execution_accepted"
VALIDATION_AUDIT_EVENT_EXECUTION_REJECTED = "validation_execution_rejected"
VALIDATION_AUDIT_EVENT_REQUEST_REJECTED = "validation_request_rejected"
VALIDATION_AUDIT_EVENT_EXECUTION_FAILED = "validation_execution_failed"


class ValidationAuditEvent(TypedDict):
    """Represent one canonical validation audit event."""

    event_id: str
    event_type: str
    event_time: str
    correlation_id: str
    trace_id: str
    status: str
    return_id: str | None
    tax_domain: str | None
    mode: str | None
    validation_id: str | None
    error_code: str | None
    reason: str | None
    context: dict[str, object]


class ValidationAuditEventStore(Protocol):
    """Describe deterministic storage for validation audit events."""

    def append(self, event: ValidationAuditEvent) -> ValidationAuditEvent:
        """Persist one audit event append-only and return the stored payload."""
        ...

    def list(self, *, correlation_id: str | None = None) -> list[ValidationAuditEvent]:
        """Return stored audit events, optionally filtered by correlation id."""
        ...

    def clear(self) -> None:
        """Reset audit events for deterministic test isolation."""
        ...


class InMemoryValidationAuditEventStore:
    """Provide deterministic append-only in-memory validation audit storage."""

    def __init__(self) -> None:
        self._events: dict[str, ValidationAuditEvent] = {}

    def append(self, event: ValidationAuditEvent) -> ValidationAuditEvent:
        self._events.setdefault(event["event_id"], event)
        return self._events[event["event_id"]]

    def list(self, *, correlation_id: str | None = None) -> list[ValidationAuditEvent]:
        events = list(self._events.values())
        if correlation_id is not None:
            events = [event for event in events if event["correlation_id"] == correlation_id]
        return sorted(
            events,
            key=lambda item: (
                item["event_time"],
                item["event_type"],
                item["event_id"],
            ),
        )

    def clear(self) -> None:
        self._events.clear()


def build_default_validation_audit_event_store() -> ValidationAuditEventStore:
    """Build the default validation audit store."""

    return InMemoryValidationAuditEventStore()


def build_validation_execution_audit_event(
    *,
    record: ValidationExecutionRecord,
) -> ValidationAuditEvent:
    """Build a deterministic audit event for one stored validation execution."""

    event_type = (
        VALIDATION_AUDIT_EVENT_EXECUTION_ACCEPTED
        if record.validation_status == "accepted"
        else VALIDATION_AUDIT_EVENT_EXECUTION_REJECTED
    )
    return _build_validation_audit_event(
        event_type=event_type,
        correlation_id=record.correlation_id,
        trace_id=record.trace_id,
        status=record.validation_status,
        return_id=record.return_id,
        tax_domain=record.tax_domain,
        mode=record.mode,
        validation_id=str(record.validation_id),
        error_code=None,
        reason=None,
        context={"request_fingerprint": record.request_fingerprint},
    )


def build_validation_failure_audit_event(
    *,
    correlation_id: str,
    trace_id: str,
    error_code: str,
    reason: str,
    return_id: str | None,
    tax_domain: str | None,
    mode: str | None,
    context: Mapping[str, object] | None = None,
) -> ValidationAuditEvent:
    """Build a deterministic audit event for one validation rejection/failure."""

    event_type = (
        VALIDATION_AUDIT_EVENT_EXECUTION_FAILED
        if error_code == "validation_persistence_unavailable"
        else VALIDATION_AUDIT_EVENT_REQUEST_REJECTED
    )
    return _build_validation_audit_event(
        event_type=event_type,
        correlation_id=correlation_id,
        trace_id=trace_id,
        status="failed",
        return_id=return_id,
        tax_domain=tax_domain,
        mode=mode,
        validation_id=None,
        error_code=error_code,
        reason=reason,
        context=dict(context or {}),
    )


def emit_validation_audit_event(
    *,
    store: ValidationAuditEventStore,
    event: ValidationAuditEvent,
) -> ValidationAuditEvent:
    """Append one deterministic validation audit event."""

    return store.append(event)


def build_validation_audit_evidence(
    event: ValidationAuditEvent,
) -> ValidationAuditEvidence:
    """Build a machine-consumable audit evidence envelope from one event."""

    return ValidationAuditEvidence(
        audit_event_id=event["event_id"],
        event_type=event["event_type"],
        event_time=event["event_time"],
        status=event["status"],
    )


def _build_validation_audit_event(
    *,
    event_type: str,
    correlation_id: str,
    trace_id: str,
    status: str,
    return_id: str | None,
    tax_domain: str | None,
    mode: str | None,
    validation_id: str | None,
    error_code: str | None,
    reason: str | None,
    context: dict[str, object],
) -> ValidationAuditEvent:
    seed_payload = {
        "event_type": event_type,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "status": status,
        "return_id": return_id,
        "tax_domain": tax_domain,
        "mode": mode,
        "validation_id": validation_id,
        "error_code": error_code,
        "reason": reason,
        "context": context,
    }
    event_id = hashlib.sha256(canonical_json_dumps(seed_payload).encode("utf-8")).hexdigest()
    event_time = _event_time_from_id(event_id=event_id)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "event_time": event_time,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "status": status,
        "return_id": return_id,
        "tax_domain": tax_domain,
        "mode": mode,
        "validation_id": validation_id,
        "error_code": error_code,
        "reason": reason,
        "context": context,
    }


def _event_time_from_id(*, event_id: str) -> str:
    offset_seconds = int(event_id[:8], 16) % (365 * 24 * 60 * 60)
    return (_AUDIT_TIME_BASE + timedelta(seconds=offset_seconds)).isoformat()
