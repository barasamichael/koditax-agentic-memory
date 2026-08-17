"""Models for persistent event-store append and read paths."""

from __future__ import annotations

from uuid import UUID
from dataclasses import dataclass


@dataclass(frozen=True)
class PersistedAuditEvent:
    """Represent one persisted event-store audit record."""

    event_id: UUID
    event_type: str
    action_type: str
    user_id: UUID | None
    trace_id: str
    correlation_id: str
    idempotency_key: str
    is_delegated: bool
    principal_user_id: UUID | None
    delegate_user_id: UUID | None
    delegation_id: UUID | None
    details: dict[str, object]
    created_at: str
    previous_event_checksum: str | None
    event_checksum: str
    retention_expires_at: str
    retention_policy_code: str
    retention_days: int
    archived_at: str | None
    archival_reason_code: str | None


@dataclass(frozen=True)
class ArchivedAuditEvent:
    """Represent one deterministic archival transition outcome."""

    event_id: UUID
    user_id: UUID
    correlation_id: str
    archived_at: str
    archival_reason_code: str
    status: str


@dataclass(frozen=True)
class AuditEventQueryPage:
    """Represent one deterministic query/replay page result."""

    events: tuple[PersistedAuditEvent, ...]
    next_cursor_created_at: str | None
    next_cursor_event_id: UUID | None


@dataclass(frozen=True)
class AuditEventIntegrityVerification:
    """Represent one deterministic integrity verification result page."""

    algorithm: str
    verified_event_count: int
    verified_through_event_id: UUID | None
    next_cursor_created_at: str | None
    next_cursor_event_id: UUID | None
