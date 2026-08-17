"""Emit immutable deterministic audit evidence for document lifecycle actions."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Any
from typing import cast
from typing import Literal
from typing import Protocol
from typing import TypedDict
from typing import LiteralString
import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Mapping
from collections.abc import Callable

import psycopg
from psycopg import sql

from shared.determinism.input_hash import canonical_json_dumps
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction

_AUDIT_TIME_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _row_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError("document_ai_audit_payload_invalid")
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


LifecycleActionName = Literal[
    "trash",
    "restore",
    "mark_eligible_for_purge",
    "execute_purge",
]
LifecycleActionStatus = Literal["success", "failure", "checked"]
ComplianceOverrideAction = Literal[
    "trash",
    "restore",
    "mark_eligible_for_purge",
    "execute_purge",
]
ComplianceOverrideEventType = Literal["request", "approve", "reject", "use", "expire"]
ComplianceOverrideEventStatus = Literal["success", "failure"]


class DocumentLifecycleAuditEvidence(TypedDict):
    """Represent one immutable lifecycle audit evidence record."""

    audit_evidence_id: str
    event_time: str
    action: LifecycleActionName
    action_status: LifecycleActionStatus
    document_id: str
    previous_state: str | None
    new_state: str | None
    tenant_id: str
    user_id: str
    reason_code: str | None
    trace_id: str
    correlation_id: str


class DocumentAuditBackend(Protocol):
    """Represent the required audit backend interface."""

    def append_lifecycle(
        self,
        event: DocumentLifecycleAuditEvidence,
    ) -> DocumentLifecycleAuditEvidence: ...

    def list_lifecycle(
        self,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
    ) -> list[DocumentLifecycleAuditEvidence]: ...

    def clear_lifecycle(self) -> None: ...

    def append_compliance_override(
        self,
        event: DocumentComplianceOverrideAuditEvidence,
    ) -> DocumentComplianceOverrideAuditEvidence: ...

    def list_compliance_override(
        self,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        override_id: str | None = None,
    ) -> list[DocumentComplianceOverrideAuditEvidence]: ...

    def clear_compliance_override(self) -> None: ...


class InMemoryDocumentAuditBackend:
    """Use deterministic in-memory audit storage for development and tests."""

    def __init__(self) -> None:
        self._lifecycle_events: list[DocumentLifecycleAuditEvidence] = []
        self._compliance_override_events: list[DocumentComplianceOverrideAuditEvidence] = []

    def append_lifecycle(
        self,
        event: DocumentLifecycleAuditEvidence,
    ) -> DocumentLifecycleAuditEvidence:
        self._lifecycle_events.append(event)
        return event

    def list_lifecycle(
        self,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
    ) -> list[DocumentLifecycleAuditEvidence]:
        events = list(self._lifecycle_events)
        if correlation_id is not None:
            events = [event for event in events if event["correlation_id"] == correlation_id]
        if document_id is not None:
            events = [event for event in events if event["document_id"] == document_id]
        return events

    def clear_lifecycle(self) -> None:
        self._lifecycle_events.clear()

    def append_compliance_override(
        self,
        event: DocumentComplianceOverrideAuditEvidence,
    ) -> DocumentComplianceOverrideAuditEvidence:
        self._compliance_override_events.append(event)
        return event

    def list_compliance_override(
        self,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        override_id: str | None = None,
    ) -> list[DocumentComplianceOverrideAuditEvidence]:
        events = list(self._compliance_override_events)
        if correlation_id is not None:
            events = [event for event in events if event["correlation_id"] == correlation_id]
        if document_id is not None:
            events = [event for event in events if event["document_id"] == document_id]
        if override_id is not None:
            events = [event for event in events if event["override_id"] == override_id]
        return events

    def clear_compliance_override(self) -> None:
        self._compliance_override_events.clear()


class PersistentDocumentAuditBackend:
    """Persist immutable deterministic audit evidence to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def append_lifecycle(
        self,
        event: DocumentLifecycleAuditEvidence,
    ) -> DocumentLifecycleAuditEvidence:
        payload = event
        self._execute(
            transaction_name="document_ai.document_audit.append_lifecycle",
            query="""
            INSERT INTO document_ai_lifecycle_audit_evidence (
                audit_evidence_id,
                tenant_id,
                document_id,
                action,
                action_status,
                previous_state,
                new_state,
                user_id,
                reason_code,
                trace_id,
                correlation_id,
                event_time,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (audit_evidence_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                document_id = EXCLUDED.document_id,
                action = EXCLUDED.action,
                action_status = EXCLUDED.action_status,
                previous_state = EXCLUDED.previous_state,
                new_state = EXCLUDED.new_state,
                user_id = EXCLUDED.user_id,
                reason_code = EXCLUDED.reason_code,
                trace_id = EXCLUDED.trace_id,
                correlation_id = EXCLUDED.correlation_id,
                event_time = EXCLUDED.event_time,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            params=(
                event["audit_evidence_id"],
                event["tenant_id"],
                UUID(event["document_id"]),
                event["action"],
                event["action_status"],
                event["previous_state"],
                event["new_state"],
                UUID(event["user_id"]),
                event["reason_code"],
                event["trace_id"],
                event["correlation_id"],
                _parse_iso_datetime(event["event_time"]),
                json.dumps(payload, sort_keys=True),
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_lifecycle_result(
                connection=connection, event=event
            ),
        )
        return event

    def list_lifecycle(
        self,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
    ) -> list[DocumentLifecycleAuditEvidence]:
        filters: list[str] = []
        params: list[object] = []
        if correlation_id is not None:
            filters.append("correlation_id = %s")
            params.append(correlation_id)
        if document_id is not None:
            filters.append("document_id = %s")
            params.append(UUID(document_id))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self._fetch_all(
            f"""
            SELECT payload
            FROM document_ai_lifecycle_audit_evidence
            {where}
            ORDER BY event_time ASC, audit_evidence_id ASC
            """,
            tuple(params),
        )
        return [cast(DocumentLifecycleAuditEvidence, _row_payload(row[0])) for row in rows]

    def clear_lifecycle(self) -> None:
        self._execute(
            transaction_name="document_ai.document_audit.clear_lifecycle",
            query="DELETE FROM document_ai_lifecycle_audit_evidence",
            reconcile_ambiguous_result=lambda connection: self._reconcile_lifecycle_clear(
                connection=connection
            ),
        )

    def append_compliance_override(
        self,
        event: DocumentComplianceOverrideAuditEvidence,
    ) -> DocumentComplianceOverrideAuditEvidence:
        payload = event
        self._execute(
            transaction_name="document_ai.document_audit.append_compliance_override",
            query="""
            INSERT INTO document_ai_compliance_override_audit_evidence (
                audit_evidence_id,
                override_id,
                tenant_id,
                document_id,
                event_type,
                event_status,
                requested_action,
                actor_user_id,
                actor_role,
                reason_code,
                state_before,
                state_after,
                trace_id,
                correlation_id,
                event_time,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (audit_evidence_id) DO UPDATE SET
                override_id = EXCLUDED.override_id,
                tenant_id = EXCLUDED.tenant_id,
                document_id = EXCLUDED.document_id,
                event_type = EXCLUDED.event_type,
                event_status = EXCLUDED.event_status,
                requested_action = EXCLUDED.requested_action,
                actor_user_id = EXCLUDED.actor_user_id,
                actor_role = EXCLUDED.actor_role,
                reason_code = EXCLUDED.reason_code,
                state_before = EXCLUDED.state_before,
                state_after = EXCLUDED.state_after,
                trace_id = EXCLUDED.trace_id,
                correlation_id = EXCLUDED.correlation_id,
                event_time = EXCLUDED.event_time,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            params=(
                event["audit_evidence_id"],
                event["override_id"],
                event["tenant_id"],
                UUID(event["document_id"]),
                event["event_type"],
                event["event_status"],
                event["requested_action"],
                UUID(event["actor_user_id"]),
                event["actor_role"],
                event["reason_code"],
                event["state_before"],
                event["state_after"],
                event["trace_id"],
                event["correlation_id"],
                _parse_iso_datetime(event["event_time"]),
                json.dumps(payload, sort_keys=True),
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_compliance_result(
                connection=connection, event=event
            ),
        )
        return event

    def list_compliance_override(
        self,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        override_id: str | None = None,
    ) -> list[DocumentComplianceOverrideAuditEvidence]:
        filters: list[str] = []
        params: list[object] = []
        if correlation_id is not None:
            filters.append("correlation_id = %s")
            params.append(correlation_id)
        if document_id is not None:
            filters.append("document_id = %s")
            params.append(UUID(document_id))
        if override_id is not None:
            filters.append("override_id = %s")
            params.append(override_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self._fetch_all(
            f"""
            SELECT payload
            FROM document_ai_compliance_override_audit_evidence
            {where}
            ORDER BY event_time ASC, audit_evidence_id ASC
            """,
            tuple(params),
        )
        return [cast(DocumentComplianceOverrideAuditEvidence, _row_payload(row[0])) for row in rows]

    def clear_compliance_override(self) -> None:
        self._execute(
            transaction_name="document_ai.document_audit.clear_compliance_override",
            query="DELETE FROM document_ai_compliance_override_audit_evidence",
            reconcile_ambiguous_result=lambda connection: self._reconcile_compliance_clear(
                connection=connection
            ),
        )

    def _fetch_all(self, query: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL(cast(LiteralString, query)), params)
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_audit_persistence_unavailable") from error
        return rows

    def _execute(
        self,
        query: str,
        params: tuple[object, ...] = (),
        *,
        transaction_name: str,
        reconcile_ambiguous_result: Callable[[Any], bool | None] | None = None,
    ) -> None:
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name=transaction_name,
                transaction_callback=lambda cursor: self._execute_transaction(
                    cursor=cursor, query=query, params=params
                ),
                reconcile_ambiguous_result=reconcile_ambiguous_result,  # type: ignore[arg-type]
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_audit_persistence_unavailable") from error

    def _execute_transaction(
        self, *, cursor: object, query: str, params: tuple[object, ...]
    ) -> bool:
        cursor.execute(sql.SQL(cast(LiteralString, query)), params)
        return True

    def _reconcile_lifecycle_result(
        self, *, connection: object, event: DocumentLifecycleAuditEvidence
    ) -> bool | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM document_ai_lifecycle_audit_evidence
                WHERE audit_evidence_id = %s
                """,
                (event["audit_evidence_id"],),
            )
            row = cursor.fetchone()
        return True if row is not None else None

    def _reconcile_lifecycle_clear(self, *, connection: object) -> bool | None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM document_ai_lifecycle_audit_evidence LIMIT 1")
            row = cursor.fetchone()
        return True if row is None else None

    def _reconcile_compliance_result(
        self, *, connection: object, event: DocumentComplianceOverrideAuditEvidence
    ) -> bool | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM document_ai_compliance_override_audit_evidence
                WHERE audit_evidence_id = %s
                """,
                (event["audit_evidence_id"],),
            )
            row = cursor.fetchone()
        return True if row is not None else None

    def _reconcile_compliance_clear(self, *, connection: object) -> bool | None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM document_ai_compliance_override_audit_evidence LIMIT 1")
            row = cursor.fetchone()
        return True if row is None else None


_document_audit_backend: DocumentAuditBackend = InMemoryDocumentAuditBackend()


def configure_document_audit_backend(backend: DocumentAuditBackend) -> None:
    """Configure the document audit backend for the active runtime."""

    global _document_audit_backend
    _document_audit_backend = backend


def emit_document_lifecycle_audit_evidence(
    *,
    action: LifecycleActionName,
    action_status: LifecycleActionStatus,
    document_id: UUID,
    previous_state: str | None,
    new_state: str | None,
    tenant_id: str,
    user_id: UUID,
    reason_code: str | None,
    trace_id: str,
    correlation_id: str,
) -> DocumentLifecycleAuditEvidence:
    """Append one deterministic audit evidence record for lifecycle action outcome."""

    canonical_identity = _canonical_identity(
        action=action,
        action_status=action_status,
        document_id=str(document_id),
        previous_state=previous_state,
        new_state=new_state,
        tenant_id=tenant_id,
        user_id=str(user_id),
        reason_code=reason_code,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    audit_evidence: DocumentLifecycleAuditEvidence = {
        "audit_evidence_id": _sha256_hex(canonical_json_dumps(canonical_identity)),
        "event_time": _deterministic_event_time(canonical_identity),
        "action": action,
        "action_status": action_status,
        "document_id": str(document_id),
        "previous_state": previous_state,
        "new_state": new_state,
        "tenant_id": tenant_id,
        "user_id": str(user_id),
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
    }
    return _document_audit_backend.append_lifecycle(audit_evidence)


def list_document_lifecycle_audit_evidence(
    *,
    correlation_id: str | None = None,
    document_id: str | None = None,
) -> list[DocumentLifecycleAuditEvidence]:
    """Return emitted lifecycle audit evidence records with optional filtering."""

    return _document_audit_backend.list_lifecycle(
        correlation_id=correlation_id,
        document_id=document_id,
    )


def clear_document_lifecycle_audit_evidence() -> None:
    """Reset in-memory lifecycle audit evidence for deterministic test isolation."""

    _document_audit_backend.clear_lifecycle()


class DocumentComplianceOverrideAuditEvidence(TypedDict):
    """Represent one immutable compliance-override audit evidence record."""

    audit_evidence_id: str
    event_time: str
    override_id: str
    event_type: ComplianceOverrideEventType
    event_status: ComplianceOverrideEventStatus
    document_id: str
    requested_action: ComplianceOverrideAction
    tenant_id: str
    actor_user_id: str
    actor_role: str
    reason_code: str | None
    state_before: str | None
    state_after: str | None
    trace_id: str
    correlation_id: str


def emit_document_compliance_override_audit_evidence(
    *,
    override_id: str,
    event_type: ComplianceOverrideEventType,
    event_status: ComplianceOverrideEventStatus,
    document_id: UUID,
    requested_action: ComplianceOverrideAction,
    tenant_id: str,
    actor_user_id: UUID,
    actor_role: str,
    reason_code: str | None,
    state_before: str | None,
    state_after: str | None,
    trace_id: str,
    correlation_id: str,
) -> DocumentComplianceOverrideAuditEvidence:
    """Append one deterministic audit evidence record for compliance override events."""

    canonical_identity = _canonical_override_identity(
        override_id=override_id,
        event_type=event_type,
        event_status=event_status,
        document_id=str(document_id),
        requested_action=requested_action,
        tenant_id=tenant_id,
        actor_user_id=str(actor_user_id),
        actor_role=actor_role,
        reason_code=reason_code,
        state_before=state_before,
        state_after=state_after,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    audit_evidence: DocumentComplianceOverrideAuditEvidence = {
        "audit_evidence_id": _sha256_hex(canonical_json_dumps(canonical_identity)),
        "event_time": _deterministic_event_time(canonical_identity),
        "override_id": override_id,
        "event_type": event_type,
        "event_status": event_status,
        "document_id": str(document_id),
        "requested_action": requested_action,
        "tenant_id": tenant_id,
        "actor_user_id": str(actor_user_id),
        "actor_role": actor_role,
        "reason_code": reason_code,
        "state_before": state_before,
        "state_after": state_after,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
    }
    return _document_audit_backend.append_compliance_override(audit_evidence)


def list_document_compliance_override_audit_evidence(
    *,
    correlation_id: str | None = None,
    document_id: str | None = None,
    override_id: str | None = None,
) -> list[DocumentComplianceOverrideAuditEvidence]:
    """Return emitted compliance-override audit evidence with optional filtering."""

    return _document_audit_backend.list_compliance_override(
        correlation_id=correlation_id,
        document_id=document_id,
        override_id=override_id,
    )


def clear_document_compliance_override_audit_evidence() -> None:
    """Reset in-memory compliance-override audit evidence for deterministic tests."""

    _document_audit_backend.clear_compliance_override()


def _canonical_identity(
    *,
    action: LifecycleActionName,
    action_status: LifecycleActionStatus,
    document_id: str,
    previous_state: str | None,
    new_state: str | None,
    tenant_id: str,
    user_id: str,
    reason_code: str | None,
    trace_id: str,
    correlation_id: str,
) -> dict[str, object]:
    return json.loads(
        canonical_json_dumps(
            {
                "scope": "document_ai_lifecycle_audit_evidence",
                "action": action,
                "action_status": action_status,
                "document_id": document_id,
                "previous_state": previous_state,
                "new_state": new_state,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "reason_code": reason_code,
                "trace_id": trace_id,
                "correlation_id": correlation_id,
            }
        )
    )


def _canonical_override_identity(
    *,
    override_id: str,
    event_type: ComplianceOverrideEventType,
    event_status: ComplianceOverrideEventStatus,
    document_id: str,
    requested_action: ComplianceOverrideAction,
    tenant_id: str,
    actor_user_id: str,
    actor_role: str,
    reason_code: str | None,
    state_before: str | None,
    state_after: str | None,
    trace_id: str,
    correlation_id: str,
) -> dict[str, object]:
    return json.loads(
        canonical_json_dumps(
            {
                "scope": "document_ai_compliance_override_audit_evidence",
                "override_id": override_id,
                "event_type": event_type,
                "event_status": event_status,
                "document_id": document_id,
                "requested_action": requested_action,
                "tenant_id": tenant_id,
                "actor_user_id": actor_user_id,
                "actor_role": actor_role,
                "reason_code": reason_code,
                "state_before": state_before,
                "state_after": state_after,
                "trace_id": trace_id,
                "correlation_id": correlation_id,
            }
        )
    )


def _deterministic_event_time(canonical_identity: dict[str, object]) -> str:
    digest = _sha256_hex(canonical_json_dumps(canonical_identity))
    offset_seconds = int(digest[:8], 16) % (365 * 24 * 60 * 60)
    return (_AUDIT_TIME_BASE + timedelta(seconds=offset_seconds)).isoformat()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(UTC)
