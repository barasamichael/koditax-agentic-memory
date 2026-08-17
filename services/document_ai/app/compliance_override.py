"""Deterministic dual-control compliance-lock override workflow."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Literal
from typing import Protocol
from typing import TypedDict
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import psycopg
from pydantic import Field
from pydantic import BaseModel

from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.redaction import redact_sensitive_fields
from services.document_ai.app.document_audit import emit_document_compliance_override_audit_evidence
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction

ComplianceOverrideRequestedAction = Literal[
    "trash",
    "restore",
    "mark_eligible_for_purge",
    "execute_purge",
]
ComplianceOverrideState = Literal["requested", "approved", "rejected", "expired", "consumed"]

_REQUESTER_ALLOWED_ROLES: frozenset[str] = frozenset({"IndividualTaxpayer"})
_APPROVER_ALLOWED_ROLES: frozenset[str] = frozenset({"ComplianceOfficer"})
COMPLIANCE_OVERRIDE_TTL_MINUTES = 15


class ComplianceOverrideRequestPayload(BaseModel):
    """Represent override-request payload."""

    requested_action: ComplianceOverrideRequestedAction
    justification: str = Field(min_length=1, max_length=2000)


class ComplianceOverrideDecisionPayload(BaseModel):
    """Represent override approval/rejection decision payload."""

    requested_action: ComplianceOverrideRequestedAction
    justification: str = Field(min_length=1, max_length=2000)


class ComplianceOverrideRecord(BaseModel):
    """Represent one deterministic compliance override record."""

    override_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: UUID
    requested_action: ComplianceOverrideRequestedAction
    tenant_id: str = Field(min_length=1, max_length=120)
    requested_by_user_id: UUID
    requested_by_role: str = Field(min_length=1, max_length=120)
    justification: str = Field(min_length=1, max_length=2000)
    status: ComplianceOverrideState
    created_at: str
    expires_at: str
    approved_by_user_id: UUID | None = None
    approved_by_role: str | None = None
    approved_at: str | None = None
    rejected_by_user_id: UUID | None = None
    rejected_by_role: str | None = None
    rejected_at: str | None = None
    consumed_by_user_id: UUID | None = None
    consumed_at: str | None = None


class ComplianceOverrideResponseEnvelope(BaseModel):
    """Represent canonical override workflow response envelope."""

    status: Literal[
        "compliance_override_requested",
        "compliance_override_approved",
        "compliance_override_rejected",
        "compliance_override_consumed",
    ]
    override: ComplianceOverrideRecord
    traceability: dict[str, str]


class ComplianceOverrideError(ValueError):
    """Represent deterministic compliance override workflow rejection."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.details = redact_sensitive_fields(details if details is not None else {})


class ComplianceOverrideStoreRecord(TypedDict):
    """Represent deterministic store payload for one override record."""

    override_id: str
    response_payload: dict[str, object]


class ComplianceOverrideStoreProtocol(Protocol):
    """Define deterministic compliance override store contract."""

    def get(self, override_id: str) -> ComplianceOverrideStoreRecord | None:
        """Get one override record by override ID."""

        ...

    def set(self, record: ComplianceOverrideStoreRecord) -> None:
        """Persist one override record."""

        ...

    def clear(self) -> None:
        """Clear stored override records."""

        ...


class InMemoryComplianceOverrideStore:
    """Provide deterministic in-memory compliance override store."""

    def __init__(self) -> None:
        self._records: dict[str, ComplianceOverrideStoreRecord] = {}

    def get(self, override_id: str) -> ComplianceOverrideStoreRecord | None:
        return self._records.get(override_id)

    def set(self, record: ComplianceOverrideStoreRecord) -> None:
        self._records[record["override_id"]] = record

    def clear(self) -> None:
        self._records.clear()


class PersistentComplianceOverrideStore:
    """Persist compliance override workflow state to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get(self, override_id: str) -> ComplianceOverrideStoreRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            override_id,
                            tenant_id,
                            document_id,
                            requested_action,
                            requested_by_user_id,
                            requested_by_role,
                            justification,
                            status,
                            created_at,
                            expires_at,
                            approved_by_user_id,
                            approved_by_role,
                            approved_at,
                            rejected_by_user_id,
                            rejected_by_role,
                            rejected_at,
                            consumed_by_user_id,
                            consumed_at,
                            response_payload
                        FROM document_ai_compliance_overrides
                        WHERE override_id = %s
                        """,
                        (override_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_compliance_override_persistence_unavailable") from error
        if row is None:
            return None
        return ComplianceOverrideStoreRecord(
            override_id=str(row[0]),
            response_payload=dict(row[18]),
        )

    def set(self, record: ComplianceOverrideStoreRecord) -> None:
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.compliance_override.set",
                transaction_callback=lambda cursor: self._set_transaction(
                    cursor=cursor, record=record
                ),
                reconcile_ambiguous_result=lambda connection: self._reconcile_set_result(
                    connection=connection, record=record
                ),
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_compliance_override_persistence_unavailable") from error

    def _set_transaction(self, *, cursor: object, record: ComplianceOverrideStoreRecord) -> str:
        payload = record["response_payload"]
        cursor.execute(
            """
            INSERT INTO document_ai_compliance_overrides (
                override_id,
                tenant_id,
                document_id,
                requested_action,
                requested_by_user_id,
                requested_by_role,
                justification,
                status,
                created_at,
                expires_at,
                approved_by_user_id,
                approved_by_role,
                approved_at,
                rejected_by_user_id,
                rejected_by_role,
                rejected_at,
                consumed_by_user_id,
                consumed_at,
                response_payload
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (override_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                document_id = EXCLUDED.document_id,
                requested_action = EXCLUDED.requested_action,
                requested_by_user_id = EXCLUDED.requested_by_user_id,
                requested_by_role = EXCLUDED.requested_by_role,
                justification = EXCLUDED.justification,
                status = EXCLUDED.status,
                created_at = EXCLUDED.created_at,
                expires_at = EXCLUDED.expires_at,
                approved_by_user_id = EXCLUDED.approved_by_user_id,
                approved_by_role = EXCLUDED.approved_by_role,
                approved_at = EXCLUDED.approved_at,
                rejected_by_user_id = EXCLUDED.rejected_by_user_id,
                rejected_by_role = EXCLUDED.rejected_by_role,
                rejected_at = EXCLUDED.rejected_at,
                consumed_by_user_id = EXCLUDED.consumed_by_user_id,
                consumed_at = EXCLUDED.consumed_at,
                response_payload = EXCLUDED.response_payload,
                updated_at = now()
            """,
            (
                record["override_id"],
                payload["tenant_id"],
                payload["document_id"],
                payload["requested_action"],
                str(payload["requested_by_user_id"]),
                payload["requested_by_role"],
                payload["justification"],
                payload["status"],
                payload["created_at"],
                payload["expires_at"],
                None if payload.get("approved_by_user_id") is None else str(payload["approved_by_user_id"]),
                payload.get("approved_by_role"),
                payload.get("approved_at"),
                None if payload.get("rejected_by_user_id") is None else str(payload["rejected_by_user_id"]),
                payload.get("rejected_by_role"),
                payload.get("rejected_at"),
                None if payload.get("consumed_by_user_id") is None else str(payload["consumed_by_user_id"]),
                payload.get("consumed_at"),
                json.dumps(record["response_payload"], sort_keys=True),
            ),
        )
        return record["override_id"]

    def _reconcile_set_result(
        self, *, connection: object, record: ComplianceOverrideStoreRecord
    ) -> str | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT response_payload
                FROM document_ai_compliance_overrides
                WHERE override_id = %s
                """,
                (record["override_id"],),
            )
            row = cursor.fetchone()
        if row is None or dict(row[0]) != record["response_payload"]:
            return None
        return record["override_id"]

    def clear(self) -> None:
        try:
            execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.compliance_override.clear",
                transaction_callback=lambda cursor: self._clear_transaction(cursor=cursor),
                reconcile_ambiguous_result=lambda connection: self._reconcile_clear_result(
                    connection=connection
                ),
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_compliance_override_persistence_unavailable") from error

    def _clear_transaction(self, *, cursor: object) -> bool:
        cursor.execute("DELETE FROM document_ai_compliance_overrides")
        return True

    def _reconcile_clear_result(self, *, connection: object) -> bool | None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM document_ai_compliance_overrides LIMIT 1")
            row = cursor.fetchone()
        return True if row is None else None


_DEFAULT_COMPLIANCE_OVERRIDE_STORE = InMemoryComplianceOverrideStore()


def get_default_compliance_override_store() -> InMemoryComplianceOverrideStore:
    """Return default compliance override store."""

    return _DEFAULT_COMPLIANCE_OVERRIDE_STORE


def reset_default_compliance_override_store() -> None:
    """Reset default compliance override store for test isolation."""

    _DEFAULT_COMPLIANCE_OVERRIDE_STORE.clear()


def request_compliance_override(
    *,
    document_id: UUID,
    requested_action: ComplianceOverrideRequestedAction,
    justification: str,
    tenant_id: str,
    requester_user_id: UUID,
    requester_role: str,
    correlation_id: str,
    trace_id: str,
    compliance_override_store: ComplianceOverrideStoreProtocol,
    now_utc: datetime | None = None,
) -> ComplianceOverrideResponseEnvelope:
    """Create deterministic compliance override request."""

    if requester_role not in _REQUESTER_ALLOWED_ROLES:
        raise ComplianceOverrideError(
            reason="compliance_override_not_authorized",
            message="Compliance override action is not authorized for actor role.",
            details={"actor_role": requester_role},
        )
    override_id = _build_override_id(
        document_id=document_id,
        requested_action=requested_action,
        requester_user_id=requester_user_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
    )
    existing = compliance_override_store.get(override_id)
    if existing is not None:
        replay_record = ComplianceOverrideRecord.model_validate(existing["response_payload"])
        return ComplianceOverrideResponseEnvelope(
            status="compliance_override_requested",
            override=replay_record,
            traceability={"trace_id": trace_id, "correlation_id": correlation_id},
        )

    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    created_at = _to_iso_utc(reference_now)
    expires_at = _to_iso_utc(
        reference_now.replace(microsecond=0) + timedelta(minutes=COMPLIANCE_OVERRIDE_TTL_MINUTES)
    )
    record = ComplianceOverrideRecord(
        override_id=override_id,
        document_id=document_id,
        requested_action=requested_action,
        tenant_id=tenant_id,
        requested_by_user_id=requester_user_id,
        requested_by_role=requester_role,
        justification=justification,
        status="requested",
        created_at=created_at,
        expires_at=expires_at,
    )
    compliance_override_store.set(
        ComplianceOverrideStoreRecord(
            override_id=override_id,
            response_payload=record.model_dump(mode="json"),
        )
    )
    emit_document_compliance_override_audit_evidence(
        override_id=override_id,
        event_type="request",
        event_status="success",
        document_id=document_id,
        requested_action=requested_action,
        tenant_id=tenant_id,
        actor_user_id=requester_user_id,
        actor_role=requester_role,
        reason_code=None,
        state_before=None,
        state_after="requested",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    return ComplianceOverrideResponseEnvelope(
        status="compliance_override_requested",
        override=record,
        traceability={"trace_id": trace_id, "correlation_id": correlation_id},
    )


def approve_compliance_override(
    *,
    override_id: str,
    document_id: UUID,
    requested_action: ComplianceOverrideRequestedAction,
    tenant_id: str,
    approver_user_id: UUID,
    approver_role: str,
    correlation_id: str,
    trace_id: str,
    compliance_override_store: ComplianceOverrideStoreProtocol,
    now_utc: datetime | None = None,
) -> ComplianceOverrideResponseEnvelope:
    """Approve compliance override by independent authorized approver."""

    if approver_role not in _APPROVER_ALLOWED_ROLES:
        raise ComplianceOverrideError(
            reason="compliance_override_not_authorized",
            message="Compliance override action is not authorized for actor role.",
            details={"actor_role": approver_role},
        )
    existing = _get_override_record_or_raise(
        override_id=override_id,
        compliance_override_store=compliance_override_store,
    )
    _enforce_scope_match(
        record=existing,
        document_id=document_id,
        requested_action=requested_action,
    )
    _enforce_tenant_match(record=existing, tenant_id=tenant_id)
    if existing.requested_by_user_id == approver_user_id:
        raise ComplianceOverrideError(
            reason="compliance_override_self_approval_forbidden",
            message="Requester cannot approve own compliance override request.",
            details={"override_id": override_id},
        )
    _enforce_pending_state_or_raise(existing)
    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    if _parse_iso_utc(existing.expires_at) < reference_now:
        expired = existing.model_copy(update={"status": "expired"})
        _store_record(expired, compliance_override_store=compliance_override_store)
        emit_document_compliance_override_audit_evidence(
            override_id=override_id,
            event_type="expire",
            event_status="success",
            document_id=existing.document_id,
            requested_action=existing.requested_action,
            tenant_id=existing.tenant_id,
            actor_user_id=approver_user_id,
            actor_role=approver_role,
            reason_code="compliance_override_expired",
            state_before=existing.status,
            state_after="expired",
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise ComplianceOverrideError(
            reason="compliance_override_expired",
            message="Compliance override request has expired.",
            details={"override_id": override_id},
        )
    approved = existing.model_copy(
        update={
            "status": "approved",
            "approved_by_user_id": approver_user_id,
            "approved_by_role": approver_role,
            "approved_at": _to_iso_utc(reference_now),
        }
    )
    _store_record(approved, compliance_override_store=compliance_override_store)
    emit_document_compliance_override_audit_evidence(
        override_id=override_id,
        event_type="approve",
        event_status="success",
        document_id=existing.document_id,
        requested_action=existing.requested_action,
        tenant_id=existing.tenant_id,
        actor_user_id=approver_user_id,
        actor_role=approver_role,
        reason_code=None,
        state_before=existing.status,
        state_after="approved",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    return ComplianceOverrideResponseEnvelope(
        status="compliance_override_approved",
        override=approved,
        traceability={"trace_id": trace_id, "correlation_id": correlation_id},
    )


def reject_compliance_override(
    *,
    override_id: str,
    document_id: UUID,
    requested_action: ComplianceOverrideRequestedAction,
    tenant_id: str,
    approver_user_id: UUID,
    approver_role: str,
    correlation_id: str,
    trace_id: str,
    compliance_override_store: ComplianceOverrideStoreProtocol,
    now_utc: datetime | None = None,
) -> ComplianceOverrideResponseEnvelope:
    """Reject pending compliance override by independent authorized approver."""

    if approver_role not in _APPROVER_ALLOWED_ROLES:
        raise ComplianceOverrideError(
            reason="compliance_override_not_authorized",
            message="Compliance override action is not authorized for actor role.",
            details={"actor_role": approver_role},
        )
    existing = _get_override_record_or_raise(
        override_id=override_id,
        compliance_override_store=compliance_override_store,
    )
    _enforce_scope_match(
        record=existing,
        document_id=document_id,
        requested_action=requested_action,
    )
    _enforce_tenant_match(record=existing, tenant_id=tenant_id)
    if existing.requested_by_user_id == approver_user_id:
        raise ComplianceOverrideError(
            reason="compliance_override_self_approval_forbidden",
            message="Requester cannot approve own compliance override request.",
            details={"override_id": override_id},
        )
    _enforce_pending_state_or_raise(existing)
    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    if _parse_iso_utc(existing.expires_at) < reference_now:
        expired = existing.model_copy(update={"status": "expired"})
        _store_record(expired, compliance_override_store=compliance_override_store)
        emit_document_compliance_override_audit_evidence(
            override_id=override_id,
            event_type="expire",
            event_status="success",
            document_id=existing.document_id,
            requested_action=existing.requested_action,
            tenant_id=existing.tenant_id,
            actor_user_id=approver_user_id,
            actor_role=approver_role,
            reason_code="compliance_override_expired",
            state_before=existing.status,
            state_after="expired",
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise ComplianceOverrideError(
            reason="compliance_override_expired",
            message="Compliance override request has expired.",
            details={"override_id": override_id},
        )
    rejected = existing.model_copy(
        update={
            "status": "rejected",
            "rejected_by_user_id": approver_user_id,
            "rejected_by_role": approver_role,
            "rejected_at": _to_iso_utc(reference_now),
        }
    )
    _store_record(rejected, compliance_override_store=compliance_override_store)
    emit_document_compliance_override_audit_evidence(
        override_id=override_id,
        event_type="reject",
        event_status="success",
        document_id=existing.document_id,
        requested_action=existing.requested_action,
        tenant_id=existing.tenant_id,
        actor_user_id=approver_user_id,
        actor_role=approver_role,
        reason_code=None,
        state_before=existing.status,
        state_after="rejected",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    return ComplianceOverrideResponseEnvelope(
        status="compliance_override_rejected",
        override=rejected,
        traceability={"trace_id": trace_id, "correlation_id": correlation_id},
    )


def consume_compliance_override_for_action(
    *,
    override_id: str,
    document_id: UUID,
    requested_action: ComplianceOverrideRequestedAction,
    actor_user_id: UUID,
    tenant_id: str,
    correlation_id: str,
    trace_id: str,
    compliance_override_store: ComplianceOverrideStoreProtocol,
    now_utc: datetime | None = None,
) -> ComplianceOverrideRecord:
    """Consume approved override grant for one scoped locked action (single-use)."""

    existing = _get_override_record_or_raise(
        override_id=override_id,
        compliance_override_store=compliance_override_store,
    )
    _enforce_scope_match(
        record=existing,
        document_id=document_id,
        requested_action=requested_action,
    )
    if existing.tenant_id != tenant_id or existing.requested_by_user_id != actor_user_id:
        raise ComplianceOverrideError(
            reason="compliance_override_scope_mismatch",
            message="Compliance override scope does not match requested action context.",
            details={
                "override_id": override_id,
                "tenant_id": tenant_id,
                "requested_by_user_id": str(actor_user_id),
            },
        )
    if existing.status != "approved":
        raise ComplianceOverrideError(
            reason="compliance_override_invalid_state",
            message="Compliance override is not in a usable state.",
            details={"override_id": override_id, "status": existing.status},
        )
    reference_now = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
    if _parse_iso_utc(existing.expires_at) < reference_now:
        expired = existing.model_copy(update={"status": "expired"})
        _store_record(expired, compliance_override_store=compliance_override_store)
        emit_document_compliance_override_audit_evidence(
            override_id=override_id,
            event_type="expire",
            event_status="success",
            document_id=existing.document_id,
            requested_action=existing.requested_action,
            tenant_id=existing.tenant_id,
            actor_user_id=actor_user_id,
            actor_role=existing.requested_by_role,
            reason_code="compliance_override_expired",
            state_before=existing.status,
            state_after="expired",
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        raise ComplianceOverrideError(
            reason="compliance_override_expired",
            message="Compliance override request has expired.",
            details={"override_id": override_id},
        )
    consumed = existing.model_copy(
        update={
            "status": "consumed",
            "consumed_by_user_id": actor_user_id,
            "consumed_at": _to_iso_utc(reference_now),
        }
    )
    _store_record(consumed, compliance_override_store=compliance_override_store)
    emit_document_compliance_override_audit_evidence(
        override_id=override_id,
        event_type="use",
        event_status="success",
        document_id=existing.document_id,
        requested_action=existing.requested_action,
        tenant_id=existing.tenant_id,
        actor_user_id=actor_user_id,
        actor_role=existing.requested_by_role,
        reason_code=None,
        state_before=existing.status,
        state_after="consumed",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    return consumed


def _build_override_id(
    *,
    document_id: UUID,
    requested_action: ComplianceOverrideRequestedAction,
    requester_user_id: UUID,
    tenant_id: str,
    correlation_id: str,
) -> str:
    return compute_canonical_hash(
        {
            "scope": "document_ai_compliance_override",
            "document_id": str(document_id),
            "requested_action": requested_action,
            "requester_user_id": str(requester_user_id),
            "tenant_id": tenant_id,
            "correlation_id": correlation_id,
        }
    ).sha256_hex


def _get_override_record_or_raise(
    *,
    override_id: str,
    compliance_override_store: ComplianceOverrideStoreProtocol,
) -> ComplianceOverrideRecord:
    existing = compliance_override_store.get(override_id)
    if existing is None:
        raise ComplianceOverrideError(
            reason="compliance_override_invalid_state",
            message="Compliance override is not in a usable state.",
            details={"override_id": override_id},
        )
    return ComplianceOverrideRecord.model_validate(existing["response_payload"])


def _store_record(
    record: ComplianceOverrideRecord,
    *,
    compliance_override_store: ComplianceOverrideStoreProtocol,
) -> None:
    compliance_override_store.set(
        ComplianceOverrideStoreRecord(
            override_id=record.override_id,
            response_payload=record.model_dump(mode="json"),
        )
    )


def _enforce_scope_match(
    *,
    record: ComplianceOverrideRecord,
    document_id: UUID,
    requested_action: ComplianceOverrideRequestedAction,
) -> None:
    if record.document_id != document_id or record.requested_action != requested_action:
        raise ComplianceOverrideError(
            reason="compliance_override_scope_mismatch",
            message="Compliance override scope does not match requested action context.",
            details={
                "override_id": record.override_id,
                "document_id": str(document_id),
                "requested_action": requested_action,
            },
        )


def _enforce_tenant_match(
    *,
    record: ComplianceOverrideRecord,
    tenant_id: str,
) -> None:
    if record.tenant_id != tenant_id:
        raise ComplianceOverrideError(
            reason="compliance_override_scope_mismatch",
            message="Compliance override scope does not match requested action context.",
            details={
                "override_id": record.override_id,
                "tenant_id": tenant_id,
            },
        )


def _enforce_pending_state_or_raise(record: ComplianceOverrideRecord) -> None:
    if record.status != "requested":
        raise ComplianceOverrideError(
            reason="compliance_override_invalid_state",
            message="Compliance override is not in a usable state.",
            details={"override_id": record.override_id, "status": record.status},
        )


def _parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
