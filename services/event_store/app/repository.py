"""Persistent append-only repository for event-store audit events."""

from __future__ import annotations

import json
from uuid import UUID
from typing import cast
import hashlib
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass

import psycopg

from services.event_store.app.config import load_database_url
from services.event_store.app.config import load_event_retention_policy
from services.event_store.app.models import ArchivedAuditEvent
from services.event_store.app.models import AuditEventQueryPage
from services.event_store.app.models import PersistedAuditEvent
from services.event_store.app.models import AuditEventIntegrityVerification

AUDIT_RESOURCE_TYPE = "event_store_runtime"
PERSISTENCE_UNAVAILABLE = "event_store_persistence_unavailable"
APPEND_CONFLICT = "event_store_append_conflict"
PERSISTENCE_NOT_CONFIGURED = "event_store_persistence_not_configured"
RETENTION_POLICY_INVALID = "event_store_retention_policy_invalid"
ARCHIVAL_INELIGIBLE = "event_store_archival_ineligible"
ARCHIVAL_NOT_FOUND = "event_store_archival_event_not_found"
ARCHIVAL_FORBIDDEN = "event_store_archival_forbidden"
QUERY_SCOPE_FORBIDDEN = "event_store_query_scope_forbidden"
QUERY_CURSOR_INVALID = "event_store_query_cursor_invalid"
INTEGRITY_CHECK_FAILED = "event_store_integrity_check_failed"


class EventStoreRepositoryError(RuntimeError):
    """Represent deterministic persistence failure in event-store repository."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class _IdempotencyLookupRecord:
    event: PersistedAuditEvent
    payload_fingerprint: str | None
    role_at_time: str | None


class EventStoreRepository:
    """Provide persistent append-only event repository operations."""

    def __init__(self, *, database_url: str | None = None) -> None:
        self._database_url = load_database_url() if database_url is None else database_url

    def append_event(
        self,
        *,
        event_type: str,
        user_id: UUID | None,
        role_at_time: str | None,
        trace_id: str,
        correlation_id: str,
        idempotency_key: str,
        is_delegated: bool,
        principal_user_id: UUID | None,
        delegate_user_id: UUID | None,
        delegation_id: UUID | None,
        event_timestamp: datetime,
        details: dict[str, object] | None = None,
        resource_id: UUID | None = None,
    ) -> PersistedAuditEvent:
        """Append one immutable audit event into persistent storage."""

        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )

        try:
            retention_policy_code, retention_days = load_event_retention_policy()
        except ValueError as error:
            raise EventStoreRepositoryError(
                reason_code=RETENTION_POLICY_INVALID,
                message=str(error),
            ) from error

        persisted_details: dict[str, object] = {
            "trace_id": trace_id,
            "action_type": event_type,
            "is_delegated": is_delegated,
            "principal_user_id": str(principal_user_id) if principal_user_id is not None else None,
            "delegate_user_id": str(delegate_user_id) if delegate_user_id is not None else None,
            "delegation_id": str(delegation_id) if delegation_id is not None else None,
            "retention_policy_code": retention_policy_code,
            "retention_days": retention_days,
        }
        if details:
            persisted_details.update(details)
        resolved_resource_id = resource_id if resource_id is not None else user_id
        persisted_details.setdefault(
            "resource_id",
            None if resolved_resource_id is None else str(resolved_resource_id),
        )
        payload_fingerprint = _compute_append_payload_fingerprint(
            event_type=event_type,
            user_id=user_id,
            role_at_time=role_at_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
            is_delegated=is_delegated,
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            delegation_id=delegation_id,
            details=persisted_details,
            resource_id=resolved_resource_id,
        )

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=user_id, role=role_at_time)
                    previous_event_hash = self._latest_event_hash(
                        cursor=cursor,
                        user_id=user_id,
                        resource_id=resolved_resource_id,
                    )
                    cursor.execute(
                        """
                        INSERT INTO audit_events (
                            user_id,
                            role_at_time,
                            event_type,
                            resource_type,
                            resource_id,
                            correlation_id,
                            request_id,
                            idempotency_key,
                            details,
                            previous_event_hash,
                            event_hash,
                            event_timestamp,
                            retention_expires_at,
                            retention_policy_code,
                            retention_days,
                            idempotency_payload_fingerprint
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s::jsonb,
                            %s,
                            %s,
                            LEAST(%s, now()),
                            LEAST(%s, now()) + make_interval(days => %s),
                            %s,
                            %s,
                            %s
                        )
                        RETURNING
                            id::text,
                            created_at,
                            previous_event_hash,
                            event_hash,
                            event_timestamp,
                            retention_expires_at
                        """,
                        (
                            user_id,
                            role_at_time,
                            event_type,
                            AUDIT_RESOURCE_TYPE,
                            resolved_resource_id,
                            correlation_id,
                            trace_id,
                            idempotency_key,
                            json.dumps(persisted_details, sort_keys=True),
                            previous_event_hash,
                            None,
                            event_timestamp,
                            event_timestamp,
                            retention_days,
                            retention_policy_code,
                            retention_days,
                            payload_fingerprint,
                        ),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.errors.UniqueViolation as error:
            existing = self._get_event_by_idempotency_key(idempotency_key=idempotency_key)
            if existing is not None:
                existing_fingerprint = (
                    existing.payload_fingerprint
                    or _compute_append_payload_fingerprint(
                        event_type=existing.event.event_type,
                        user_id=existing.event.user_id,
                        role_at_time=existing.role_at_time,
                        trace_id=existing.event.trace_id,
                        correlation_id=existing.event.correlation_id,
                        is_delegated=existing.event.is_delegated,
                        principal_user_id=existing.event.principal_user_id,
                        delegate_user_id=existing.event.delegate_user_id,
                        delegation_id=existing.event.delegation_id,
                        details=existing.event.details,
                        resource_id=_parse_optional_uuid(existing.event.details.get("resource_id")),
                    )
                )
                if existing_fingerprint == payload_fingerprint:
                    return existing.event
            raise EventStoreRepositoryError(
                reason_code=APPEND_CONFLICT,
                message="Audit event append conflicts with existing idempotency payload.",
            ) from error
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        if row is None:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store append did not return persisted row metadata.",
            )
        created_at = cast(datetime, row[1]).astimezone(UTC).isoformat().replace("+00:00", "Z")
        persisted_retention_expires_at = (
            cast(datetime, row[5]).astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
        return PersistedAuditEvent(
            event_id=UUID(str(row[0])),
            event_type=event_type,
            action_type=event_type,
            user_id=user_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            is_delegated=is_delegated,
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            delegation_id=delegation_id,
            details=persisted_details,
            created_at=created_at,
            previous_event_checksum=cast(str | None, row[2]),
            event_checksum=str(row[3]),
            retention_expires_at=persisted_retention_expires_at,
            retention_policy_code=retention_policy_code,
            retention_days=retention_days,
            archived_at=None,
            archival_reason_code=None,
        )

    def list_events_since(self, *, created_at_floor: datetime) -> tuple[PersistedAuditEvent, ...]:
        """List deterministic immutable events created after floor timestamp."""

        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_events.id::text,
                            audit_events.event_type,
                            audit_events.user_id::text,
                            audit_events.correlation_id,
                            audit_events.idempotency_key,
                            audit_events.details,
                            audit_events.created_at,
                            audit_events.previous_event_hash,
                            audit_events.event_hash,
                            audit_events.retention_expires_at,
                            audit_events.retention_policy_code,
                            audit_events.retention_days,
                            archived.archived_at,
                            archived.archival_reason_code
                        FROM audit_events
                        LEFT JOIN audit_event_archivals AS archived
                          ON archived.event_id = audit_events.id
                        WHERE audit_events.resource_type = %s
                          AND audit_events.created_at >= %s
                        ORDER BY audit_events.created_at ASC, audit_events.id ASC
                        """,
                        (AUDIT_RESOURCE_TYPE, created_at_floor),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        events: list[PersistedAuditEvent] = []
        for row in rows:
            details = _parse_details(cast_value=row[5])
            created_at = cast(datetime, row[6]).astimezone(UTC).isoformat().replace("+00:00", "Z")
            events.append(
                _build_persisted_audit_event(
                    row=row,
                    details=details,
                    created_at=created_at,
                    retention_expires_at=cast(datetime, row[9])
                    .astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    archived_at=_parse_optional_datetime_to_z(row[12]),
                    archival_reason_code=cast(str | None, row[13]),
                )
            )
        return tuple(events)

    def query_events_page(
        self,
        *,
        allowed_user_ids: tuple[UUID, ...],
        user_id: UUID | None,
        correlation_id: str | None,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_event_id: UUID | None,
    ) -> AuditEventQueryPage:
        """Return one deterministic query page scoped to allowed users."""

        if limit < 1 or limit > 200:
            raise EventStoreRepositoryError(
                reason_code="invalid_event_store_request",
                message="Limit must be between 1 and 200.",
            )
        if cursor_created_at is None and cursor_event_id is not None:
            raise EventStoreRepositoryError(
                reason_code=QUERY_CURSOR_INVALID,
                message="Pagination cursor is invalid.",
            )
        if cursor_created_at is not None and cursor_event_id is None:
            raise EventStoreRepositoryError(
                reason_code=QUERY_CURSOR_INVALID,
                message="Pagination cursor is invalid.",
            )
        if user_id is not None and user_id not in allowed_user_ids:
            raise EventStoreRepositoryError(
                reason_code=QUERY_SCOPE_FORBIDDEN,
                message="Requested user scope is forbidden for this principal.",
            )
        if not allowed_user_ids:
            return AuditEventQueryPage(
                events=(),
                next_cursor_created_at=None,
                next_cursor_event_id=None,
            )
        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )

        query_user_ids = (user_id,) if user_id is not None else allowed_user_ids
        query_correlation_id = correlation_id.strip() if correlation_id is not None else None
        if correlation_id is not None and not query_correlation_id:
            raise EventStoreRepositoryError(
                reason_code="invalid_event_store_request",
                message="Correlation identifier must be a non-empty string.",
            )

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_events.id::text,
                            audit_events.event_type,
                            audit_events.user_id::text,
                            audit_events.correlation_id,
                            audit_events.idempotency_key,
                            audit_events.details,
                            audit_events.created_at,
                            audit_events.previous_event_hash,
                            audit_events.event_hash,
                            audit_events.retention_expires_at,
                            audit_events.retention_policy_code,
                            audit_events.retention_days,
                            archived.archived_at,
                            archived.archival_reason_code
                        FROM audit_events
                        LEFT JOIN audit_event_archivals AS archived
                          ON archived.event_id = audit_events.id
                        WHERE audit_events.resource_type = %s
                          AND audit_events.user_id = ANY(%s::uuid[])
                          AND (%s::text IS NULL OR audit_events.correlation_id = %s::text)
                          AND (
                                %s::timestamptz IS NULL
                                OR
                                (audit_events.created_at, audit_events.id)
                                    > (%s::timestamptz, %s::uuid)
                          )
                        ORDER BY audit_events.created_at ASC, audit_events.id ASC
                        LIMIT %s
                        """,
                        (
                            AUDIT_RESOURCE_TYPE,
                            list(query_user_ids),
                            query_correlation_id,
                            query_correlation_id,
                            cursor_created_at,
                            cursor_created_at,
                            cursor_event_id,
                            limit + 1,
                        ),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        page_rows = rows[:limit]
        events: list[PersistedAuditEvent] = []
        for row in page_rows:
            details = _parse_details(cast_value=row[5])
            events.append(
                _build_persisted_audit_event(
                    row=row,
                    details=details,
                    created_at=_to_z_datetime(cast(datetime, row[6])),
                    retention_expires_at=_to_z_datetime(cast(datetime, row[9])),
                    archived_at=_parse_optional_datetime_to_z(row[12]),
                    archival_reason_code=cast(str | None, row[13]),
                )
            )

        next_cursor_created_at: str | None = None
        next_cursor_event_id: UUID | None = None
        if len(rows) > limit and events:
            tail = events[-1]
            next_cursor_created_at = tail.created_at
            next_cursor_event_id = tail.event_id

        return AuditEventQueryPage(
            events=tuple(events),
            next_cursor_created_at=next_cursor_created_at,
            next_cursor_event_id=next_cursor_event_id,
        )

    def list_retention_eligible_events(
        self,
        *,
        as_of: datetime,
        limit: int,
        allowed_user_ids: tuple[UUID, ...],
    ) -> tuple[PersistedAuditEvent, ...]:
        """List deterministic, scoped archival-eligible events."""

        if limit < 1 or limit > 200:
            raise EventStoreRepositoryError(
                reason_code="invalid_event_store_request",
                message="Limit must be between 1 and 200.",
            )
        if not allowed_user_ids:
            return ()
        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_events.id::text,
                            audit_events.event_type,
                            audit_events.user_id::text,
                            audit_events.correlation_id,
                            audit_events.idempotency_key,
                            audit_events.details,
                            audit_events.created_at,
                            audit_events.previous_event_hash,
                            audit_events.event_hash,
                            audit_events.retention_expires_at,
                            audit_events.retention_policy_code,
                            audit_events.retention_days,
                            archived.archived_at,
                            archived.archival_reason_code
                        FROM audit_events
                        LEFT JOIN audit_event_archivals AS archived
                          ON archived.event_id = audit_events.id
                        WHERE audit_events.resource_type = %s
                          AND audit_events.user_id = ANY(%s::uuid[])
                          AND audit_events.retention_expires_at <= %s
                          AND archived.event_id IS NULL
                        ORDER BY
                            audit_events.retention_expires_at ASC,
                            audit_events.created_at ASC,
                            audit_events.id ASC
                        LIMIT %s
                        """,
                        (AUDIT_RESOURCE_TYPE, list(allowed_user_ids), as_of, limit),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        events: list[PersistedAuditEvent] = []
        for row in rows:
            details = _parse_details(cast_value=row[5])
            events.append(
                _build_persisted_audit_event(
                    row=row,
                    details=details,
                    created_at=_to_z_datetime(cast(datetime, row[6])),
                    retention_expires_at=_to_z_datetime(cast(datetime, row[9])),
                    archived_at=_parse_optional_datetime_to_z(row[12]),
                    archival_reason_code=cast(str | None, row[13]),
                )
            )
        return tuple(events)

    def verify_integrity_page(
        self,
        *,
        allowed_user_ids: tuple[UUID, ...],
        user_id: UUID | None,
        correlation_id: str | None,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_event_id: UUID | None,
    ) -> AuditEventIntegrityVerification:
        """Verify deterministic hash-chain/checksum integrity for one scoped page."""

        if limit < 1 or limit > 200:
            raise EventStoreRepositoryError(
                reason_code="invalid_event_store_request",
                message="Limit must be between 1 and 200.",
            )
        if cursor_created_at is None and cursor_event_id is not None:
            raise EventStoreRepositoryError(
                reason_code=QUERY_CURSOR_INVALID,
                message="Pagination cursor is invalid.",
            )
        if cursor_created_at is not None and cursor_event_id is None:
            raise EventStoreRepositoryError(
                reason_code=QUERY_CURSOR_INVALID,
                message="Pagination cursor is invalid.",
            )
        if user_id is not None and user_id not in allowed_user_ids:
            raise EventStoreRepositoryError(
                reason_code=QUERY_SCOPE_FORBIDDEN,
                message="Requested user scope is forbidden for this principal.",
            )
        if not allowed_user_ids:
            return AuditEventIntegrityVerification(
                algorithm="sha256",
                verified_event_count=0,
                verified_through_event_id=None,
                next_cursor_created_at=None,
                next_cursor_event_id=None,
            )
        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )

        query_user_ids = (user_id,) if user_id is not None else allowed_user_ids
        query_correlation_id = correlation_id.strip() if correlation_id is not None else None
        if correlation_id is not None and not query_correlation_id:
            raise EventStoreRepositoryError(
                reason_code="invalid_event_store_request",
                message="Correlation identifier must be a non-empty string.",
            )

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_events.id::text,
                            audit_events.created_at,
                            audit_events.event_hash,
                            audit_events.previous_event_hash,
                            encode(
                                digest(
                                    CASE
                                        WHEN audit_events.previous_event_hash IS NULL THEN
                                            fn_audit_events_canonical_payload(
                                                audit_events.user_id,
                                                audit_events.resource_type,
                                                audit_events.resource_id,
                                                audit_events.event_type,
                                                audit_events.event_timestamp,
                                                audit_events.correlation_id
                                            )
                                        ELSE
                                            audit_events.previous_event_hash
                                            || fn_audit_events_canonical_payload(
                                                audit_events.user_id,
                                                audit_events.resource_type,
                                                audit_events.resource_id,
                                                audit_events.event_type,
                                                audit_events.event_timestamp,
                                                audit_events.correlation_id
                                            )
                                    END,
                                    'sha256'
                                ),
                                'hex'
                            ) AS expected_event_hash,
                            CASE
                                WHEN audit_events.previous_event_hash IS NULL THEN TRUE
                                ELSE EXISTS (
                                    SELECT 1
                                    FROM audit_events AS prior
                                    WHERE prior.event_hash = audit_events.previous_event_hash
                                )
                            END AS previous_hash_exists
                        FROM audit_events
                        WHERE audit_events.resource_type = %s
                          AND audit_events.user_id = ANY(%s::uuid[])
                          AND (%s::text IS NULL OR audit_events.correlation_id = %s::text)
                          AND (
                                %s::timestamptz IS NULL
                                OR
                                (audit_events.created_at, audit_events.id)
                                    > (%s::timestamptz, %s::uuid)
                          )
                        ORDER BY audit_events.created_at ASC, audit_events.id ASC
                        LIMIT %s
                        """,
                        (
                            AUDIT_RESOURCE_TYPE,
                            list(query_user_ids),
                            query_correlation_id,
                            query_correlation_id,
                            cursor_created_at,
                            cursor_created_at,
                            cursor_event_id,
                            limit + 1,
                        ),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        page_rows = rows[:limit]
        for row in page_rows:
            event_id = UUID(str(row[0]))
            stored_checksum = str(row[2])
            previous_checksum = cast(str | None, row[3])
            expected_checksum = str(row[4])
            previous_exists = bool(row[5])
            if stored_checksum != expected_checksum:
                raise EventStoreRepositoryError(
                    reason_code=INTEGRITY_CHECK_FAILED,
                    message=f"Integrity verification failed for event_id={event_id}.",
                )
            if previous_checksum is not None and not previous_exists:
                raise EventStoreRepositoryError(
                    reason_code=INTEGRITY_CHECK_FAILED,
                    message=f"Integrity chain reference is missing for event_id={event_id}.",
                )

        next_cursor_created_at: str | None = None
        next_cursor_event_id: UUID | None = None
        verified_through_event_id: UUID | None = None
        if page_rows:
            verified_through_event_id = UUID(str(page_rows[-1][0]))
        if len(rows) > limit and page_rows:
            next_cursor_created_at = _to_z_datetime(cast(datetime, page_rows[-1][1]))
            next_cursor_event_id = UUID(str(page_rows[-1][0]))

        return AuditEventIntegrityVerification(
            algorithm="sha256",
            verified_event_count=len(page_rows),
            verified_through_event_id=verified_through_event_id,
            next_cursor_created_at=next_cursor_created_at,
            next_cursor_event_id=next_cursor_event_id,
        )

    def mark_event_archived(
        self,
        *,
        event_id: UUID,
        archived_at: datetime,
        reason_code: str,
        archived_by_user_id: UUID,
        allowed_user_ids: tuple[UUID, ...],
    ) -> ArchivedAuditEvent:
        """Mark one scoped event archived deterministically and idempotently."""

        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )
        if not allowed_user_ids:
            raise EventStoreRepositoryError(
                reason_code=ARCHIVAL_FORBIDDEN,
                message="Archival access is forbidden for this principal scope.",
            )

        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_events.user_id::text,
                            audit_events.correlation_id,
                            audit_events.retention_expires_at,
                            archived.archived_at,
                            archived.archival_reason_code
                        FROM audit_events
                        LEFT JOIN audit_event_archivals AS archived
                          ON archived.event_id = audit_events.id
                        WHERE audit_events.id = %s
                          AND audit_events.resource_type = %s
                        LIMIT 1
                        """,
                        (event_id, AUDIT_RESOURCE_TYPE),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise EventStoreRepositoryError(
                            reason_code=ARCHIVAL_NOT_FOUND,
                            message="Audit event was not found for archival.",
                        )

                    event_user_id = UUID(str(row[0]))
                    correlation_id = str(row[1])
                    retention_expires_at = cast(datetime, row[2])
                    already_archived_at = cast(datetime | None, row[3])
                    already_reason_code = cast(str | None, row[4])

                    if event_user_id not in allowed_user_ids:
                        raise EventStoreRepositoryError(
                            reason_code=ARCHIVAL_FORBIDDEN,
                            message="Archival access is forbidden for this principal scope.",
                        )

                    if already_archived_at is not None:
                        return ArchivedAuditEvent(
                            event_id=event_id,
                            user_id=event_user_id,
                            correlation_id=correlation_id,
                            archived_at=already_archived_at.astimezone(UTC)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            archival_reason_code=already_reason_code or reason_code,
                            status="already_archived",
                        )

                    if archived_at < retention_expires_at:
                        raise EventStoreRepositoryError(
                            reason_code=ARCHIVAL_INELIGIBLE,
                            message="Audit event is not yet eligible for archival.",
                        )

                    cursor.execute(
                        """
                        INSERT INTO audit_event_archivals (
                            event_id,
                            archived_by_user_id,
                            archived_at,
                            archival_reason_code
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        RETURNING archived_at
                        """,
                        (event_id, archived_by_user_id, archived_at, reason_code),
                    )
                    inserted = cursor.fetchone()
                connection.commit()
        except EventStoreRepositoryError:
            raise
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        if inserted is None:
            refreshed = self.mark_event_archived(
                event_id=event_id,
                archived_at=archived_at,
                reason_code=reason_code,
                archived_by_user_id=archived_by_user_id,
                allowed_user_ids=allowed_user_ids,
            )
            return ArchivedAuditEvent(
                event_id=refreshed.event_id,
                user_id=refreshed.user_id,
                correlation_id=refreshed.correlation_id,
                archived_at=refreshed.archived_at,
                archival_reason_code=refreshed.archival_reason_code,
                status="already_archived",
            )

        return ArchivedAuditEvent(
            event_id=event_id,
            user_id=event_user_id,
            correlation_id=correlation_id,
            archived_at=_to_z_datetime(cast(datetime, inserted[0])),
            archival_reason_code=reason_code,
            status="archived",
        )

    def latest_created_at(self) -> datetime | None:
        """Return latest persisted creation timestamp for event-store records."""

        if self._database_url is None or not self._database_url.strip():
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_NOT_CONFIGURED,
                message="Event-store persistence is not configured.",
            )
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT MAX(created_at)
                        FROM audit_events
                        WHERE resource_type = %s
                        """,
                        (AUDIT_RESOURCE_TYPE,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise EventStoreRepositoryError(
                reason_code=PERSISTENCE_UNAVAILABLE,
                message="Event-store persistence is unavailable.",
            ) from error

        if row is None or row[0] is None:
            return None
        return cast(datetime, row[0]).astimezone(UTC)

    def _latest_event_hash(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID | None,
        resource_id: UUID | None,
    ) -> str | None:
        cursor.execute(
            """
            SELECT event_hash
            FROM audit_events
            WHERE user_id IS NOT DISTINCT FROM %s
              AND resource_type = %s
              AND resource_id IS NOT DISTINCT FROM %s
            ORDER BY event_timestamp DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, AUDIT_RESOURCE_TYPE, resource_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return cast(str, row[0])

    def _get_event_by_idempotency_key(
        self, *, idempotency_key: str
    ) -> _IdempotencyLookupRecord | None:
        if self._database_url is None or not self._database_url.strip():
            return None
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_events.id::text,
                            audit_events.event_type,
                            audit_events.user_id::text,
                            audit_events.correlation_id,
                            audit_events.idempotency_key,
                            audit_events.details,
                            audit_events.created_at,
                            audit_events.previous_event_hash,
                            audit_events.event_hash,
                            audit_events.retention_expires_at,
                            audit_events.retention_policy_code,
                            audit_events.retention_days,
                            audit_events.role_at_time,
                            audit_events.request_id,
                            audit_events.idempotency_payload_fingerprint,
                            archived.archived_at,
                            archived.archival_reason_code
                        FROM audit_events
                        LEFT JOIN audit_event_archivals AS archived
                          ON archived.event_id = audit_events.id
                        WHERE audit_events.resource_type = %s
                          AND audit_events.idempotency_key = %s
                        ORDER BY audit_events.created_at ASC, audit_events.id ASC
                        LIMIT 1
                        """,
                        (AUDIT_RESOURCE_TYPE, idempotency_key),
                    )
                    row = cursor.fetchone()
        except psycopg.Error:
            return None

        if row is None:
            return None

        details = _parse_details(cast_value=row[5])
        created_at = cast(datetime, row[6]).astimezone(UTC).isoformat().replace("+00:00", "Z")
        event = _build_persisted_audit_event(
            row=row,
            details=details,
            created_at=created_at,
            retention_expires_at=_to_z_datetime(cast(datetime, row[9])),
            archived_at=_parse_optional_datetime_to_z(row[15]),
            archival_reason_code=cast(str | None, row[16]),
            trace_id_override=str(row[13]) if row[13] is not None else None,
        )
        return _IdempotencyLookupRecord(
            event=event,
            payload_fingerprint=cast(str | None, row[14]),
            role_at_time=cast(str | None, row[12]),
        )

    def _ensure_user_row(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID | None,
        role: str | None,
    ) -> None:
        if user_id is None:
            return
        synthetic_phone = f"enc-phone-{user_id}"
        synthetic_email = f"enc-email-{user_id}@kodi.local"
        cursor.execute(
            """
            INSERT INTO users (
                id,
                phone_number_encrypted,
                email_encrypted,
                role,
                subscription_tier
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (user_id, synthetic_phone, synthetic_email, role or "IndividualTaxpayer", "standard"),
        )


_default_event_store_repository: EventStoreRepository | None = None


def get_default_event_store_repository() -> EventStoreRepository:
    """Return singleton event-store repository."""

    global _default_event_store_repository
    if _default_event_store_repository is None:
        _default_event_store_repository = EventStoreRepository()
    return _default_event_store_repository


def _parse_optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _parse_details(*, cast_value: object) -> dict[str, object]:
    if isinstance(cast_value, str):
        try:
            parsed = json.loads(cast_value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
        return {}
    if isinstance(cast_value, dict):
        return cast(dict[str, object], cast_value)
    return {}


def _parse_optional_datetime_to_z(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_z_datetime(value)
    return None


def _to_z_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _compute_append_payload_fingerprint(
    *,
    event_type: str,
    user_id: UUID | None,
    role_at_time: str | None,
    trace_id: str,
    correlation_id: str,
    is_delegated: bool,
    principal_user_id: UUID | None,
    delegate_user_id: UUID | None,
    delegation_id: UUID | None,
    details: dict[str, object],
    resource_id: UUID | None,
) -> str:
    canonical_payload = {
        "event_type": event_type,
        "user_id": None if user_id is None else str(user_id),
        "role_at_time": role_at_time,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "is_delegated": is_delegated,
        "principal_user_id": str(principal_user_id) if principal_user_id is not None else None,
        "delegate_user_id": str(delegate_user_id) if delegate_user_id is not None else None,
        "delegation_id": str(delegation_id) if delegation_id is not None else None,
        "resource_id": None if resource_id is None else str(resource_id),
        "details": details,
    }
    encoded = json.dumps(canonical_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_persisted_audit_event(
    *,
    row: tuple[object, ...],
    details: dict[str, object],
    created_at: str,
    retention_expires_at: str,
    archived_at: str | None,
    archival_reason_code: str | None,
    trace_id_override: str | None = None,
) -> PersistedAuditEvent:
    return PersistedAuditEvent(
        event_id=UUID(str(row[0])),
        event_type=str(row[1]),
        action_type=str(details.get("action_type", row[1])),
        user_id=_parse_optional_uuid(row[2]),
        trace_id=trace_id_override
        if trace_id_override is not None
        else str(details.get("trace_id", "")),
        correlation_id=str(row[3]),
        idempotency_key=str(row[4]),
        is_delegated=bool(details.get("is_delegated", False)),
        principal_user_id=_parse_optional_uuid(details.get("principal_user_id")),
        delegate_user_id=_parse_optional_uuid(details.get("delegate_user_id")),
        delegation_id=_parse_optional_uuid(details.get("delegation_id")),
        details=details,
        created_at=created_at,
        previous_event_checksum=cast(str | None, row[7]),
        event_checksum=str(row[8]),
        retention_expires_at=retention_expires_at,
        retention_policy_code=str(row[10]),
        retention_days=int(cast(int | str, row[11])),
        archived_at=archived_at,
        archival_reason_code=archival_reason_code,
    )
