"""Persistent and in-memory audit-event primitives for orchestration."""

from __future__ import annotations

import json
from typing import cast
from typing import Protocol
from typing import TypedDict
import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping

import psycopg

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.trace_context import build_optional_trace_id
from services.orchestration.app.action_execution_store import load_database_url

_AUDIT_TIME_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class OrchestrationAuditStoreError(RuntimeError):
    """Represent deterministic orchestration audit persistence failure."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class IncomeTaxAuditEvent(TypedDict):
    """Represent one canonical deterministic orchestration audit event."""

    event_id: str
    event_type: str
    event_time: str
    trace_id: str | None
    correlation_id: str | None
    tenant_id: str | None
    user_id: str | None
    resource_id: str | None
    status: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    context: dict[str, object]


class OrchestrationAuditEventStore(Protocol):
    """Describe deterministic storage for orchestration audit events."""

    def append(self, event: IncomeTaxAuditEvent) -> IncomeTaxAuditEvent:
        """Persist one audit event append-only and return stored payload."""
        ...

    def list(self, *, correlation_id: str | None = None) -> list[IncomeTaxAuditEvent]:
        """Return stored audit events, optionally filtered by correlation id."""
        ...

    def clear(self) -> None:
        """Reset audit events for deterministic test isolation."""
        ...


class InMemoryOrchestrationAuditEventStore:
    """Provide deterministic in-memory orchestration audit storage."""

    def __init__(self) -> None:
        self._events: dict[str, IncomeTaxAuditEvent] = {}

    def append(self, event: IncomeTaxAuditEvent) -> IncomeTaxAuditEvent:
        self._events.setdefault(event["event_id"], event)
        return self._events[event["event_id"]]

    def list(self, *, correlation_id: str | None = None) -> list[IncomeTaxAuditEvent]:
        events = list(self._events.values())
        if correlation_id is not None:
            events = [event for event in events if event["correlation_id"] == correlation_id]
        return _sorted_events(events)

    def clear(self) -> None:
        self._events.clear()


class PersistentOrchestrationAuditEventStore:
    """Persist orchestration audit events append-only in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def append(self, event: IncomeTaxAuditEvent) -> IncomeTaxAuditEvent:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO orchestration_audit_events (
                            event_id,
                            event_type,
                            event_time,
                            trace_id,
                            correlation_id,
                            tenant_id,
                            user_id,
                            resource_id,
                            status,
                            supported_lane_id,
                            historical_version_id,
                            tax_year,
                            payload_summary
                        )
                        VALUES (
                            %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                        )
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            event["event_id"],
                            event["event_type"],
                            event["event_time"],
                            event["trace_id"],
                            event["correlation_id"],
                            event["tenant_id"],
                            event["user_id"],
                            event["resource_id"],
                            event["status"],
                            event["supported_lane_id"],
                            event["historical_version_id"],
                            event["tax_year"],
                            canonical_json_dumps(event["context"]),
                        ),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise OrchestrationAuditStoreError(
                reason_code="audit_persistence_unavailable",
                message="Orchestration audit persistence is unavailable.",
            ) from error

        stored = self.list(correlation_id=event["correlation_id"])
        for item in stored:
            if item["event_id"] == event["event_id"]:
                return item
        return event

    def list(self, *, correlation_id: str | None = None) -> list[IncomeTaxAuditEvent]:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    if correlation_id is None:
                        cursor.execute(
                            """
                            SELECT
                                event_id,
                                event_type,
                                event_time,
                                trace_id,
                                correlation_id,
                                tenant_id,
                                user_id,
                                resource_id,
                                status,
                                supported_lane_id,
                                historical_version_id,
                                tax_year,
                                payload_summary
                            FROM orchestration_audit_events
                            ORDER BY event_time ASC, event_type ASC, event_id ASC
                            """
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT
                                event_id,
                                event_type,
                                event_time,
                                trace_id,
                                correlation_id,
                                tenant_id,
                                user_id,
                                resource_id,
                                status,
                                supported_lane_id,
                                historical_version_id,
                                tax_year,
                                payload_summary
                            FROM orchestration_audit_events
                            WHERE correlation_id = %s
                            ORDER BY event_time ASC, event_type ASC, event_id ASC
                            """,
                            (correlation_id,),
                        )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise OrchestrationAuditStoreError(
                reason_code="audit_persistence_unavailable",
                message="Orchestration audit persistence is unavailable.",
            ) from error

        events: list[IncomeTaxAuditEvent] = []
        for row in rows:
            payload_summary = row[12]
            context: dict[str, object]
            if isinstance(payload_summary, str):
                loaded = json.loads(payload_summary)
                assert isinstance(loaded, dict)
                context = cast(dict[str, object], loaded)
            else:
                assert isinstance(payload_summary, dict)
                context = cast(dict[str, object], payload_summary)
            event_time = cast(datetime, row[2]).astimezone(UTC).isoformat()
            events.append(
                {
                    "event_id": cast(str, row[0]),
                    "event_type": cast(str, row[1]),
                    "event_time": event_time,
                    "trace_id": cast(str | None, row[3]),
                    "correlation_id": cast(str | None, row[4]),
                    "tenant_id": cast(str | None, row[5]),
                    "user_id": cast(str | None, row[6]),
                    "resource_id": cast(str | None, row[7]),
                    "status": cast(str, row[8]),
                    "supported_lane_id": cast(str | None, row[9]),
                    "historical_version_id": cast(str | None, row[10]),
                    "tax_year": cast(int | None, row[11]),
                    "context": context,
                }
            )
        return _sorted_events(events)

    def clear(self) -> None:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM orchestration_audit_events")
                connection.commit()
        except psycopg.Error as error:
            raise OrchestrationAuditStoreError(
                reason_code="audit_persistence_unavailable",
                message="Orchestration audit persistence is unavailable.",
            ) from error


def build_default_orchestration_audit_event_store() -> OrchestrationAuditEventStore:
    """Build the default audit store with DB-backed persistence when available."""

    database_url = load_database_url()
    if not database_url:
        return InMemoryOrchestrationAuditEventStore()
    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.orchestration_audit_events')")
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    return InMemoryOrchestrationAuditEventStore()
    except psycopg.Error:
        return InMemoryOrchestrationAuditEventStore()
    return PersistentOrchestrationAuditEventStore(database_url=database_url)


_default_audit_event_store: OrchestrationAuditEventStore = (
    build_default_orchestration_audit_event_store()
)


def set_default_orchestration_audit_event_store(
    store: OrchestrationAuditEventStore,
) -> None:
    """Override the default orchestration audit store for runtime/tests."""

    global _default_audit_event_store
    _default_audit_event_store = store


def reset_default_orchestration_audit_event_store() -> None:
    """Reset the default orchestration audit store to environment-backed default."""

    global _default_audit_event_store
    _default_audit_event_store = build_default_orchestration_audit_event_store()


def emit_income_tax_audit_event(
    *,
    event_type: str,
    status: str,
    correlation_id: str | None,
    trace_id: str | None = None,
    supported_lane_id: str | None = None,
    historical_version_id: str | None = None,
    tax_year: int | None = None,
    context: Mapping[str, object] | None = None,
    event_time: str | None = None,
) -> IncomeTaxAuditEvent:
    """Emit one deterministic orchestration audit event and return it."""

    resolved_trace_id = trace_id or build_optional_trace_id(correlation_id)
    canonical_context = _canonical_context(context)
    resolved_event_time = event_time or _deterministic_event_time(
        event_type=event_type,
        status=status,
        correlation_id=correlation_id,
        trace_id=resolved_trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        context=canonical_context,
    )
    identity = {
        "scope": "orchestration_audit_event",
        "event_type": event_type,
        "event_time": resolved_event_time,
        "trace_id": resolved_trace_id,
        "correlation_id": correlation_id,
        "tenant_id": _context_string(canonical_context, "tenant_id"),
        "user_id": _context_string(canonical_context, "user_id"),
        "resource_id": _context_string(canonical_context, "resource_id"),
        "status": status,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "context": canonical_context,
    }
    event: IncomeTaxAuditEvent = {
        "event_id": _sha256_hex(canonical_json_dumps(identity)),
        "event_type": event_type,
        "event_time": resolved_event_time,
        "trace_id": resolved_trace_id,
        "correlation_id": correlation_id,
        "tenant_id": _context_string(canonical_context, "tenant_id"),
        "user_id": _context_string(canonical_context, "user_id"),
        "resource_id": _context_string(canonical_context, "resource_id"),
        "status": status,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "context": canonical_context,
    }
    return _default_audit_event_store.append(event)


def list_income_tax_audit_events(*, correlation_id: str | None = None) -> list[IncomeTaxAuditEvent]:
    """Return emitted audit events, optionally filtered by correlation id."""

    return _default_audit_event_store.list(correlation_id=correlation_id)


def clear_income_tax_audit_events() -> None:
    """Reset orchestration audit events for deterministic test isolation."""

    _default_audit_event_store.clear()


def _deterministic_event_time(
    *,
    event_type: str,
    status: str,
    correlation_id: str | None,
    trace_id: str | None,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    context: dict[str, object],
) -> str:
    digest_input = {
        "event_type": event_type,
        "status": status,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "context": context,
    }
    digest = _sha256_hex(canonical_json_dumps(digest_input))
    offset_seconds = int(digest[:8], 16) % (365 * 24 * 60 * 60)
    return (_AUDIT_TIME_BASE + timedelta(seconds=offset_seconds)).isoformat()


def _canonical_context(context: Mapping[str, object] | None) -> dict[str, object]:
    if context is None:
        return {}
    return json.loads(canonical_json_dumps(dict(context)))


def _context_string(context: Mapping[str, object], field_name: str) -> str | None:
    value = context.get(field_name)
    if isinstance(value, str) and value:
        return value
    return None


def _sorted_events(events: list[IncomeTaxAuditEvent]) -> list[IncomeTaxAuditEvent]:
    return sorted(
        events,
        key=lambda event: (event["event_time"], event["event_type"], event["event_id"]),
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
