"""Deterministic self-service account deletion request and confirmation logic."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from typing import Literal
from typing import Protocol
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Lock
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel

from services.auth.app.config import get_account_deletion_cooldown_seconds
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import _build_tombstoned_phone_number
from services.auth.app.session_issuance import PersistentSessionIssuanceStore
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import AuthCockroachTransactionError
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import validate_auth_database_connection
from services.auth.app.persistence_support import AuthCockroachTransactionAmbiguousCommitError

DeletionState = Literal[
    "requested", "blocked", "confirmed", "cancelled", "executed"
]
ExecutionOutcome = Literal["tombstoned"]
NotificationChannel = Literal["email"]
NotificationStatus = Literal["queued", "sent", "failed_non_retryable"]
LifecycleAction = Literal[
    "account_deletion_request_created",
    "account_deletion_request_blocked",
    "account_deletion_request_confirmed",
    "account_deletion_request_cancelled",
    "account_deletion_request_executed",
]
LifecycleStatus = Literal[
    "created", "blocked", "confirmed", "cancelled", "executed"
]
PrecheckReasonCode = Literal[
    "deletion_blocked_compliance_lock",
    "deletion_blocked_legal_hold",
    "deletion_blocked_active_obligation",
    "deletion_blocked_retention_constraint",
]
_BLOCKER_REASON_ORDER: tuple[PrecheckReasonCode, ...] = (
    "deletion_blocked_compliance_lock",
    "deletion_blocked_legal_hold",
    "deletion_blocked_active_obligation",
    "deletion_blocked_retention_constraint",
)
AUTH_LOG_EVENT_ACCOUNT_DELETION = "auth.account_deletion"


class AccountDeletionRequestCreateRequest(BaseModel):
    """Represent account deletion request payload."""

    request_reason: str


class AccountDeletionConfirmRequest(BaseModel):
    """Represent deletion confirmation payload."""

    request_id: UUID
    reauth_proof: str
    otp_verification_id: UUID


class AccountDeletionRequestResponse(BaseModel):
    """Represent successful account deletion request response envelope."""

    status: Literal["accepted"]
    request_id: UUID
    deletion_state: Literal["requested", "blocked"]
    requested_at: str
    blockers: list[PrecheckReasonCode]


class AccountDeletionConfirmResponse(BaseModel):
    """Represent successful account deletion confirm response envelope."""

    status: Literal["confirmed"]
    request_id: UUID
    deletion_state: Literal["confirmed", "cooldown_active"]
    cooldown_expires_at: str


class AccountDeletionCancelRequest(BaseModel):
    """Represent deletion cancel payload."""

    request_id: UUID


class AccountDeletionCancelResponse(BaseModel):
    """Represent successful account deletion cancel response envelope."""

    status: Literal["cancelled"]
    request_id: UUID
    deletion_state: Literal["cancelled"]


class AccountDeletionExecuteRequest(BaseModel):
    """Represent deletion execution payload."""

    request_id: UUID


class AccountDeletionExecuteResponse(BaseModel):
    """Represent successful account deletion execution response envelope."""

    status: Literal["executed"]
    request_id: UUID
    deletion_state: Literal["executed"]
    execution_outcome: ExecutionOutcome
    executed_at: str
    revoked_session_count: int


@dataclass(frozen=True)
class AuthPrincipal:
    """Represent authenticated principal context for account deletion requests."""

    user_id: UUID
    tenant_id: str
    role: str


@dataclass(frozen=True)
class AccountDeletionRequestRecord:
    """Represent persisted account deletion request state."""

    request_id: UUID
    user_id: UUID
    tenant_id: str
    request_reason: str
    requested_at: str
    deletion_state: DeletionState
    blocker_reasons: tuple[PrecheckReasonCode, ...]
    request_idempotency_key: str
    confirmed_at: str | None
    cooldown_expires_at: str | None
    executed_at: str | None
    execution_outcome: ExecutionOutcome | None
    revoked_session_count: int | None


@dataclass(frozen=True)
class AccountDeletionAuditRecord:
    """Represent immutable request-creation audit evidence."""

    audit_evidence_id: str
    event_id: str
    event_type: LifecycleAction
    user_id: UUID
    request_id: UUID
    action: LifecycleAction
    action_status: LifecycleStatus
    deletion_state: DeletionState
    occurred_at: str
    correlation_id: str | None
    blocker_reasons: tuple[PrecheckReasonCode, ...]
    reason_code: str | None
    trace_ref: str
    created_at: str


@dataclass(frozen=True)
class AccountDeletionNotificationRecord:
    """Represent immutable notification emission outcome for one lifecycle transition."""

    notification_id: str
    request_id: UUID
    channel: NotificationChannel
    status: NotificationStatus
    attempted_at: str
    event_type: LifecycleAction
    user_id: UUID
    deletion_state: DeletionState
    correlation_id: str | None


@dataclass(frozen=True)
class AccountDeletionPrecheckContext:
    """Represent deterministic precheck inputs for deletion request blocking."""

    compliance_lock: bool = False
    legal_hold: bool = False
    active_obligation: bool = False
    retention_constraint: bool = False


@dataclass(frozen=True)
class AccountDeletionIncidentRecord:
    """Represent immutable operational incident evidence for deletion-risk paths."""

    audit_reference_id: str
    incident_code: str
    message: str
    reason: str
    request_id: UUID
    actor_user_id: UUID
    tenant_id: str
    account_deletion_state: str
    occurred_at: str
    correlation_id: str | None


@dataclass(frozen=True)
class _RequestIdempotencyRecord:
    """Represent deterministic idempotency replay record for deletion requests."""

    request_fingerprint: str
    response: AccountDeletionRequestResponse


@dataclass(frozen=True)
class _ConfirmIdempotencyRecord:
    """Represent deterministic idempotency replay record for deletion confirmation."""

    request_fingerprint: str
    response: AccountDeletionConfirmResponse


@dataclass(frozen=True)
class _CancelIdempotencyRecord:
    """Represent deterministic idempotency replay record for deletion cancellation."""

    request_fingerprint: str
    response: AccountDeletionCancelResponse


@dataclass(frozen=True)
class _ExecuteIdempotencyRecord:
    """Represent deterministic idempotency replay record for deletion execution."""

    request_fingerprint: str
    response: AccountDeletionExecuteResponse


@dataclass(frozen=True)
class _ReauthProofRecord:
    """Represent one persisted re-auth proof for deletion confirmation."""

    proof_id: str
    user_id: UUID
    tenant_id: str
    request_id: UUID
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class _OtpProofRecord:
    """Represent one persisted OTP verification proof for deletion confirmation."""

    otp_verification_id: UUID
    user_id: UUID
    tenant_id: str
    request_id: UUID
    expires_at: datetime
    consumed_at: datetime | None


class AccountDeletionRequestError(ValueError):
    """Represent deterministic account deletion request failures."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


class AccountDeletionRequestStoreProtocol(Protocol):
    """Define persistence boundary for deletion request state + audit evidence."""

    def create_or_replay_request(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_reason: str,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        idempotency_key: str,
        request_fingerprint: str,
        requested_at: str,
        correlation_id: str | None,
    ) -> AccountDeletionRequestResponse:
        """Create or replay a deletion request deterministically."""

        ...

    def create_or_replay_confirmation(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        reauth_proof: str,
        otp_verification_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionConfirmResponse:
        """Confirm one deletion request deterministically."""

        ...

    def create_or_replay_cancel(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionCancelResponse:
        """Cancel one deletion request deterministically."""

        ...

    def create_or_replay_execution(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
    ) -> AccountDeletionExecuteResponse:
        """Execute one deletion request deterministically."""

        ...

    def get_active_request_for_user(
        self, *, user_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        """Return active deletion request for one user when present."""

        ...

    def get_request_by_id(
        self, *, request_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        """Return deletion request by identifier when present."""

        ...

    def get_audit_events_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionAuditRecord]:
        """Return immutable deletion lifecycle audit events for one user."""

        ...

    def get_notification_records_for_user(
        self,
        *,
        user_id: UUID,
    ) -> list[AccountDeletionNotificationRecord]:
        """Return immutable notification outcome records for one user."""

        ...

    def get_incident_records_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionIncidentRecord]:
        """Return immutable incident evidence records for one user."""

        ...

    def evaluate_deletion_precheck_blockers(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
    ) -> tuple[PrecheckReasonCode, ...]:
        """Return deterministic ordered precheck blocker reasons for one user context."""

        ...

    def revoke_all_active_sessions_for_user(self, *, user_id: UUID) -> int:
        """Revoke all active sessions for one user and return deterministic count."""

        ...


class InMemoryAccountDeletionRequestStore:
    """Persist account deletion request state and confirmation proofs in memory."""

    def __init__(self) -> None:
        self._requests_by_id: dict[UUID, AccountDeletionRequestRecord] = {}
        self._active_request_id_by_user: dict[UUID, UUID] = {}
        self._request_idempotency_records: dict[
            str, _RequestIdempotencyRecord
        ] = {}
        self._confirm_idempotency_records: dict[
            str, _ConfirmIdempotencyRecord
        ] = {}
        self._cancel_idempotency_records: dict[
            str, _CancelIdempotencyRecord
        ] = {}
        self._execute_idempotency_records: dict[
            str, _ExecuteIdempotencyRecord
        ] = {}
        self._reauth_proofs_by_value: dict[str, _ReauthProofRecord] = {}
        self._otp_proofs_by_id: dict[UUID, _OtpProofRecord] = {}
        self._active_sessions_by_user: dict[UUID, set[str]] = {}
        self._audit_by_user: dict[UUID, list[AccountDeletionAuditRecord]] = {}
        self._notifications_by_user: dict[
            UUID, list[AccountDeletionNotificationRecord]
        ] = {}
        self._incidents_by_user: dict[
            UUID, list[AccountDeletionIncidentRecord]
        ] = {}
        self._precheck_context_by_identity: dict[
            tuple[UUID, str], AccountDeletionPrecheckContext
        ] = {}
        self._lock = Lock()

    def create_or_replay_request(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_reason: str,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        idempotency_key: str,
        request_fingerprint: str,
        requested_at: str,
        correlation_id: str | None,
    ) -> AccountDeletionRequestResponse:
        with self._lock:
            existing_idempotency_record = self._request_idempotency_records.get(
                idempotency_key
            )
            if existing_idempotency_record is not None:
                if (
                    existing_idempotency_record.request_fingerprint
                    != request_fingerprint
                ):
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message=(
                            "Idempotency key conflicts with an existing account deletion request."
                        ),
                        reason="idempotency_key_conflict",
                    )
                return existing_idempotency_record.response

            existing_request_id = self._active_request_id_by_user.get(user_id)
            existing_request = (
                None
                if existing_request_id is None
                else self._requests_by_id.get(existing_request_id)
            )
            if existing_request is not None:
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_request_already_active",
                    message="An active account deletion request already exists.",
                    reason="account_deletion_request_already_active",
                    details=self._build_request_state_details_locked(
                        request_record=existing_request,
                        requested_state="requested",
                    ),
                )

            request_id = uuid4()
            deletion_state: Literal["requested", "blocked"] = (
                "blocked" if blocker_reasons else "requested"
            )
            request_record = AccountDeletionRequestRecord(
                request_id=request_id,
                user_id=user_id,
                tenant_id=tenant_id,
                request_reason=request_reason,
                requested_at=requested_at,
                deletion_state=deletion_state,
                blocker_reasons=blocker_reasons,
                request_idempotency_key=idempotency_key,
                confirmed_at=None,
                cooldown_expires_at=None,
                executed_at=None,
                execution_outcome=None,
                revoked_session_count=None,
            )
            response = AccountDeletionRequestResponse(
                status="accepted",
                request_id=request_record.request_id,
                deletion_state=request_record.deletion_state,  # type: ignore
                requested_at=request_record.requested_at,
                blockers=[*blocker_reasons],
            )
            self._requests_by_id[request_id] = request_record
            self._active_request_id_by_user[user_id] = request_id
            self._request_idempotency_records[idempotency_key] = (
                _RequestIdempotencyRecord(
                    request_fingerprint=request_fingerprint,
                    response=response,
                )
            )
            self._emit_lifecycle_evidence_locked(
                user_id=user_id,
                request_id=request_id,
                trace_ref=request_fingerprint,
                occurred_at=requested_at,
                correlation_id=correlation_id,
                action=(
                    "account_deletion_request_blocked"
                    if blocker_reasons
                    else "account_deletion_request_created"
                ),
                action_status="blocked" if blocker_reasons else "created",
                deletion_state=deletion_state,
                blocker_reasons=blocker_reasons,
            )
            return response

    def create_or_replay_confirmation(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        reauth_proof: str,
        otp_verification_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionConfirmResponse:
        with self._lock:
            existing_idempotency_record = self._confirm_idempotency_records.get(
                idempotency_key
            )
            if existing_idempotency_record is not None:
                if (
                    existing_idempotency_record.request_fingerprint
                    != request_fingerprint
                ):
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message="Idempotency key conflicts with an existing deletion confirmation.",
                        reason="idempotency_key_conflict",
                    )
                return existing_idempotency_record.response

            request_record = self._requests_by_id.get(request_id)
            if request_record is None:
                raise AccountDeletionRequestError(
                    status_code=404,
                    error_code="account_deletion_confirm_request_not_found",
                    message="Account deletion request was not found.",
                    reason="account_deletion_confirm_request_not_found",
                )
            if (
                request_record.user_id != user_id
                or request_record.tenant_id != tenant_id
            ):
                raise AccountDeletionRequestError(
                    status_code=404,
                    error_code="account_deletion_confirm_request_not_found",
                    message="Account deletion request was not found.",
                    reason="account_deletion_confirm_request_not_found",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_malicious_takeover_attempt",
                        message="Malicious account deletion confirmation attempt was blocked.",
                        reason="account_deletion_confirm_request_not_found",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state="not_owned",
                        correlation_id=correlation_id,
                    ),
                )
            if request_record.deletion_state != "requested":
                details = self._build_request_state_details_locked(
                    request_record=request_record,
                    requested_state="confirmed",
                )
                if (
                    "deletion_blocked_legal_hold"
                    in request_record.blocker_reasons
                ):
                    details.update(
                        self._record_incident_and_build_details_locked(
                            incident_code="account_deletion_legal_hold_dispute",
                            message=(
                                "Legal-hold dispute is required before deletion may proceed."
                            ),
                            reason="account_deletion_confirm_invalid_state",
                            actor_user_id=user_id,
                            tenant_id=tenant_id,
                            request_id=request_id,
                            account_deletion_state=request_record.deletion_state,
                            correlation_id=correlation_id,
                        )
                    )
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_confirm_invalid_state",
                    message="Account deletion request is not in confirmable state.",
                    reason="account_deletion_confirm_invalid_state",
                    details=details,
                )

            now = datetime.now(UTC)
            self._validate_reauth_proof_locked(
                reauth_proof=reauth_proof,
                user_id=user_id,
                tenant_id=tenant_id,
                request_id=request_id,
                now=now,
            )
            self._validate_otp_proof_locked(
                otp_verification_id=otp_verification_id,
                user_id=user_id,
                tenant_id=tenant_id,
                request_id=request_id,
                now=now,
            )

            confirmed_at = _utc_iso(now)
            cooldown_expires_at = _utc_iso(
                now + timedelta(seconds=get_account_deletion_cooldown_seconds())
            )
            updated_request_record = AccountDeletionRequestRecord(
                request_id=request_record.request_id,
                user_id=request_record.user_id,
                tenant_id=request_record.tenant_id,
                request_reason=request_record.request_reason,
                requested_at=request_record.requested_at,
                deletion_state="confirmed",
                blocker_reasons=request_record.blocker_reasons,
                request_idempotency_key=request_record.request_idempotency_key,
                confirmed_at=confirmed_at,
                cooldown_expires_at=cooldown_expires_at,
                executed_at=request_record.executed_at,
                execution_outcome=request_record.execution_outcome,
                revoked_session_count=request_record.revoked_session_count,
            )
            self._requests_by_id[request_id] = updated_request_record
            self._consume_reauth_proof_locked(
                reauth_proof=reauth_proof, consumed_at=now
            )
            self._consume_otp_proof_locked(
                otp_verification_id=otp_verification_id,
                consumed_at=now,
            )

            response = AccountDeletionConfirmResponse(
                status="confirmed",
                request_id=request_id,
                deletion_state="cooldown_active",
                cooldown_expires_at=cooldown_expires_at,
            )
            self._confirm_idempotency_records[idempotency_key] = (
                _ConfirmIdempotencyRecord(
                    request_fingerprint=request_fingerprint,
                    response=response,
                )
            )
            self._emit_lifecycle_evidence_locked(
                user_id=user_id,
                request_id=request_id,
                trace_ref=request_fingerprint,
                occurred_at=confirmed_at,
                correlation_id=correlation_id,
                action="account_deletion_request_confirmed",
                action_status="confirmed",
                deletion_state="confirmed",
                blocker_reasons=(),
            )
            return response

    def create_or_replay_cancel(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionCancelResponse:
        with self._lock:
            existing_idempotency_record = self._cancel_idempotency_records.get(
                idempotency_key
            )
            if existing_idempotency_record is not None:
                if (
                    existing_idempotency_record.request_fingerprint
                    != request_fingerprint
                ):
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message="Idempotency key conflicts with an existing deletion cancellation.",
                        reason="idempotency_key_conflict",
                    )
                return existing_idempotency_record.response

            request_record = self._requests_by_id.get(request_id)
            if request_record is None:
                raise AccountDeletionRequestError(
                    status_code=404,
                    error_code="account_deletion_cancel_request_not_found",
                    message="Account deletion request was not found.",
                    reason="account_deletion_cancel_request_not_found",
                )
            if (
                request_record.user_id != user_id
                or request_record.tenant_id != tenant_id
            ):
                raise AccountDeletionRequestError(
                    status_code=404,
                    error_code="account_deletion_cancel_request_not_found",
                    message="Account deletion request was not found.",
                    reason="account_deletion_cancel_request_not_found",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_malicious_takeover_attempt",
                        message="Malicious account deletion cancellation attempt was blocked.",
                        reason="account_deletion_cancel_request_not_found",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state="not_owned",
                        correlation_id=correlation_id,
                    ),
                )
            if request_record.deletion_state != "confirmed":
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_cancel_not_allowed_for_state",
                    message="Account deletion request is not in cancellable state.",
                    reason="account_deletion_cancel_not_allowed_for_state",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_erroneous_request",
                        message="Erroneous account deletion cancellation attempt was blocked.",
                        reason="account_deletion_cancel_not_allowed_for_state",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state=request_record.deletion_state,
                        correlation_id=correlation_id,
                        requested_state="cancelled",
                        include_request_state=True,
                    ),
                )
            if request_record.cooldown_expires_at is None:
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_cancel_not_allowed_for_state",
                    message="Account deletion request is not in cancellable state.",
                    reason="account_deletion_cancel_not_allowed_for_state",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_erroneous_request",
                        message="Erroneous account deletion cancellation attempt was blocked.",
                        reason="account_deletion_cancel_not_allowed_for_state",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state=request_record.deletion_state,
                        correlation_id=correlation_id,
                        requested_state="cancelled",
                        include_request_state=True,
                    ),
                )

            now = datetime.now(UTC)
            cooldown_expires_at = datetime.fromisoformat(
                request_record.cooldown_expires_at.replace("Z", "+00:00")
            )
            if now >= cooldown_expires_at:
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_cancel_cooldown_expired",
                    message="Account deletion request cooldown window has expired.",
                    reason="account_deletion_cancel_cooldown_expired",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_erroneous_request",
                        message="Erroneous cancellation attempt after cooldown expiry was blocked.",
                        reason="account_deletion_cancel_cooldown_expired",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state=request_record.deletion_state,
                        correlation_id=correlation_id,
                        requested_state="cancelled",
                        include_request_state=True,
                    ),
                )

            updated_request_record = AccountDeletionRequestRecord(
                request_id=request_record.request_id,
                user_id=request_record.user_id,
                tenant_id=request_record.tenant_id,
                request_reason=request_record.request_reason,
                requested_at=request_record.requested_at,
                deletion_state="cancelled",
                blocker_reasons=request_record.blocker_reasons,
                request_idempotency_key=request_record.request_idempotency_key,
                confirmed_at=request_record.confirmed_at,
                cooldown_expires_at=request_record.cooldown_expires_at,
                executed_at=request_record.executed_at,
                execution_outcome=request_record.execution_outcome,
                revoked_session_count=request_record.revoked_session_count,
            )
            self._requests_by_id[request_id] = updated_request_record
            self._active_request_id_by_user.pop(user_id, None)

            response = AccountDeletionCancelResponse(
                status="cancelled",
                request_id=request_id,
                deletion_state="cancelled",
            )
            self._cancel_idempotency_records[idempotency_key] = (
                _CancelIdempotencyRecord(
                    request_fingerprint=request_fingerprint,
                    response=response,
                )
            )
            self._emit_lifecycle_evidence_locked(
                user_id=user_id,
                request_id=request_id,
                trace_ref=request_fingerprint,
                occurred_at=_utc_iso(now),
                correlation_id=correlation_id,
                action="account_deletion_request_cancelled",
                action_status="cancelled",
                deletion_state="cancelled",
                blocker_reasons=(),
            )
            return response

    def create_or_replay_execution(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
    ) -> AccountDeletionExecuteResponse:
        with self._lock:
            existing_idempotency_record = self._execute_idempotency_records.get(
                idempotency_key
            )
            if existing_idempotency_record is not None:
                if (
                    existing_idempotency_record.request_fingerprint
                    != request_fingerprint
                ):
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message="Idempotency key conflicts with an existing deletion execution.",
                        reason="idempotency_key_conflict",
                    )
                return existing_idempotency_record.response

            request_record = self._requests_by_id.get(request_id)
            if request_record is None:
                raise AccountDeletionRequestError(
                    status_code=404,
                    error_code="account_deletion_execute_request_not_found",
                    message="Account deletion request was not found.",
                    reason="account_deletion_execute_request_not_found",
                )
            if (
                request_record.user_id != user_id
                or request_record.tenant_id != tenant_id
            ):
                raise AccountDeletionRequestError(
                    status_code=404,
                    error_code="account_deletion_execute_request_not_found",
                    message="Account deletion request was not found.",
                    reason="account_deletion_execute_request_not_found",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_malicious_takeover_attempt",
                        message="Malicious account deletion execution attempt was blocked.",
                        reason="account_deletion_execute_request_not_found",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state="not_owned",
                        correlation_id=correlation_id,
                    ),
                )
            if request_record.deletion_state == "executed":
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_execute_already_completed",
                    message="Account deletion execution was already completed.",
                    reason="account_deletion_execute_already_completed",
                    details=self._build_request_state_details_locked(
                        request_record=request_record,
                        requested_state="executed",
                    ),
                )
            if request_record.deletion_state != "confirmed":
                details = self._build_request_state_details_locked(
                    request_record=request_record,
                    requested_state="executed",
                )
                if (
                    "deletion_blocked_legal_hold"
                    in request_record.blocker_reasons
                ):
                    details.update(
                        self._record_incident_and_build_details_locked(
                            incident_code="account_deletion_legal_hold_dispute",
                            message=(
                                "Legal-hold dispute is required before deletion "
                                "execution may proceed."
                            ),
                            reason="account_deletion_execute_invalid_state",
                            actor_user_id=user_id,
                            tenant_id=tenant_id,
                            request_id=request_id,
                            account_deletion_state=request_record.deletion_state,
                            correlation_id=correlation_id,
                        )
                    )
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_execute_invalid_state",
                    message="Account deletion request is not in executable state.",
                    reason="account_deletion_execute_invalid_state",
                    details=details,
                )
            if request_record.cooldown_expires_at is None:
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_execute_invalid_state",
                    message="Account deletion request is not in executable state.",
                    reason="account_deletion_execute_invalid_state",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_erroneous_request",
                        message="Erroneous account deletion execution attempt was blocked.",
                        reason="account_deletion_execute_invalid_state",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state=request_record.deletion_state,
                        correlation_id=correlation_id,
                        requested_state="executed",
                        include_request_state=True,
                    ),
                )

            now = datetime.now(UTC)
            cooldown_expires_at = datetime.fromisoformat(
                request_record.cooldown_expires_at.replace("Z", "+00:00")
            )
            if now < cooldown_expires_at:
                raise AccountDeletionRequestError(
                    status_code=409,
                    error_code="account_deletion_execute_not_allowed",
                    message="Account deletion execution is not allowed before cooldown expires.",
                    reason="account_deletion_execute_not_allowed",
                    details=self._record_incident_and_build_details_locked(
                        incident_code="account_deletion_erroneous_request",
                        message="Erroneous account deletion execution during cooldown was blocked.",
                        reason="account_deletion_execute_not_allowed",
                        actor_user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_deletion_state=request_record.deletion_state,
                        correlation_id=correlation_id,
                        requested_state="executed",
                        include_request_state=True,
                        extra_details={
                            "reason_code": "deletion_cooldown_active"
                        },
                    ),
                )

            executed_at = _utc_iso(now)
            registration_store.tombstone_user_and_invalidate_credentials(
                user_id=user_id,
                tombstoned_at=executed_at,
            )
            revoked_session_count = (
                self._revoke_all_active_sessions_for_user_locked(
                    user_id=user_id
                )
            )
            updated_request_record = AccountDeletionRequestRecord(
                request_id=request_record.request_id,
                user_id=request_record.user_id,
                tenant_id=request_record.tenant_id,
                request_reason=request_record.request_reason,
                requested_at=request_record.requested_at,
                deletion_state="executed",
                blocker_reasons=request_record.blocker_reasons,
                request_idempotency_key=request_record.request_idempotency_key,
                confirmed_at=request_record.confirmed_at,
                cooldown_expires_at=request_record.cooldown_expires_at,
                executed_at=executed_at,
                execution_outcome="tombstoned",
                revoked_session_count=revoked_session_count,
            )
            self._requests_by_id[request_id] = updated_request_record
            self._active_request_id_by_user.pop(user_id, None)

            response = AccountDeletionExecuteResponse(
                status="executed",
                request_id=request_id,
                deletion_state="executed",
                execution_outcome="tombstoned",
                executed_at=executed_at,
                revoked_session_count=revoked_session_count,
            )
            self._execute_idempotency_records[idempotency_key] = (
                _ExecuteIdempotencyRecord(
                    request_fingerprint=request_fingerprint,
                    response=response,
                )
            )
            self._emit_lifecycle_evidence_locked(
                user_id=user_id,
                request_id=request_id,
                trace_ref=request_fingerprint,
                occurred_at=executed_at,
                correlation_id=correlation_id,
                action="account_deletion_request_executed",
                action_status="executed",
                deletion_state="executed",
                blocker_reasons=(),
                reason_code=None,
            )
            return response

    def get_active_request_for_user(
        self, *, user_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        with self._lock:
            request_id = self._active_request_id_by_user.get(user_id)
            if request_id is None:
                return None
            return self._requests_by_id[request_id]

    def get_request_by_id(
        self, *, request_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        """Return request by identifier for deterministic tests and handlers."""

        with self._lock:
            return self._requests_by_id.get(request_id)

    def evaluate_deletion_precheck_blockers(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
    ) -> tuple[PrecheckReasonCode, ...]:
        """Return deterministic ordered blocker reasons for one identity context."""

        with self._lock:
            context = self._precheck_context_by_identity.get(
                (user_id, tenant_id), AccountDeletionPrecheckContext()
            )
            blocker_map: dict[PrecheckReasonCode, bool] = {
                "deletion_blocked_compliance_lock": context.compliance_lock,
                "deletion_blocked_legal_hold": context.legal_hold,
                "deletion_blocked_active_obligation": context.active_obligation,
                "deletion_blocked_retention_constraint": context.retention_constraint,
            }
            blockers = [
                reason_code
                for reason_code in _BLOCKER_REASON_ORDER
                if blocker_map.get(reason_code, False)
            ]
            return tuple(blockers)  # type: ignore

    def revoke_all_active_sessions_for_user(self, *, user_id: UUID) -> int:
        """Revoke all active sessions for one user and return deterministic count."""

        with self._lock:
            return self._revoke_all_active_sessions_for_user_locked(
                user_id=user_id
            )

    def get_audit_events_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionAuditRecord]:
        """Return immutable audit evidence entries for one user."""

        with self._lock:
            existing = self._audit_by_user.get(user_id, [])
            return [*existing]

    def get_notification_records_for_user(
        self,
        *,
        user_id: UUID,
    ) -> list[AccountDeletionNotificationRecord]:
        """Return immutable notification evidence entries for one user."""

        with self._lock:
            existing = self._notifications_by_user.get(user_id, [])
            return [*existing]

    def get_incident_records_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionIncidentRecord]:
        """Return immutable incident evidence entries for one user."""

        with self._lock:
            existing = self._incidents_by_user.get(user_id, [])
            return [*existing]

    def issue_test_session_for_user(
        self, *, user_id: UUID, session_id: str | None = None
    ) -> str:
        """Issue deterministic active session for local tests only."""

        with self._lock:
            normalized_session_id = session_id or f"session:{uuid4()}"
            existing_sessions = self._active_sessions_by_user.get(
                user_id, set()
            )
            updated_sessions = {*existing_sessions, normalized_session_id}
            self._active_sessions_by_user[user_id] = updated_sessions
            return normalized_session_id

    def get_active_session_count_for_user(self, *, user_id: UUID) -> int:
        """Return active session count for one user for deterministic tests."""

        with self._lock:
            return len(self._active_sessions_by_user.get(user_id, set()))

    def is_session_active(self, *, session_id: str) -> bool:
        """Return whether one session identifier is currently active."""

        with self._lock:
            return any(
                session_id in session_ids
                for session_ids in self._active_sessions_by_user.values()
            )

    def set_test_precheck_context(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        compliance_lock: bool = False,
        legal_hold: bool = False,
        active_obligation: bool = False,
        retention_constraint: bool = False,
    ) -> None:
        """Set deterministic precheck context for local tests only."""

        with self._lock:
            self._precheck_context_by_identity[(user_id, tenant_id)] = (
                AccountDeletionPrecheckContext(
                    compliance_lock=compliance_lock,
                    legal_hold=legal_hold,
                    active_obligation=active_obligation,
                    retention_constraint=retention_constraint,
                )
            )

    def issue_test_reauth_proof(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        ttl_seconds: int = 180,
    ) -> str:
        """Issue deterministic re-auth proof for local tests only."""

        with self._lock:
            proof_id = f"reauth:{uuid4()}"
            expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
            self._reauth_proofs_by_value[proof_id] = _ReauthProofRecord(
                proof_id=proof_id,
                user_id=user_id,
                tenant_id=tenant_id,
                request_id=request_id,
                expires_at=expires_at,
                consumed_at=None,
            )
            return proof_id

    def issue_test_otp_verification_proof(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        ttl_seconds: int = 180,
    ) -> UUID:
        """Issue deterministic OTP verification proof for local tests only."""

        with self._lock:
            otp_verification_id = uuid4()
            expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
            self._otp_proofs_by_id[otp_verification_id] = _OtpProofRecord(
                otp_verification_id=otp_verification_id,
                user_id=user_id,
                tenant_id=tenant_id,
                request_id=request_id,
                expires_at=expires_at,
                consumed_at=None,
            )
            return otp_verification_id

    def force_request_cooldown_expired(self, *, request_id: UUID) -> None:
        """Force request cooldown to expired state for deterministic local tests only."""

        with self._lock:
            record = self._requests_by_id[request_id]
            self._requests_by_id[request_id] = AccountDeletionRequestRecord(
                request_id=record.request_id,
                user_id=record.user_id,
                tenant_id=record.tenant_id,
                request_reason=record.request_reason,
                requested_at=record.requested_at,
                deletion_state=record.deletion_state,
                blocker_reasons=record.blocker_reasons,
                request_idempotency_key=record.request_idempotency_key,
                confirmed_at=record.confirmed_at,
                cooldown_expires_at=_utc_iso(
                    datetime.now(UTC) - timedelta(seconds=1)
                ),
                executed_at=record.executed_at,
                execution_outcome=record.execution_outcome,
                revoked_session_count=record.revoked_session_count,
            )

    def _revoke_all_active_sessions_for_user_locked(
        self, *, user_id: UUID
    ) -> int:
        existing_sessions = self._active_sessions_by_user.get(user_id, set())
        revoked_count = len(existing_sessions)
        self._active_sessions_by_user[user_id] = set()
        return revoked_count

    def _build_request_state_details_locked(
        self,
        *,
        request_record: AccountDeletionRequestRecord,
        requested_state: str,
    ) -> dict[str, object]:
        details: dict[str, object] = {
            "current_state": request_record.deletion_state,
            "requested_state": requested_state,
            "account_deletion_state": request_record.deletion_state,
            "blocker_reasons": [*request_record.blocker_reasons],
        }
        latest_audit_reference_id = (
            self._latest_audit_reference_for_request_locked(
                user_id=request_record.user_id,
                request_id=request_record.request_id,
            )
        )
        if latest_audit_reference_id is not None:
            details["audit_reference_id"] = latest_audit_reference_id
        return details

    def _latest_audit_reference_for_request_locked(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
    ) -> str | None:
        existing = self._audit_by_user.get(user_id, [])
        for record in reversed(existing):
            if record.request_id == request_id:
                return record.audit_evidence_id
        return None

    def _record_incident_and_build_details_locked(
        self,
        *,
        incident_code: str,
        message: str,
        reason: str,
        actor_user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        account_deletion_state: str,
        correlation_id: str | None,
        requested_state: str | None = None,
        include_request_state: bool = False,
        extra_details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        audit_reference_id = self._append_incident_record_locked(
            incident_code=incident_code,
            message=message,
            reason=reason,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            request_id=request_id,
            account_deletion_state=account_deletion_state,
            correlation_id=correlation_id,
        )
        details: dict[str, object] = {
            "incident_code": incident_code,
            "account_deletion_state": account_deletion_state,
            "audit_reference_id": audit_reference_id,
        }
        if include_request_state:
            details["current_state"] = account_deletion_state
            if isinstance(requested_state, str):
                details["requested_state"] = requested_state
        if extra_details:
            details.update(extra_details)
        return details

    def _append_incident_record_locked(
        self,
        *,
        incident_code: str,
        message: str,
        reason: str,
        actor_user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        account_deletion_state: str,
        correlation_id: str | None,
    ) -> str:
        payload = (
            f"{incident_code}:{reason}:{actor_user_id}:{tenant_id}:{request_id}:"
            f"{account_deletion_state}:{correlation_id or ''}"
        )
        audit_reference_id = sha256(payload.encode("utf-8")).hexdigest()
        occurred_at = _utc_now_iso()
        record = AccountDeletionIncidentRecord(
            audit_reference_id=audit_reference_id,
            incident_code=incident_code,
            message=message,
            reason=reason,
            request_id=request_id,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            account_deletion_state=account_deletion_state,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
        )
        existing = self._incidents_by_user.get(actor_user_id, [])
        if any(
            entry.audit_reference_id == audit_reference_id for entry in existing
        ):
            return audit_reference_id
        self._incidents_by_user[actor_user_id] = [*existing, record]
        return audit_reference_id

    def _emit_lifecycle_evidence_locked(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
        trace_ref: str,
        occurred_at: str,
        correlation_id: str | None,
        action: LifecycleAction,
        action_status: LifecycleStatus,
        deletion_state: DeletionState,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        reason_code: str | None = None,
    ) -> None:
        try:
            self._append_audit_record(
                user_id=user_id,
                request_id=request_id,
                trace_ref=trace_ref,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                action=action,
                action_status=action_status,
                deletion_state=deletion_state,
                blocker_reasons=blocker_reasons,
                reason_code=reason_code,
            )
        except Exception as error:
            raise AccountDeletionRequestError(
                status_code=500,
                error_code="account_deletion_audit_emit_failed",
                message="Account deletion audit evidence emission failed.",
                reason="account_deletion_audit_emit_failed",
            ) from error

        try:
            self._append_notification_record_locked(
                user_id=user_id,
                request_id=request_id,
                action=action,
                deletion_state=deletion_state,
                correlation_id=correlation_id,
                attempted_at=occurred_at,
            )
        except AccountDeletionRequestError:
            raise
        except Exception as error:
            raise AccountDeletionRequestError(
                status_code=500,
                error_code="account_deletion_notification_emit_failed",
                message="Account deletion notification evidence emission failed.",
                reason="account_deletion_notification_emit_failed",
            ) from error

    def _append_notification_record_locked(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
        action: LifecycleAction,
        deletion_state: DeletionState,
        correlation_id: str | None,
        attempted_at: str,
    ) -> None:
        status = self._resolve_notification_status_for_action(
            action=action, deletion_state=deletion_state
        )
        notification_payload = (
            f"{request_id}:{action}:{status}:{attempted_at}:"
            f"{correlation_id or ''}:email"
        )
        notification_id = sha256(
            notification_payload.encode("utf-8")
        ).hexdigest()
        record = AccountDeletionNotificationRecord(
            notification_id=notification_id,
            request_id=request_id,
            channel="email",
            status=status,
            attempted_at=attempted_at,
            event_type=action,
            user_id=user_id,
            deletion_state=deletion_state,
            correlation_id=correlation_id,
        )
        existing = self._notifications_by_user.get(user_id, [])
        self._notifications_by_user[user_id] = [*existing, record]

    def _resolve_notification_status_for_action(
        self,
        *,
        action: LifecycleAction,
        deletion_state: DeletionState,
    ) -> NotificationStatus:
        if action not in {
            "account_deletion_request_created",
            "account_deletion_request_blocked",
            "account_deletion_request_confirmed",
            "account_deletion_request_cancelled",
            "account_deletion_request_executed",
        }:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_notification_not_allowed_for_state",
                message="Account deletion notification is not allowed for current lifecycle state.",
                reason="account_deletion_notification_not_allowed_for_state",
                details={
                    "current_state": deletion_state,
                    "requested_state": deletion_state,
                },
            )
        if action == "account_deletion_request_executed":
            return "sent"
        return "queued"

    def _validate_reauth_proof_locked(
        self,
        *,
        reauth_proof: str,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        now: datetime,
    ) -> None:
        reauth_record = self._reauth_proofs_by_value.get(reauth_proof)
        if reauth_record is None or reauth_record.consumed_at is not None:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_reauth_invalid",
                message="Deletion confirmation re-auth proof is invalid.",
                reason="account_deletion_confirm_reauth_invalid",
            )
        if now >= reauth_record.expires_at:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_reauth_expired",
                message="Deletion confirmation re-auth proof has expired.",
                reason="account_deletion_confirm_reauth_expired",
            )
        if (
            reauth_record.user_id != user_id
            or reauth_record.tenant_id != tenant_id
            or reauth_record.request_id != request_id
        ):
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_proof_context_mismatch",
                message="Deletion confirmation proof context does not match request context.",
                reason="account_deletion_confirm_proof_context_mismatch",
            )

    def _validate_otp_proof_locked(
        self,
        *,
        otp_verification_id: UUID,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        now: datetime,
    ) -> None:
        otp_record = self._otp_proofs_by_id.get(otp_verification_id)
        if otp_record is None or otp_record.consumed_at is not None:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_otp_invalid",
                message="Deletion confirmation OTP proof is invalid.",
                reason="account_deletion_confirm_otp_invalid",
            )
        if now >= otp_record.expires_at:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_otp_expired",
                message="Deletion confirmation OTP proof has expired.",
                reason="account_deletion_confirm_otp_expired",
            )
        if (
            otp_record.user_id != user_id
            or otp_record.tenant_id != tenant_id
            or otp_record.request_id != request_id
        ):
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_proof_context_mismatch",
                message="Deletion confirmation proof context does not match request context.",
                reason="account_deletion_confirm_proof_context_mismatch",
            )

    def _consume_reauth_proof_locked(
        self, *, reauth_proof: str, consumed_at: datetime
    ) -> None:
        record = self._reauth_proofs_by_value[reauth_proof]
        self._reauth_proofs_by_value[reauth_proof] = _ReauthProofRecord(
            proof_id=record.proof_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            request_id=record.request_id,
            expires_at=record.expires_at,
            consumed_at=consumed_at,
        )

    def _consume_otp_proof_locked(
        self,
        *,
        otp_verification_id: UUID,
        consumed_at: datetime,
    ) -> None:
        record = self._otp_proofs_by_id[otp_verification_id]
        self._otp_proofs_by_id[otp_verification_id] = _OtpProofRecord(
            otp_verification_id=record.otp_verification_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            request_id=record.request_id,
            expires_at=record.expires_at,
            consumed_at=consumed_at,
        )

    def _append_audit_record(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
        trace_ref: str,
        occurred_at: str,
        correlation_id: str | None,
        action: LifecycleAction,
        action_status: LifecycleStatus,
        deletion_state: DeletionState,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        reason_code: str | None = None,
    ) -> None:
        digest_payload = (
            f"{user_id}:{request_id}:{trace_ref}:{occurred_at}:{action}:"
            f"{deletion_state}:{','.join(blocker_reasons)}:{reason_code or ''}:"
            f"{correlation_id or ''}"
        )
        digest = sha256(digest_payload.encode()).hexdigest()
        audit_event = AccountDeletionAuditRecord(
            audit_evidence_id=digest,
            event_id=digest,
            event_type=action,
            user_id=user_id,
            request_id=request_id,
            action=action,
            action_status=action_status,
            deletion_state=deletion_state,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            blocker_reasons=blocker_reasons,
            reason_code=reason_code,
            trace_ref=trace_ref,
            created_at=occurred_at,
        )
        existing = self._audit_by_user.get(user_id, [])
        self._audit_by_user[user_id] = [*existing, audit_event]


class UnavailableAccountDeletionRequestStore:
    """Fail closed when production deletion persistence is unavailable."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
    ) -> None:
        self._status_code = status_code
        self._error_code = error_code
        self._message = message
        self._reason = reason

    def create_or_replay_request(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_reason: str,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        idempotency_key: str,
        request_fingerprint: str,
        requested_at: str,
        correlation_id: str | None,
    ) -> AccountDeletionRequestResponse:
        del (
            user_id,
            tenant_id,
            request_reason,
            blocker_reasons,
            idempotency_key,
            request_fingerprint,
            requested_at,
            correlation_id,
        )
        raise self._error()

    def create_or_replay_confirmation(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        reauth_proof: str,
        otp_verification_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionConfirmResponse:
        del (
            user_id,
            tenant_id,
            request_id,
            reauth_proof,
            otp_verification_id,
            idempotency_key,
            request_fingerprint,
            correlation_id,
        )
        raise self._error()

    def create_or_replay_cancel(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionCancelResponse:
        del (
            user_id,
            tenant_id,
            request_id,
            idempotency_key,
            request_fingerprint,
            correlation_id,
        )
        raise self._error()

    def create_or_replay_execution(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
    ) -> AccountDeletionExecuteResponse:
        del (
            user_id,
            tenant_id,
            request_id,
            idempotency_key,
            request_fingerprint,
            correlation_id,
            registration_store,
        )
        raise self._error()

    def get_active_request_for_user(
        self, *, user_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        del user_id
        raise self._error()

    def get_request_by_id(
        self, *, request_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        del request_id
        raise self._error()

    def get_audit_events_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionAuditRecord]:
        del user_id
        raise self._error()

    def get_notification_records_for_user(
        self,
        *,
        user_id: UUID,
    ) -> list[AccountDeletionNotificationRecord]:
        del user_id
        raise self._error()

    def get_incident_records_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionIncidentRecord]:
        del user_id
        raise self._error()

    def evaluate_deletion_precheck_blockers(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
    ) -> tuple[PrecheckReasonCode, ...]:
        del user_id, tenant_id
        raise self._error()

    def revoke_all_active_sessions_for_user(self, *, user_id: UUID) -> int:
        del user_id
        raise self._error()

    def _error(self) -> AccountDeletionRequestError:
        return AccountDeletionRequestError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentAccountDeletionRequestStore:
    """Persist account-deletion lifecycle state in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url
        self._precheck_context_by_identity: dict[
            tuple[UUID, str], AccountDeletionPrecheckContext
        ] = {}
        self._lock = Lock()

    def create_or_replay_request(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_reason: str,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        idempotency_key: str,
        request_fingerprint: str,
        requested_at: str,
        correlation_id: str | None,
    ) -> AccountDeletionRequestResponse:
        requested_at_value = _parse_utc(requested_at)
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    existing_row = self._fetch_request_by_field_locked(
                        cursor=cursor,
                        field_name="request_idempotency_key",
                        field_value=idempotency_key,
                    )
                    if existing_row is not None:
                        if str(existing_row[8]) != request_fingerprint:
                            raise AccountDeletionRequestError(
                                status_code=409,
                                error_code="idempotency_key_conflict",
                                message=(
                                    "Idempotency key conflicts with an existing "
                                    "account deletion request."
                                ),
                                reason="idempotency_key_conflict",
                            )
                        existing_record = (
                            _row_to_account_deletion_request_record(
                                row=existing_row
                            )
                        )
                        if existing_record.deletion_state not in (
                            "requested",
                            "blocked",
                        ):
                            raise AccountDeletionRequestError(
                                status_code=409,
                                error_code="account_deletion_request_already_active",
                                message="An active account deletion request already exists.",
                                reason="account_deletion_request_already_active",
                            )
                        idempotent_state: Literal["requested", "blocked"] = (
                            existing_record.deletion_state
                        )
                        return AccountDeletionRequestResponse(
                            status="accepted",
                            request_id=existing_record.request_id,
                            deletion_state=idempotent_state,
                            requested_at=existing_record.requested_at,
                            blockers=[*existing_record.blocker_reasons],
                        )

                    active_row = self._fetch_active_request_for_user_locked(
                        cursor=cursor,
                        user_id=user_id,
                    )
                    if active_row is not None:
                        active_record = _row_to_account_deletion_request_record(
                            row=active_row
                        )
                        raise AccountDeletionRequestError(
                            status_code=409,
                            error_code="account_deletion_request_already_active",
                            message="An active account deletion request already exists.",
                            reason="account_deletion_request_already_active",
                            details=self._build_request_state_details_locked(
                                cursor=cursor,
                                request_record=active_record,
                                requested_state="requested",
                            ),
                        )

                    request_id = uuid4()
                    deletion_state: Literal["requested", "blocked"] = (
                        "blocked" if blocker_reasons else "requested"
                    )
                    cursor.execute(
                        """
                        INSERT INTO auth_account_deletion_requests (
                            request_id,
                            user_id,
                            tenant_id,
                            request_reason,
                            requested_at,
                            deletion_state,
                            blocker_reasons,
                            request_idempotency_key,
                            request_fingerprint,
                            confirmed_at,
                            cooldown_expires_at,
                            executed_at,
                            execution_outcome,
                            revoked_session_count,
                            confirm_idempotency_key,
                            confirm_request_fingerprint,
                            cancel_idempotency_key,
                            cancel_request_fingerprint,
                            execute_idempotency_key,
                            execute_request_fingerprint
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                            NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
                        )
                        """,
                        (
                            request_id,
                            user_id,
                            tenant_id,
                            request_reason,
                            requested_at_value,
                            deletion_state,
                            json.dumps([*blocker_reasons]),
                            idempotency_key,
                            request_fingerprint,
                        ),
                    )
                    self._emit_lifecycle_evidence_locked(
                        cursor=cursor,
                        user_id=user_id,
                        request_id=request_id,
                        trace_ref=request_fingerprint,
                        occurred_at=requested_at_value,
                        correlation_id=correlation_id,
                        action=(
                            "account_deletion_request_blocked"
                            if blocker_reasons
                            else "account_deletion_request_created"
                        ),
                        action_status=(
                            "blocked" if blocker_reasons else "created"
                        ),
                        deletion_state=deletion_state,
                        blocker_reasons=blocker_reasons,
                    )
                connection.commit()
        except AccountDeletionRequestError:
            raise
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return AccountDeletionRequestResponse(
            status="accepted",
            request_id=request_id,
            deletion_state=deletion_state,
            requested_at=requested_at,
            blockers=[*blocker_reasons],
        )

    def create_or_replay_confirmation(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        reauth_proof: str,
        otp_verification_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionConfirmResponse:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    existing_row = self._fetch_request_by_field_locked(
                        cursor=cursor,
                        field_name="confirm_idempotency_key",
                        field_value=idempotency_key,
                    )
                    if existing_row is not None:
                        if str(existing_row[15]) != request_fingerprint:
                            raise AccountDeletionRequestError(
                                status_code=409,
                                error_code="idempotency_key_conflict",
                                message=(
                                    "Idempotency key conflicts with an existing "
                                    "deletion confirmation."
                                ),
                                reason="idempotency_key_conflict",
                            )
                        existing_record = (
                            _row_to_account_deletion_request_record(
                                row=existing_row
                            )
                        )
                        if existing_record.cooldown_expires_at is None:
                            raise _account_deletion_missing_state()
                        return AccountDeletionConfirmResponse(
                            status="confirmed",
                            request_id=existing_record.request_id,
                            deletion_state="cooldown_active",
                            cooldown_expires_at=existing_record.cooldown_expires_at,
                        )

                    row = self._fetch_request_by_field_locked(
                        cursor=cursor,
                        field_name="request_id",
                        field_value=request_id,
                        for_update=True,
                    )
                    if row is None:
                        raise AccountDeletionRequestError(
                            status_code=404,
                            error_code="account_deletion_confirm_request_not_found",
                            message="Account deletion request was not found.",
                            reason="account_deletion_confirm_request_not_found",
                        )
                    request_record = _row_to_account_deletion_request_record(
                        row=row
                    )
                    if (
                        request_record.user_id != user_id
                        or request_record.tenant_id != tenant_id
                    ):
                        raise AccountDeletionRequestError(
                            status_code=404,
                            error_code="account_deletion_confirm_request_not_found",
                            message="Account deletion request was not found.",
                            reason="account_deletion_confirm_request_not_found",
                            details=self._record_incident_and_build_details_locked(
                                cursor=cursor,
                                incident_code="account_deletion_malicious_takeover_attempt",
                                message=(
                                    "Malicious account deletion confirmation attempt was blocked."
                                ),
                                reason="account_deletion_confirm_request_not_found",
                                actor_user_id=user_id,
                                tenant_id=tenant_id,
                                request_id=request_id,
                                account_deletion_state="not_owned",
                                correlation_id=correlation_id,
                            ),
                        )
                    if request_record.deletion_state != "requested":
                        details = self._build_request_state_details_locked(
                            cursor=cursor,
                            request_record=request_record,
                            requested_state="confirmed",
                        )
                        if (
                            "deletion_blocked_legal_hold"
                            in request_record.blocker_reasons
                        ):
                            details.update(
                                self._record_incident_and_build_details_locked(
                                    cursor=cursor,
                                    incident_code="account_deletion_legal_hold_dispute",
                                    message=(
                                        "Legal-hold dispute is required before "
                                        "deletion may proceed."
                                    ),
                                    reason="account_deletion_confirm_invalid_state",
                                    actor_user_id=user_id,
                                    tenant_id=tenant_id,
                                    request_id=request_id,
                                    account_deletion_state=request_record.deletion_state,
                                    correlation_id=correlation_id,
                                )
                            )
                        raise AccountDeletionRequestError(
                            status_code=409,
                            error_code="account_deletion_confirm_invalid_state",
                            message="Account deletion request is not in confirmable state.",
                            reason="account_deletion_confirm_invalid_state",
                            details=details,
                        )

                    now = datetime.now(UTC)
                    self._validate_reauth_proof_locked(
                        cursor=cursor,
                        reauth_proof=reauth_proof,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        now=now,
                    )
                    self._validate_otp_proof_locked(
                        cursor=cursor,
                        otp_verification_id=otp_verification_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        now=now,
                    )
                    cooldown_expires_at = now + timedelta(
                        seconds=get_account_deletion_cooldown_seconds()
                    )
                    cursor.execute(
                        """
                        UPDATE auth_account_deletion_requests
                        SET deletion_state = 'confirmed',
                            confirmed_at = %s,
                            cooldown_expires_at = %s,
                            confirm_idempotency_key = %s,
                            confirm_request_fingerprint = %s
                        WHERE request_id = %s
                        """,
                        (
                            now,
                            cooldown_expires_at,
                            idempotency_key,
                            request_fingerprint,
                            request_id,
                        ),
                    )
                    self._consume_reauth_proof_locked(
                        cursor=cursor,
                        reauth_proof=reauth_proof,
                        consumed_at=now,
                    )
                    self._consume_otp_proof_locked(
                        cursor=cursor,
                        otp_verification_id=otp_verification_id,
                        consumed_at=now,
                    )
                    self._emit_lifecycle_evidence_locked(
                        cursor=cursor,
                        user_id=user_id,
                        request_id=request_id,
                        trace_ref=request_fingerprint,
                        occurred_at=now,
                        correlation_id=correlation_id,
                        action="account_deletion_request_confirmed",
                        action_status="confirmed",
                        deletion_state="confirmed",
                        blocker_reasons=(),
                    )
                connection.commit()
        except AccountDeletionRequestError:
            raise
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return AccountDeletionConfirmResponse(
            status="confirmed",
            request_id=request_id,
            deletion_state="cooldown_active",
            cooldown_expires_at=_utc_iso(cooldown_expires_at),
        )

    def create_or_replay_cancel(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> AccountDeletionCancelResponse:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    existing_row = self._fetch_request_by_field_locked(
                        cursor=cursor,
                        field_name="cancel_idempotency_key",
                        field_value=idempotency_key,
                    )
                    if existing_row is not None:
                        if str(existing_row[17]) != request_fingerprint:
                            raise AccountDeletionRequestError(
                                status_code=409,
                                error_code="idempotency_key_conflict",
                                message=(
                                    "Idempotency key conflicts with an existing "
                                    "deletion cancellation."
                                ),
                                reason="idempotency_key_conflict",
                            )
                        existing_record = (
                            _row_to_account_deletion_request_record(
                                row=existing_row
                            )
                        )
                        return AccountDeletionCancelResponse(
                            status="cancelled",
                            request_id=existing_record.request_id,
                            deletion_state="cancelled",
                        )

                    row = self._fetch_request_by_field_locked(
                        cursor=cursor,
                        field_name="request_id",
                        field_value=request_id,
                        for_update=True,
                    )
                    if row is None:
                        raise AccountDeletionRequestError(
                            status_code=404,
                            error_code="account_deletion_cancel_request_not_found",
                            message="Account deletion request was not found.",
                            reason="account_deletion_cancel_request_not_found",
                        )
                    request_record = _row_to_account_deletion_request_record(
                        row=row
                    )
                    if (
                        request_record.user_id != user_id
                        or request_record.tenant_id != tenant_id
                    ):
                        raise AccountDeletionRequestError(
                            status_code=404,
                            error_code="account_deletion_cancel_request_not_found",
                            message="Account deletion request was not found.",
                            reason="account_deletion_cancel_request_not_found",
                            details=self._record_incident_and_build_details_locked(
                                cursor=cursor,
                                incident_code="account_deletion_malicious_takeover_attempt",
                                message=(
                                    "Malicious account deletion cancellation attempt was blocked."
                                ),
                                reason="account_deletion_cancel_request_not_found",
                                actor_user_id=user_id,
                                tenant_id=tenant_id,
                                request_id=request_id,
                                account_deletion_state="not_owned",
                                correlation_id=correlation_id,
                            ),
                        )
                    if request_record.deletion_state != "confirmed":
                        raise AccountDeletionRequestError(
                            status_code=409,
                            error_code="account_deletion_cancel_not_allowed_for_state",
                            message="Account deletion request is not in cancellable state.",
                            reason="account_deletion_cancel_not_allowed_for_state",
                            details=self._record_incident_and_build_details_locked(
                                cursor=cursor,
                                incident_code="account_deletion_erroneous_request",
                                message=(
                                    "Erroneous account deletion cancellation attempt was blocked."
                                ),
                                reason="account_deletion_cancel_not_allowed_for_state",
                                actor_user_id=user_id,
                                tenant_id=tenant_id,
                                request_id=request_id,
                                account_deletion_state=request_record.deletion_state,
                                correlation_id=correlation_id,
                                requested_state="cancelled",
                                include_request_state=True,
                            ),
                        )
                    if request_record.cooldown_expires_at is None:
                        raise AccountDeletionRequestError(
                            status_code=409,
                            error_code="account_deletion_cancel_not_allowed_for_state",
                            message="Account deletion request is not in cancellable state.",
                            reason="account_deletion_cancel_not_allowed_for_state",
                            details=self._record_incident_and_build_details_locked(
                                cursor=cursor,
                                incident_code="account_deletion_erroneous_request",
                                message=(
                                    "Erroneous account deletion cancellation attempt was blocked."
                                ),
                                reason="account_deletion_cancel_not_allowed_for_state",
                                actor_user_id=user_id,
                                tenant_id=tenant_id,
                                request_id=request_id,
                                account_deletion_state=request_record.deletion_state,
                                correlation_id=correlation_id,
                                requested_state="cancelled",
                                include_request_state=True,
                            ),
                        )
                    now = datetime.now(UTC)
                    cooldown_expires_at = _parse_utc(
                        request_record.cooldown_expires_at
                    )
                    if now >= cooldown_expires_at:
                        raise AccountDeletionRequestError(
                            status_code=409,
                            error_code="account_deletion_cancel_cooldown_expired",
                            message="Account deletion request cooldown window has expired.",
                            reason="account_deletion_cancel_cooldown_expired",
                            details=self._record_incident_and_build_details_locked(
                                cursor=cursor,
                                incident_code="account_deletion_erroneous_request",
                                message=(
                                    "Erroneous cancellation attempt after cooldown "
                                    "expiry was blocked."
                                ),
                                reason="account_deletion_cancel_cooldown_expired",
                                actor_user_id=user_id,
                                tenant_id=tenant_id,
                                request_id=request_id,
                                account_deletion_state=request_record.deletion_state,
                                correlation_id=correlation_id,
                                requested_state="cancelled",
                                include_request_state=True,
                            ),
                        )
                    cursor.execute(
                        """
                        UPDATE auth_account_deletion_requests
                        SET deletion_state = 'cancelled',
                            cancel_idempotency_key = %s,
                            cancel_request_fingerprint = %s
                        WHERE request_id = %s
                        """,
                        (idempotency_key, request_fingerprint, request_id),
                    )
                    self._emit_lifecycle_evidence_locked(
                        cursor=cursor,
                        user_id=user_id,
                        request_id=request_id,
                        trace_ref=request_fingerprint,
                        occurred_at=now,
                        correlation_id=correlation_id,
                        action="account_deletion_request_cancelled",
                        action_status="cancelled",
                        deletion_state="cancelled",
                        blocker_reasons=(),
                    )
                connection.commit()
        except AccountDeletionRequestError:
            raise
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return AccountDeletionCancelResponse(
            status="cancelled",
            request_id=request_id,
            deletion_state="cancelled",
        )

    def create_or_replay_execution(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
    ) -> AccountDeletionExecuteResponse:
        executed_at: str | None = None
        revoked_session_count: int | None = None

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> AccountDeletionExecuteResponse:
            nonlocal executed_at
            nonlocal revoked_session_count
            with connection.cursor() as cursor:
                existing_row = self._fetch_request_by_field_locked(
                    cursor=cursor,
                    field_name="execute_idempotency_key",
                    field_value=idempotency_key,
                )
                if existing_row is not None:
                    if str(existing_row[19]) != request_fingerprint:
                        raise AccountDeletionRequestError(
                            status_code=409,
                            error_code="idempotency_key_conflict",
                            message=(
                                "Idempotency key conflicts with an existing deletion execution."
                            ),
                            reason="idempotency_key_conflict",
                        )
                    existing_record = _row_to_account_deletion_request_record(
                        row=existing_row
                    )
                    if (
                        existing_record.executed_at is None
                        or existing_record.revoked_session_count is None
                    ):
                        raise _account_deletion_missing_state()
                    executed_at = existing_record.executed_at
                    revoked_session_count = existing_record.revoked_session_count
                    return AccountDeletionExecuteResponse(
                        status="executed",
                        request_id=existing_record.request_id,
                        deletion_state="executed",
                        execution_outcome="tombstoned",
                        executed_at=existing_record.executed_at,
                        revoked_session_count=existing_record.revoked_session_count,
                    )

                row = self._fetch_request_by_field_locked(
                    cursor=cursor,
                    field_name="request_id",
                    field_value=request_id,
                    for_update=True,
                )
                if row is None:
                    raise AccountDeletionRequestError(
                        status_code=404,
                        error_code="account_deletion_execute_request_not_found",
                        message="Account deletion request was not found.",
                        reason="account_deletion_execute_request_not_found",
                    )
                request_record = _row_to_account_deletion_request_record(row=row)
                if (
                    request_record.user_id != user_id
                    or request_record.tenant_id != tenant_id
                ):
                    raise AccountDeletionRequestError(
                        status_code=404,
                        error_code="account_deletion_execute_request_not_found",
                        message="Account deletion request was not found.",
                        reason="account_deletion_execute_request_not_found",
                        details=self._record_incident_and_build_details_locked(
                            cursor=cursor,
                            incident_code="account_deletion_malicious_takeover_attempt",
                            message=(
                                "Malicious account deletion execution attempt was blocked."
                            ),
                            reason="account_deletion_execute_request_not_found",
                            actor_user_id=user_id,
                            tenant_id=tenant_id,
                            request_id=request_id,
                            account_deletion_state="not_owned",
                            correlation_id=correlation_id,
                        ),
                    )
                if request_record.deletion_state == "executed":
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="account_deletion_execute_already_completed",
                        message="Account deletion execution was already completed.",
                        reason="account_deletion_execute_already_completed",
                        details=self._build_request_state_details_locked(
                            cursor=cursor,
                            request_record=request_record,
                            requested_state="executed",
                        ),
                    )
                if request_record.deletion_state != "confirmed":
                    details = self._build_request_state_details_locked(
                        cursor=cursor,
                        request_record=request_record,
                        requested_state="executed",
                    )
                    if "deletion_blocked_legal_hold" in request_record.blocker_reasons:
                        details.update(
                            self._record_incident_and_build_details_locked(
                                cursor=cursor,
                                incident_code="account_deletion_legal_hold_dispute",
                                message=(
                                    "Legal-hold dispute is required before "
                                    "deletion execution may proceed."
                                ),
                                reason="account_deletion_execute_invalid_state",
                                actor_user_id=user_id,
                                tenant_id=tenant_id,
                                request_id=request_id,
                                account_deletion_state=request_record.deletion_state,
                                correlation_id=correlation_id,
                            )
                        )
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="account_deletion_execute_invalid_state",
                        message="Account deletion request is not in executable state.",
                        reason="account_deletion_execute_invalid_state",
                        details=details,
                    )
                if request_record.cooldown_expires_at is None:
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="account_deletion_execute_invalid_state",
                        message="Account deletion request is not in executable state.",
                        reason="account_deletion_execute_invalid_state",
                        details=self._record_incident_and_build_details_locked(
                            cursor=cursor,
                            incident_code="account_deletion_erroneous_request",
                            message=(
                                "Erroneous account deletion execution attempt was blocked."
                            ),
                            reason="account_deletion_execute_invalid_state",
                            actor_user_id=user_id,
                            tenant_id=tenant_id,
                            request_id=request_id,
                            account_deletion_state=request_record.deletion_state,
                            correlation_id=correlation_id,
                            requested_state="executed",
                            include_request_state=True,
                        ),
                    )
                now = datetime.now(UTC)
                cooldown_expires_at = _parse_utc(request_record.cooldown_expires_at)
                if now < cooldown_expires_at:
                    raise AccountDeletionRequestError(
                        status_code=409,
                        error_code="account_deletion_execute_not_allowed",
                        message=(
                            "Account deletion execution is not allowed before cooldown expires."
                        ),
                        reason="account_deletion_execute_not_allowed",
                        details=self._record_incident_and_build_details_locked(
                            cursor=cursor,
                            incident_code="account_deletion_erroneous_request",
                            message=(
                                "Erroneous account deletion execution during "
                                "cooldown was blocked."
                            ),
                            reason="account_deletion_execute_not_allowed",
                            actor_user_id=user_id,
                            tenant_id=tenant_id,
                            request_id=request_id,
                            account_deletion_state=request_record.deletion_state,
                            correlation_id=correlation_id,
                            requested_state="executed",
                            include_request_state=True,
                            extra_details={"reason_code": "deletion_cooldown_active"},
                        ),
                    )

                executed_at = _utc_iso(now)
                tombstoned_at_value = _parse_utc(executed_at)
                anonymized_email = f"deleted-{user_id.hex}@deleted.invalid"
                anonymized_phone = _build_tombstoned_phone_number(user_id=user_id)
                invalidated_hash = sha256(
                    f"tombstoned:{user_id}:{executed_at}".encode()
                ).hexdigest()
                cursor.execute(
                    """
                    UPDATE users
                    SET email_encrypted = %s,
                        phone_number_encrypted = %s,
                        password_hash = %s,
                        account_state = 'disabled',
                        credentials_invalidated_at = %s,
                        deletion_lifecycle_state = 'tombstoned',
                        anonymized_at = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        anonymized_email,
                        anonymized_phone,
                        invalidated_hash,
                        tombstoned_at_value,
                        tombstoned_at_value,
                        tombstoned_at_value,
                        user_id,
                    ),
                )
                session_store = PersistentSessionIssuanceStore(
                    database_url=self._database_url
                )
                revoked_session_count = 0
                session_records = session_store._get_session_records_for_user_locked(
                    cursor=cursor,
                    user_id=user_id,
                )
                for record in session_records:
                    evaluation = session_store._evaluate_record(
                        record=record,
                        now=now,
                    )
                    if evaluation.status not in {"active", "warning"}:
                        continue
                    updated_session = session_store._invalidate_record(
                        record=record,
                        invalidated_at=now,
                        invalidated_reason="session_revoked",
                    )
                    session_store._write_session_record_locked(
                        cursor=cursor,
                        record=updated_session,
                    )
                    revoked_session_count += 1
                cursor.execute(
                    """
                    UPDATE auth_account_deletion_requests
                    SET deletion_state = 'executed',
                        executed_at = %s,
                        execution_outcome = 'tombstoned',
                        revoked_session_count = %s,
                        execute_idempotency_key = %s,
                        execute_request_fingerprint = %s
                    WHERE request_id = %s
                    """,
                    (
                        now,
                        revoked_session_count,
                        idempotency_key,
                        request_fingerprint,
                        request_id,
                    ),
                )
                self._emit_lifecycle_evidence_locked(
                    cursor=cursor,
                    user_id=user_id,
                    request_id=request_id,
                    trace_ref=request_fingerprint,
                    occurred_at=now,
                    correlation_id=correlation_id,
                    action="account_deletion_request_executed",
                    action_status="executed",
                    deletion_state="executed",
                    blocker_reasons=(),
                )
            return AccountDeletionExecuteResponse(
                status="executed",
                request_id=request_id,
                deletion_state="executed",
                execution_outcome="tombstoned",
                executed_at=executed_at or _utc_now_iso(),
                revoked_session_count=0 if revoked_session_count is None else revoked_session_count,
            )

        def _reconcile_callback() -> AccountDeletionExecuteResponse | None:
            try:
                with connect_auth_database(self._database_url) as connection:
                    with connection.cursor() as cursor:
                        existing_row = self._fetch_request_by_field_locked(
                            cursor=cursor,
                            field_name="execute_idempotency_key",
                            field_value=idempotency_key,
                        )
            except psycopg.Error:
                return None
            if existing_row is None:
                return None
            if str(existing_row[19]) != request_fingerprint:
                return None
            existing_record = _row_to_account_deletion_request_record(
                row=existing_row
            )
            if (
                existing_record.executed_at is None
                or existing_record.revoked_session_count is None
            ):
                return None
            return AccountDeletionExecuteResponse(
                status="executed",
                request_id=existing_record.request_id,
                deletion_state="executed",
                execution_outcome="tombstoned",
                executed_at=existing_record.executed_at,
                revoked_session_count=existing_record.revoked_session_count,
            )

        try:
            result = execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AccountDeletionRequestError:
            raise
        except AuthCockroachTransactionAmbiguousCommitError as error:
            raise _account_deletion_ambiguous_result() from error
        except AuthCockroachTransactionError as error:
            raise _account_deletion_persistence_unavailable() from error
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return result

    def get_active_request_for_user(
        self, *, user_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    row = self._fetch_active_request_for_user_locked(
                        cursor=cursor, user_id=user_id
                    )
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_account_deletion_request_record(row=row)

    def get_request_by_id(
        self, *, request_id: UUID
    ) -> AccountDeletionRequestRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    row = self._fetch_request_by_field_locked(
                        cursor=cursor,
                        field_name="request_id",
                        field_value=request_id,
                    )
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_account_deletion_request_record(row=row)

    def get_audit_events_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionAuditRecord]:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_evidence_id,
                            event_id,
                            event_type,
                            user_id,
                            request_id,
                            action,
                            action_status,
                            deletion_state,
                            occurred_at,
                            correlation_id,
                            blocker_reasons,
                            reason_code,
                            trace_ref,
                            created_at
                        FROM auth_account_deletion_audit_events
                        WHERE user_id = %s
                        ORDER BY occurred_at ASC, created_at ASC
                        """,
                        (user_id,),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return [_row_to_account_deletion_audit_record(row=row) for row in rows]

    def get_notification_records_for_user(
        self,
        *,
        user_id: UUID,
    ) -> list[AccountDeletionNotificationRecord]:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            notification_id,
                            request_id,
                            channel,
                            status,
                            attempted_at,
                            event_type,
                            user_id,
                            deletion_state,
                            correlation_id
                        FROM auth_account_deletion_notifications
                        WHERE user_id = %s
                        ORDER BY attempted_at ASC, created_at ASC
                        """,
                        (user_id,),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return [
            _row_to_account_deletion_notification_record(row=row)
            for row in rows
        ]

    def get_incident_records_for_user(
        self, *, user_id: UUID
    ) -> list[AccountDeletionIncidentRecord]:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            audit_reference_id,
                            incident_code,
                            message,
                            reason,
                            request_id,
                            actor_user_id,
                            tenant_id,
                            account_deletion_state,
                            occurred_at,
                            correlation_id
                        FROM auth_account_deletion_incidents
                        WHERE actor_user_id = %s
                        ORDER BY occurred_at ASC, created_at ASC
                        """,
                        (user_id,),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return [
            _row_to_account_deletion_incident_record(row=row) for row in rows
        ]

    def evaluate_deletion_precheck_blockers(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
    ) -> tuple[PrecheckReasonCode, ...]:
        with self._lock:
            context = self._precheck_context_by_identity.get(
                (user_id, tenant_id), AccountDeletionPrecheckContext()
            )
        blocker_map: dict[PrecheckReasonCode, bool] = {
            "deletion_blocked_compliance_lock": context.compliance_lock,
            "deletion_blocked_legal_hold": context.legal_hold,
            "deletion_blocked_active_obligation": context.active_obligation,
            "deletion_blocked_retention_constraint": context.retention_constraint,
        }
        return tuple(
            reason_code
            for reason_code in _BLOCKER_REASON_ORDER
            if blocker_map.get(reason_code, False)
        )

    def revoke_all_active_sessions_for_user(self, *, user_id: UUID) -> int:
        return self._revoke_sessions_via_runtime(user_id=user_id)

    def set_test_precheck_context(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        compliance_lock: bool = False,
        legal_hold: bool = False,
        active_obligation: bool = False,
        retention_constraint: bool = False,
    ) -> None:
        with self._lock:
            self._precheck_context_by_identity[(user_id, tenant_id)] = (
                AccountDeletionPrecheckContext(
                    compliance_lock=compliance_lock,
                    legal_hold=legal_hold,
                    active_obligation=active_obligation,
                    retention_constraint=retention_constraint,
                )
            )

    def issue_test_reauth_proof(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        ttl_seconds: int = 180,
    ) -> str:
        proof_id = f"reauth:{uuid4()}"
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO auth_account_deletion_reauth_proofs (
                            proof_id,
                            user_id,
                            tenant_id,
                            request_id,
                            expires_at,
                            consumed_at
                        )
                        VALUES (%s, %s, %s, %s, %s, NULL)
                        """,
                        (proof_id, user_id, tenant_id, request_id, expires_at),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return proof_id

    def issue_test_otp_verification_proof(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        ttl_seconds: int = 180,
    ) -> UUID:
        otp_verification_id = uuid4()
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO auth_account_deletion_otp_proofs (
                            otp_verification_id,
                            user_id,
                            tenant_id,
                            request_id,
                            expires_at,
                            consumed_at
                        )
                        VALUES (%s, %s, %s, %s, %s, NULL)
                        """,
                        (
                            otp_verification_id,
                            user_id,
                            tenant_id,
                            request_id,
                            expires_at,
                        ),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error
        return otp_verification_id

    def force_request_cooldown_expired(self, *, request_id: UUID) -> None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE auth_account_deletion_requests
                        SET cooldown_expires_at = %s
                        WHERE request_id = %s
                        """,
                        (datetime.now(UTC) - timedelta(seconds=1), request_id),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise _account_deletion_persistence_unavailable() from error

    def issue_test_session_for_user(
        self, *, user_id: UUID, session_id: str | None = None
    ) -> str:
        del session_id
        store = PersistentSessionIssuanceStore(database_url=self._database_url)
        issued = store.issue_session(
            user_id=user_id,
            tenant_id="default_tenant",
            role="IndividualTaxpayer",
            device_fingerprint=f"account-deletion-test-{uuid4()}",
        )
        return str(issued.session_id)

    def _revoke_sessions_via_runtime(self, *, user_id: UUID) -> int:
        store = PersistentSessionIssuanceStore(database_url=self._database_url)
        return store.revoke_all_sessions_for_user(user_id=user_id)

    def _fetch_request_by_field_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        field_name: str,
        field_value: object,
        for_update: bool = False,
    ) -> tuple[object, ...] | None:
        if field_name == "request_id":
            query = """
                SELECT
                    request_id,
                    user_id,
                    tenant_id,
                    request_reason,
                    requested_at,
                    deletion_state,
                    blocker_reasons,
                    request_idempotency_key,
                    request_fingerprint,
                    confirmed_at,
                    cooldown_expires_at,
                    executed_at,
                    execution_outcome,
                    revoked_session_count,
                    confirm_idempotency_key,
                    confirm_request_fingerprint,
                    cancel_idempotency_key,
                    cancel_request_fingerprint,
                    execute_idempotency_key,
                    execute_request_fingerprint
                FROM auth_account_deletion_requests
                WHERE request_id = %s
            """
        elif field_name == "request_idempotency_key":
            query = """
                SELECT
                    request_id,
                    user_id,
                    tenant_id,
                    request_reason,
                    requested_at,
                    deletion_state,
                    blocker_reasons,
                    request_idempotency_key,
                    request_fingerprint,
                    confirmed_at,
                    cooldown_expires_at,
                    executed_at,
                    execution_outcome,
                    revoked_session_count,
                    confirm_idempotency_key,
                    confirm_request_fingerprint,
                    cancel_idempotency_key,
                    cancel_request_fingerprint,
                    execute_idempotency_key,
                    execute_request_fingerprint
                FROM auth_account_deletion_requests
                WHERE request_idempotency_key = %s
            """
        elif field_name == "confirm_idempotency_key":
            query = """
                SELECT
                    request_id,
                    user_id,
                    tenant_id,
                    request_reason,
                    requested_at,
                    deletion_state,
                    blocker_reasons,
                    request_idempotency_key,
                    request_fingerprint,
                    confirmed_at,
                    cooldown_expires_at,
                    executed_at,
                    execution_outcome,
                    revoked_session_count,
                    confirm_idempotency_key,
                    confirm_request_fingerprint,
                    cancel_idempotency_key,
                    cancel_request_fingerprint,
                    execute_idempotency_key,
                    execute_request_fingerprint
                FROM auth_account_deletion_requests
                WHERE confirm_idempotency_key = %s
            """
        elif field_name == "cancel_idempotency_key":
            query = """
                SELECT
                    request_id,
                    user_id,
                    tenant_id,
                    request_reason,
                    requested_at,
                    deletion_state,
                    blocker_reasons,
                    request_idempotency_key,
                    request_fingerprint,
                    confirmed_at,
                    cooldown_expires_at,
                    executed_at,
                    execution_outcome,
                    revoked_session_count,
                    confirm_idempotency_key,
                    confirm_request_fingerprint,
                    cancel_idempotency_key,
                    cancel_request_fingerprint,
                    execute_idempotency_key,
                    execute_request_fingerprint
                FROM auth_account_deletion_requests
                WHERE cancel_idempotency_key = %s
            """
        elif field_name == "execute_idempotency_key":
            query = """
                SELECT
                    request_id,
                    user_id,
                    tenant_id,
                    request_reason,
                    requested_at,
                    deletion_state,
                    blocker_reasons,
                    request_idempotency_key,
                    request_fingerprint,
                    confirmed_at,
                    cooldown_expires_at,
                    executed_at,
                    execution_outcome,
                    revoked_session_count,
                    confirm_idempotency_key,
                    confirm_request_fingerprint,
                    cancel_idempotency_key,
                    cancel_request_fingerprint,
                    execute_idempotency_key,
                    execute_request_fingerprint
                FROM auth_account_deletion_requests
                WHERE execute_idempotency_key = %s
            """
        else:
            raise _account_deletion_missing_state()
        if for_update:
            query += " FOR UPDATE"
        cursor.execute(query, (field_value,))
        row = cursor.fetchone()
        return None if row is None else cast(tuple[object, ...], row)

    def _fetch_active_request_for_user_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        user_id: UUID,
    ) -> tuple[object, ...] | None:
        cursor.execute(
            """
            SELECT
                request_id,
                user_id,
                tenant_id,
                request_reason,
                requested_at,
                deletion_state,
                blocker_reasons,
                request_idempotency_key,
                request_fingerprint,
                confirmed_at,
                cooldown_expires_at,
                executed_at,
                execution_outcome,
                revoked_session_count,
                confirm_idempotency_key,
                confirm_request_fingerprint,
                cancel_idempotency_key,
                cancel_request_fingerprint,
                execute_idempotency_key,
                execute_request_fingerprint
            FROM auth_account_deletion_requests
            WHERE user_id = %s
              AND deletion_state IN ('requested', 'blocked', 'confirmed')
            ORDER BY requested_at DESC, created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return None if row is None else cast(tuple[object, ...], row)

    def _build_request_state_details_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        request_record: AccountDeletionRequestRecord,
        requested_state: str,
    ) -> dict[str, object]:
        details: dict[str, object] = {
            "current_state": request_record.deletion_state,
            "requested_state": requested_state,
            "account_deletion_state": request_record.deletion_state,
            "blocker_reasons": [*request_record.blocker_reasons],
        }
        latest_audit_reference_id = (
            self._latest_audit_reference_for_request_locked(
                cursor=cursor,
                user_id=request_record.user_id,
                request_id=request_record.request_id,
            )
        )
        if latest_audit_reference_id is not None:
            details["audit_reference_id"] = latest_audit_reference_id
        return details

    def _latest_audit_reference_for_request_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        user_id: UUID,
        request_id: UUID,
    ) -> str | None:
        cursor.execute(
            """
            SELECT audit_evidence_id
            FROM auth_account_deletion_audit_events
            WHERE user_id = %s
              AND request_id = %s
            ORDER BY occurred_at DESC, created_at DESC
            LIMIT 1
            """,
            (user_id, request_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return str(cast(tuple[object, ...], row)[0])

    def _record_incident_and_build_details_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        incident_code: str,
        message: str,
        reason: str,
        actor_user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        account_deletion_state: str,
        correlation_id: str | None,
        requested_state: str | None = None,
        include_request_state: bool = False,
        extra_details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        audit_reference_id = self._append_incident_record_locked(
            cursor=cursor,
            incident_code=incident_code,
            message=message,
            reason=reason,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            request_id=request_id,
            account_deletion_state=account_deletion_state,
            correlation_id=correlation_id,
        )
        details: dict[str, object] = {
            "incident_code": incident_code,
            "account_deletion_state": account_deletion_state,
            "audit_reference_id": audit_reference_id,
        }
        if include_request_state:
            details["current_state"] = account_deletion_state
            if isinstance(requested_state, str):
                details["requested_state"] = requested_state
        if extra_details:
            details.update(extra_details)
        return details

    def _append_incident_record_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        incident_code: str,
        message: str,
        reason: str,
        actor_user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        account_deletion_state: str,
        correlation_id: str | None,
    ) -> str:
        payload = (
            f"{incident_code}:{reason}:{actor_user_id}:{tenant_id}:{request_id}:"
            f"{account_deletion_state}:{correlation_id or ''}"
        )
        audit_reference_id = sha256(payload.encode("utf-8")).hexdigest()
        occurred_at = datetime.now(UTC)
        cursor.execute(
            """
            INSERT INTO auth_account_deletion_incidents (
                audit_reference_id,
                incident_code,
                message,
                reason,
                request_id,
                actor_user_id,
                tenant_id,
                account_deletion_state,
                occurred_at,
                correlation_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (audit_reference_id) DO NOTHING
            """,
            (
                audit_reference_id,
                incident_code,
                message,
                reason,
                request_id,
                actor_user_id,
                tenant_id,
                account_deletion_state,
                occurred_at,
                correlation_id,
            ),
        )
        return audit_reference_id

    def _emit_lifecycle_evidence_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        user_id: UUID,
        request_id: UUID,
        trace_ref: str,
        occurred_at: datetime,
        correlation_id: str | None,
        action: LifecycleAction,
        action_status: LifecycleStatus,
        deletion_state: DeletionState,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        reason_code: str | None = None,
    ) -> None:
        self._append_audit_record_locked(
            cursor=cursor,
            user_id=user_id,
            request_id=request_id,
            trace_ref=trace_ref,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            action=action,
            action_status=action_status,
            deletion_state=deletion_state,
            blocker_reasons=blocker_reasons,
            reason_code=reason_code,
        )
        self._append_notification_record_locked(
            cursor=cursor,
            user_id=user_id,
            request_id=request_id,
            action=action,
            deletion_state=deletion_state,
            correlation_id=correlation_id,
            attempted_at=occurred_at,
        )

    def _append_notification_record_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        user_id: UUID,
        request_id: UUID,
        action: LifecycleAction,
        deletion_state: DeletionState,
        correlation_id: str | None,
        attempted_at: datetime,
    ) -> None:
        status = self._resolve_notification_status_for_action(
            action=action, deletion_state=deletion_state
        )
        notification_payload = (
            f"{request_id}:{action}:{status}:{_utc_iso(attempted_at)}:"
            f"{correlation_id or ''}:email"
        )
        notification_id = sha256(
            notification_payload.encode("utf-8")
        ).hexdigest()
        cursor.execute(
            """
            INSERT INTO auth_account_deletion_notifications (
                notification_id,
                request_id,
                channel,
                status,
                attempted_at,
                event_type,
                user_id,
                deletion_state,
                correlation_id
            )
            VALUES (%s, %s, 'email', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (notification_id) DO NOTHING
            """,
            (
                notification_id,
                request_id,
                status,
                attempted_at,
                action,
                user_id,
                deletion_state,
                correlation_id,
            ),
        )

    def _resolve_notification_status_for_action(
        self,
        *,
        action: LifecycleAction,
        deletion_state: DeletionState,
    ) -> NotificationStatus:
        if action not in {
            "account_deletion_request_created",
            "account_deletion_request_blocked",
            "account_deletion_request_confirmed",
            "account_deletion_request_cancelled",
            "account_deletion_request_executed",
        }:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_notification_not_allowed_for_state",
                message="Account deletion notification is not allowed for current lifecycle state.",
                reason="account_deletion_notification_not_allowed_for_state",
                details={
                    "current_state": deletion_state,
                    "requested_state": deletion_state,
                },
            )
        if action == "account_deletion_request_executed":
            return "sent"
        return "queued"

    def _validate_reauth_proof_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        reauth_proof: str,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        now: datetime,
    ) -> None:
        cursor.execute(
            """
            SELECT proof_id, user_id, tenant_id, request_id, expires_at, consumed_at
            FROM auth_account_deletion_reauth_proofs
            WHERE proof_id = %s
            FOR UPDATE
            """,
            (reauth_proof,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_reauth_invalid",
                message="Deletion confirmation re-auth proof is invalid.",
                reason="account_deletion_confirm_reauth_invalid",
            )
        record = cast(tuple[object, ...], row)
        if record[5] is not None:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_reauth_invalid",
                message="Deletion confirmation re-auth proof is invalid.",
                reason="account_deletion_confirm_reauth_invalid",
            )
        expires_at = _coerce_datetime(record[4])
        if now >= expires_at:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_reauth_expired",
                message="Deletion confirmation re-auth proof has expired.",
                reason="account_deletion_confirm_reauth_expired",
            )
        if (
            UUID(str(record[1])) != user_id
            or str(record[2]) != tenant_id
            or UUID(str(record[3])) != request_id
        ):
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_proof_context_mismatch",
                message="Deletion confirmation proof context does not match request context.",
                reason="account_deletion_confirm_proof_context_mismatch",
            )

    def _validate_otp_proof_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        otp_verification_id: UUID,
        user_id: UUID,
        tenant_id: str,
        request_id: UUID,
        now: datetime,
    ) -> None:
        cursor.execute(
            """
            SELECT otp_verification_id, user_id, tenant_id, request_id, expires_at, consumed_at
            FROM auth_account_deletion_otp_proofs
            WHERE otp_verification_id = %s
            FOR UPDATE
            """,
            (otp_verification_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_otp_invalid",
                message="Deletion confirmation OTP proof is invalid.",
                reason="account_deletion_confirm_otp_invalid",
            )
        record = cast(tuple[object, ...], row)
        if record[5] is not None:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_otp_invalid",
                message="Deletion confirmation OTP proof is invalid.",
                reason="account_deletion_confirm_otp_invalid",
            )
        expires_at = _coerce_datetime(record[4])
        if now >= expires_at:
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_otp_expired",
                message="Deletion confirmation OTP proof has expired.",
                reason="account_deletion_confirm_otp_expired",
            )
        if (
            UUID(str(record[1])) != user_id
            or str(record[2]) != tenant_id
            or UUID(str(record[3])) != request_id
        ):
            raise AccountDeletionRequestError(
                status_code=409,
                error_code="account_deletion_confirm_proof_context_mismatch",
                message="Deletion confirmation proof context does not match request context.",
                reason="account_deletion_confirm_proof_context_mismatch",
            )

    def _consume_reauth_proof_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        reauth_proof: str,
        consumed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            UPDATE auth_account_deletion_reauth_proofs
            SET consumed_at = %s
            WHERE proof_id = %s
            """,
            (consumed_at, reauth_proof),
        )

    def _consume_otp_proof_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        otp_verification_id: UUID,
        consumed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            UPDATE auth_account_deletion_otp_proofs
            SET consumed_at = %s
            WHERE otp_verification_id = %s
            """,
            (consumed_at, otp_verification_id),
        )

    def _append_audit_record_locked(
        self,
        *,
        cursor: psycopg.Cursor[object],
        user_id: UUID,
        request_id: UUID,
        trace_ref: str,
        occurred_at: datetime,
        correlation_id: str | None,
        action: LifecycleAction,
        action_status: LifecycleStatus,
        deletion_state: DeletionState,
        blocker_reasons: tuple[PrecheckReasonCode, ...],
        reason_code: str | None = None,
    ) -> None:
        digest_payload = (
            f"{user_id}:{request_id}:{trace_ref}:{_utc_iso(occurred_at)}:{action}:"
            f"{deletion_state}:{','.join(blocker_reasons)}:"
            f"{reason_code or ''}:{correlation_id or ''}"
        )
        digest = sha256(digest_payload.encode("utf-8")).hexdigest()
        cursor.execute(
            """
            INSERT INTO auth_account_deletion_audit_events (
                audit_evidence_id,
                event_id,
                event_type,
                user_id,
                request_id,
                action,
                action_status,
                deletion_state,
                occurred_at,
                correlation_id,
                blocker_reasons,
                reason_code,
                trace_ref,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (audit_evidence_id) DO NOTHING
            """,
            (
                digest,
                digest,
                action,
                user_id,
                request_id,
                action,
                action_status,
                deletion_state,
                occurred_at,
                correlation_id,
                json.dumps([*blocker_reasons]),
                reason_code,
                trace_ref,
                occurred_at,
            ),
        )


_ACCOUNT_DELETION_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "auth_account_deletion_requests": (
        "request_id",
        "user_id",
        "tenant_id",
        "request_reason",
        "requested_at",
        "deletion_state",
        "blocker_reasons",
        "request_idempotency_key",
        "request_fingerprint",
        "confirmed_at",
        "cooldown_expires_at",
        "executed_at",
        "execution_outcome",
        "revoked_session_count",
        "confirm_idempotency_key",
        "confirm_request_fingerprint",
        "cancel_idempotency_key",
        "cancel_request_fingerprint",
        "execute_idempotency_key",
        "execute_request_fingerprint",
    ),
    "auth_account_deletion_audit_events": (
        "audit_evidence_id",
        "event_id",
        "event_type",
        "user_id",
        "request_id",
        "action",
        "action_status",
        "deletion_state",
        "occurred_at",
        "correlation_id",
        "blocker_reasons",
        "reason_code",
        "trace_ref",
        "created_at",
    ),
    "auth_account_deletion_notifications": (
        "notification_id",
        "request_id",
        "channel",
        "status",
        "attempted_at",
        "event_type",
        "user_id",
        "deletion_state",
        "correlation_id",
    ),
    "auth_account_deletion_incidents": (
        "audit_reference_id",
        "incident_code",
        "message",
        "reason",
        "request_id",
        "actor_user_id",
        "tenant_id",
        "account_deletion_state",
        "occurred_at",
        "correlation_id",
    ),
    "auth_account_deletion_reauth_proofs": (
        "proof_id",
        "user_id",
        "tenant_id",
        "request_id",
        "expires_at",
        "consumed_at",
    ),
    "auth_account_deletion_otp_proofs": (
        "otp_verification_id",
        "user_id",
        "tenant_id",
        "request_id",
        "expires_at",
        "consumed_at",
    ),
}


def _account_deletion_persistence_unavailable() -> AccountDeletionRequestError:
    return AccountDeletionRequestError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _account_deletion_missing_state() -> AccountDeletionRequestError:
    return AccountDeletionRequestError(
        status_code=503,
        error_code="auth_persistence_missing_state",
        message="Required auth persistence state is missing.",
        reason="auth_persistence_missing_state",
    )


def _account_deletion_ambiguous_result() -> AccountDeletionRequestError:
    return AccountDeletionRequestError(
        status_code=500,
        error_code="auth_persistence_ambiguous_result",
        message="Auth persistence outcome is ambiguous.",
        reason="auth_persistence_ambiguous_result",
    )


def _row_to_account_deletion_request_record(
    *,
    row: tuple[object, ...],
) -> AccountDeletionRequestRecord:
    return AccountDeletionRequestRecord(
        request_id=UUID(str(row[0])),
        user_id=UUID(str(row[1])),
        tenant_id=str(row[2]),
        request_reason=str(row[3]),
        requested_at=_utc_iso(_coerce_datetime(row[4])),
        deletion_state=str(row[5]),  # type: ignore[arg-type]
        blocker_reasons=_coerce_blocker_reasons(row[6]),
        request_idempotency_key=str(row[7]),
        confirmed_at=_coerce_optional_iso(row[9]),
        cooldown_expires_at=_coerce_optional_iso(row[10]),
        executed_at=_coerce_optional_iso(row[11]),
        execution_outcome=(
            None if row[12] is None else str(row[12])
        ),  # type: ignore[arg-type]
        revoked_session_count=None if row[13] is None else _coerce_int(row[13]),
    )


def _row_to_account_deletion_audit_record(
    *,
    row: tuple[object, ...],
) -> AccountDeletionAuditRecord:
    return AccountDeletionAuditRecord(
        audit_evidence_id=str(row[0]),
        event_id=str(row[1]),
        event_type=str(row[2]),  # type: ignore[arg-type]
        user_id=UUID(str(row[3])),
        request_id=UUID(str(row[4])),
        action=str(row[5]),  # type: ignore[arg-type]
        action_status=str(row[6]),  # type: ignore[arg-type]
        deletion_state=str(row[7]),  # type: ignore[arg-type]
        occurred_at=_utc_iso(_coerce_datetime(row[8])),
        correlation_id=None if row[9] is None else str(row[9]),
        blocker_reasons=_coerce_blocker_reasons(row[10]),
        reason_code=None if row[11] is None else str(row[11]),
        trace_ref=str(row[12]),
        created_at=_utc_iso(_coerce_datetime(row[13])),
    )


def _row_to_account_deletion_notification_record(
    *,
    row: tuple[object, ...],
) -> AccountDeletionNotificationRecord:
    return AccountDeletionNotificationRecord(
        notification_id=str(row[0]),
        request_id=UUID(str(row[1])),
        channel=str(row[2]),  # type: ignore[arg-type]
        status=str(row[3]),  # type: ignore[arg-type]
        attempted_at=_utc_iso(_coerce_datetime(row[4])),
        event_type=str(row[5]),  # type: ignore[arg-type]
        user_id=UUID(str(row[6])),
        deletion_state=str(row[7]),  # type: ignore[arg-type]
        correlation_id=None if row[8] is None else str(row[8]),
    )


def _row_to_account_deletion_incident_record(
    *,
    row: tuple[object, ...],
) -> AccountDeletionIncidentRecord:
    return AccountDeletionIncidentRecord(
        audit_reference_id=str(row[0]),
        incident_code=str(row[1]),
        message=str(row[2]),
        reason=str(row[3]),
        request_id=UUID(str(row[4])),
        actor_user_id=UUID(str(row[5])),
        tenant_id=str(row[6]),
        account_deletion_state=str(row[7]),
        occurred_at=_utc_iso(_coerce_datetime(row[8])),
        correlation_id=None if row[9] is None else str(row[9]),
    )


def _coerce_blocker_reasons(value: object) -> tuple[PrecheckReasonCode, ...]:
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, list):
        return ()
    return tuple(str(entry) for entry in parsed)  # type: ignore[return-value]


def _coerce_datetime(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def _coerce_optional_iso(value: object) -> str | None:
    if value is None:
        return None
    return _utc_iso(_coerce_datetime(value))


def _coerce_int(value: object) -> int:
    return int(str(value))


def build_default_account_deletion_request_store() -> (
    AccountDeletionRequestStoreProtocol
):
    """Build the account-deletion store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryAccountDeletionRequestStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableAccountDeletionRequestStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentAccountDeletionRequestStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableAccountDeletionRequestStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailableAccountDeletionRequestStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_account_deletion_request_store = (
    build_default_account_deletion_request_store()
)


def get_default_account_deletion_request_store() -> (
    AccountDeletionRequestStoreProtocol
):
    """Return deterministic process-local deletion request store."""

    return _default_account_deletion_request_store


def reset_default_account_deletion_request_store() -> None:
    """Reset process-local deletion request store for isolated tests."""

    global _default_account_deletion_request_store
    _default_account_deletion_request_store = (
        build_default_account_deletion_request_store()
    )


def parse_account_deletion_request_payload(
    payload: object,
) -> AccountDeletionRequestCreateRequest:
    """Parse deterministic account deletion request payload."""

    if not isinstance(payload, dict):
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_request",
            message="Invalid account deletion request payload.",
            reason="invalid_account_deletion_request",
        )
    try:
        request_model = AccountDeletionRequestCreateRequest.model_validate(
            payload
        )
    except Exception as error:
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_request",
            message="Invalid account deletion request payload.",
            reason="invalid_account_deletion_request",
        ) from error
    normalized_reason = request_model.request_reason.strip()
    if not normalized_reason:
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_request",
            message="Invalid account deletion request payload.",
            reason="invalid_account_deletion_request",
            details={"field": "request_reason"},
        )
    return AccountDeletionRequestCreateRequest(request_reason=normalized_reason)


def parse_authenticated_principal(
    *,
    authorization_header: str | None,
    unauthorized_error_code: str,
    unauthorized_message: str,
    unauthorized_reason: str,
) -> AuthPrincipal:
    """Parse deterministic authenticated principal from Authorization header."""

    if authorization_header is None:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code=unauthorized_error_code,
            message=unauthorized_message,
            reason=unauthorized_reason,
        )
    normalized_header = authorization_header.strip()
    if not normalized_header.startswith("Bearer "):
        raise AccountDeletionRequestError(
            status_code=401,
            error_code=unauthorized_error_code,
            message=unauthorized_message,
            reason=unauthorized_reason,
        )
    encoded_context = normalized_header.removeprefix("Bearer ").strip()
    if not encoded_context:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code=unauthorized_error_code,
            message=unauthorized_message,
            reason=unauthorized_reason,
        )
    segments = [
        segment.strip()
        for segment in encoded_context.split(";")
        if segment.strip()
    ]
    parsed: dict[str, str] = {}
    for segment in segments:
        key, separator, value = segment.partition("=")
        if separator != "=":
            continue
        parsed[key.strip().lower()] = value.strip()

    user_id_raw = parsed.get("user_id", "")
    tenant_id = parsed.get("tenant_id", "")
    role = parsed.get("role", "")
    if not user_id_raw or not tenant_id or not role:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code=unauthorized_error_code,
            message=unauthorized_message,
            reason=unauthorized_reason,
        )
    try:
        user_id = UUID(user_id_raw)
    except ValueError as error:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code=unauthorized_error_code,
            message=unauthorized_message,
            reason=unauthorized_reason,
        ) from error

    return AuthPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
    )


def parse_account_deletion_confirm_payload(
    payload: object,
) -> AccountDeletionConfirmRequest:
    """Parse deterministic account deletion confirmation payload."""

    if not isinstance(payload, dict):
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_confirm_request",
            message="Invalid account deletion confirmation payload.",
            reason="invalid_account_deletion_confirm_request",
        )
    try:
        request_model = AccountDeletionConfirmRequest.model_validate(payload)
    except Exception as error:
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_confirm_request",
            message="Invalid account deletion confirmation payload.",
            reason="invalid_account_deletion_confirm_request",
        ) from error
    normalized_reauth_proof = request_model.reauth_proof.strip()
    if not normalized_reauth_proof:
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_confirm_request",
            message="Invalid account deletion confirmation payload.",
            reason="invalid_account_deletion_confirm_request",
            details={"field": "reauth_proof"},
        )
    return AccountDeletionConfirmRequest(
        request_id=request_model.request_id,
        reauth_proof=normalized_reauth_proof,
        otp_verification_id=request_model.otp_verification_id,
    )


def parse_account_deletion_cancel_payload(
    payload: object,
) -> AccountDeletionCancelRequest:
    """Parse deterministic account deletion cancellation payload."""

    if not isinstance(payload, dict):
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_cancel_request",
            message="Invalid account deletion cancellation payload.",
            reason="invalid_account_deletion_cancel_request",
        )
    try:
        request_model = AccountDeletionCancelRequest.model_validate(payload)
    except Exception as error:
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_cancel_request",
            message="Invalid account deletion cancellation payload.",
            reason="invalid_account_deletion_cancel_request",
        ) from error
    return request_model


def parse_account_deletion_execute_payload(
    payload: object,
) -> AccountDeletionExecuteRequest:
    """Parse deterministic account deletion execution payload."""

    if not isinstance(payload, dict):
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_execute_request",
            message="Invalid account deletion execution payload.",
            reason="invalid_account_deletion_execute_request",
        )
    try:
        request_model = AccountDeletionExecuteRequest.model_validate(payload)
    except Exception as error:
        raise AccountDeletionRequestError(
            status_code=400,
            error_code="invalid_account_deletion_execute_request",
            message="Invalid account deletion execution payload.",
            reason="invalid_account_deletion_execute_request",
        ) from error
    return request_model


def create_account_deletion_request(
    *,
    payload: object,
    authorization_header: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    registration_store: RegistrationStoreProtocol,
    account_deletion_store: AccountDeletionRequestStoreProtocol,
) -> AccountDeletionRequestResponse:
    """Create deterministic account deletion request without execution side effects."""

    request_model = parse_account_deletion_request_payload(payload)
    principal = parse_authenticated_principal(
        authorization_header=authorization_header,
        unauthorized_error_code="account_deletion_request_unauthorized",
        unauthorized_message="Authentication is required for account deletion request creation.",
        unauthorized_reason="account_deletion_request_unauthorized",
    )
    registered_user = registration_store.get_user_by_id(
        user_id=principal.user_id
    )
    if registered_user is None:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code="account_deletion_request_unauthorized",
            message="Authentication is required for account deletion request creation.",
            reason="account_deletion_request_unauthorized",
        )
    if registered_user.account_state not in {"active", "locked"}:
        raise AccountDeletionRequestError(
            status_code=409,
            error_code="account_deletion_request_ineligible_state",
            message="Account deletion request is not allowed for current account state.",
            reason="account_deletion_request_ineligible_state",
            details={
                "current_state": registered_user.account_state,
                "requested_state": "requested",
            },
        )

    requested_at = _utc_now_iso()
    blocker_reasons = (
        account_deletion_store.evaluate_deletion_precheck_blockers(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
        )
    )
    request_fingerprint = (
        f"account_deletion_request:{principal.user_id}:{principal.tenant_id}:"
        f"{request_model.request_reason}:{principal.role}"
    )
    return account_deletion_store.create_or_replay_request(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        request_reason=request_model.request_reason,
        blocker_reasons=blocker_reasons,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        requested_at=requested_at,
        correlation_id=correlation_id,
    )


def confirm_account_deletion_request(
    *,
    payload: object,
    authorization_header: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    registration_store: RegistrationStoreProtocol,
    account_deletion_store: AccountDeletionRequestStoreProtocol,
) -> AccountDeletionConfirmResponse:
    """Confirm deterministic account deletion request after proof validation."""

    request_model = parse_account_deletion_confirm_payload(payload)
    principal = parse_authenticated_principal(
        authorization_header=authorization_header,
        unauthorized_error_code="account_deletion_confirm_unauthorized",
        unauthorized_message="Authentication is required for account deletion confirmation.",
        unauthorized_reason="account_deletion_confirm_unauthorized",
    )
    registered_user = registration_store.get_user_by_id(
        user_id=principal.user_id
    )
    if registered_user is None:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code="account_deletion_confirm_unauthorized",
            message="Authentication is required for account deletion confirmation.",
            reason="account_deletion_confirm_unauthorized",
        )

    request_fingerprint = (
        f"account_deletion_confirm:{principal.user_id}:{principal.tenant_id}:{principal.role}:"
        f"{request_model.request_id}:"
        f"{request_model.reauth_proof}:"
        f"{request_model.otp_verification_id}"
    )
    return account_deletion_store.create_or_replay_confirmation(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        request_id=request_model.request_id,
        reauth_proof=request_model.reauth_proof,
        otp_verification_id=request_model.otp_verification_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        correlation_id=correlation_id,
    )


def cancel_account_deletion_request(
    *,
    payload: object,
    authorization_header: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    registration_store: RegistrationStoreProtocol,
    account_deletion_store: AccountDeletionRequestStoreProtocol,
) -> AccountDeletionCancelResponse:
    """Cancel deterministic account deletion request during cooldown window."""

    request_model = parse_account_deletion_cancel_payload(payload)
    principal = parse_authenticated_principal(
        authorization_header=authorization_header,
        unauthorized_error_code="account_deletion_cancel_unauthorized",
        unauthorized_message="Authentication is required for account deletion cancellation.",
        unauthorized_reason="account_deletion_cancel_unauthorized",
    )
    registered_user = registration_store.get_user_by_id(
        user_id=principal.user_id
    )
    if registered_user is None:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code="account_deletion_cancel_unauthorized",
            message="Authentication is required for account deletion cancellation.",
            reason="account_deletion_cancel_unauthorized",
        )

    request_fingerprint = (
        f"account_deletion_cancel:{principal.user_id}:{principal.tenant_id}:{principal.role}:"
        f"{request_model.request_id}"
    )
    return account_deletion_store.create_or_replay_cancel(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        request_id=request_model.request_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        correlation_id=correlation_id,
    )


def execute_account_deletion_request(
    *,
    payload: object,
    authorization_header: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    registration_store: RegistrationStoreProtocol,
    account_deletion_store: AccountDeletionRequestStoreProtocol,
) -> AccountDeletionExecuteResponse:
    """Execute deterministic account deletion with session/credential security closure."""

    request_model = parse_account_deletion_execute_payload(payload)
    principal = parse_authenticated_principal(
        authorization_header=authorization_header,
        unauthorized_error_code="account_deletion_execute_not_allowed",
        unauthorized_message="Authentication is required for account deletion execution.",
        unauthorized_reason="account_deletion_execute_not_allowed",
    )
    registered_user = registration_store.get_user_by_id(
        user_id=principal.user_id
    )
    if registered_user is None:
        raise AccountDeletionRequestError(
            status_code=401,
            error_code="account_deletion_execute_not_allowed",
            message="Authentication is required for account deletion execution.",
            reason="account_deletion_execute_not_allowed",
        )

    request_fingerprint = (
        f"account_deletion_execute:{principal.user_id}:{principal.tenant_id}:{principal.role}:"
        f"{request_model.request_id}"
    )
    return account_deletion_store.create_or_replay_execution(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        request_id=request_model.request_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        correlation_id=correlation_id,
        registration_store=registration_store,
    )


def _utc_now_iso() -> str:
    return _utc_iso(datetime.now(UTC))


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
