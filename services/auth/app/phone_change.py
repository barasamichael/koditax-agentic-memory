"""Implement deterministic phone-number change request and confirmation workflow."""

from __future__ import annotations

import re
from uuid import UUID
from uuid import uuid4
from typing import Literal
from typing import Protocol
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Lock
from dataclasses import replace
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel

from services.auth.app.config import get_auth_otp_policy_for_purpose
from services.auth.app.registration import RegistrationConflictError
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import verify_password_against_hash
from services.auth.app.phone_verification import PHONE_VERIFICATION_CHANNEL
from services.auth.app.phone_verification import PhoneVerificationStoreProtocol
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import validate_auth_database_connection
from services.auth.app.otp_delivery_adapters import SmsOtpDeliveryAdapterProtocol
from services.auth.app.otp_delivery_adapters import normalize_sms_delivery_outcome
from services.auth.app.otp_delivery_adapters import get_default_sms_otp_delivery_adapter

PhoneChangeState = Literal["pending_confirmation", "confirmed", "superseded"]
PhoneChangeAuditAction = Literal["phone_change_requested", "phone_change_confirmed"]
_PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")
_PHONE_CLEAN_PATTERN = re.compile(r"[\s\-\(\)]")
_KENYA_NATIONAL_PHONE_PATTERN = re.compile(r"^[17]\d{8}$")
_OTP_PATTERN = re.compile(r"^\d{4,12}$")
AUTH_LOG_EVENT_PHONE_CHANGE = "auth.phone_change"


class PhoneChangeRequestCreateRequest(BaseModel):
    """Represent phone-change request payload."""

    new_phone_number: str
    current_password: str


class PhoneChangeRequestResponse(BaseModel):
    """Represent successful phone-change request envelope."""

    status: Literal["pending_confirmation"]
    request_id: UUID
    phone_change_state: Literal["pending_confirmation"]
    step_up_challenge_id: UUID
    step_up_expires_at: str


class PhoneChangeConfirmRequest(BaseModel):
    """Represent phone-change confirmation payload."""

    request_id: UUID
    step_up_challenge_id: UUID
    step_up_otp_code: str


class PhoneChangeConfirmResponse(BaseModel):
    """Represent successful phone-change confirmation envelope."""

    status: Literal["phone_updated"]
    request_id: UUID
    phone_change_state: Literal["confirmed"]
    updated_phone_number: str
    updated_at: str


@dataclass(frozen=True)
class AuthPrincipal:
    """Represent authenticated principal context for phone-change actions."""

    user_id: UUID
    tenant_id: str
    role: str


@dataclass(frozen=True)
class PhoneChangeRequestRecord:
    """Represent one persisted phone-change request record."""

    request_id: UUID
    user_id: UUID
    tenant_id: str
    requested_at: str
    current_phone_number_normalized: str
    new_phone_number_normalized: str
    phone_change_state: PhoneChangeState
    step_up_challenge_id: UUID
    step_up_expires_at: str
    request_idempotency_key: str
    confirmed_at: str | None


@dataclass(frozen=True)
class PhoneChangeAuditRecord:
    """Represent immutable phone-change lifecycle audit evidence."""

    audit_evidence_id: str
    event_id: str
    event_type: PhoneChangeAuditAction
    user_id: UUID
    request_id: UUID
    phone_change_state: PhoneChangeState
    occurred_at: str
    correlation_id: str | None
    trace_ref: str


@dataclass(frozen=True)
class _RequestIdempotencyRecord:
    """Represent deterministic idempotency replay record for phone-change request."""

    request_fingerprint: str
    response: PhoneChangeRequestResponse


@dataclass(frozen=True)
class _ConfirmIdempotencyRecord:
    """Represent deterministic idempotency replay record for phone-change confirmation."""

    request_fingerprint: str
    response: PhoneChangeConfirmResponse


class PhoneChangeError(ValueError):
    """Represent deterministic phone-change workflow failure."""

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


class PhoneChangeStoreProtocol(Protocol):
    """Define persistence boundary for phone-change lifecycle records and evidence."""

    def create_or_replay_request(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeRequestCreateRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeRequestResponse:
        """Create or replay deterministic phone-change request state."""

        ...

    def create_or_replay_confirmation(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeConfirmRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeConfirmResponse:
        """Confirm or replay deterministic phone-change state transition."""

        ...

    def get_request_by_id(self, *, request_id: UUID) -> PhoneChangeRequestRecord | None:
        """Return one phone-change request record by identifier when present."""

        ...

    def get_audit_events_for_user(self, *, user_id: UUID) -> list[PhoneChangeAuditRecord]:
        """Return immutable phone-change audit records for one user."""

        ...


class InMemoryPhoneChangeStore:
    """Persist phone-change request lifecycle and immutable audit records in memory."""

    def __init__(self) -> None:
        self._requests_by_id: dict[UUID, PhoneChangeRequestRecord] = {}
        self._active_request_id_by_user: dict[UUID, UUID] = {}
        self._request_idempotency_records: dict[str, _RequestIdempotencyRecord] = {}
        self._confirm_idempotency_records: dict[str, _ConfirmIdempotencyRecord] = {}
        self._audit_by_user: dict[UUID, list[PhoneChangeAuditRecord]] = {}
        self._lock = Lock()

    def create_or_replay_request(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeRequestCreateRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeRequestResponse:
        with self._lock:
            existing_idempotency_record = self._request_idempotency_records.get(idempotency_key)
            if existing_idempotency_record is not None:
                if existing_idempotency_record.request_fingerprint != request_fingerprint:
                    raise PhoneChangeError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message="Idempotency key conflicts with an existing phone-change request.",
                        reason="idempotency_key_conflict",
                    )
                return existing_idempotency_record.response

            registered_user = registration_store.get_user_by_id(user_id=principal.user_id)
            if registered_user is None:
                raise PhoneChangeError(
                    status_code=401,
                    error_code="phone_change_unauthorized",
                    message="Authentication is required for phone-number change.",
                    reason="phone_change_unauthorized",
                )
            if registered_user.account_state != "active":
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_not_allowed_for_state",
                    message="Phone-number change is not allowed for current account state.",
                    reason="phone_change_not_allowed_for_state",
                    details={
                        "current_state": registered_user.account_state,
                        "requested_state": "pending_confirmation",
                    },
                )
            if not verify_password_against_hash(
                password=request_model.current_password,
                password_hash=registered_user.password_hash,
            ):
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_step_up_invalid",
                    message="Phone-number change step-up proof is invalid.",
                    reason="phone_change_step_up_invalid",
                )

            if request_model.new_phone_number == registered_user.phone_number_normalized:
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_request_invalid",
                    message="Phone-number change request is not in a valid state.",
                    reason="phone_change_request_invalid",
                    details={
                        "current_state": "phone_unchanged",
                        "requested_state": "pending_confirmation",
                    },
                )
            existing_phone_owner = registration_store.get_user_by_phone(
                phone_number_normalized=request_model.new_phone_number
            )
            if (
                existing_phone_owner is not None
                and existing_phone_owner.user_id != registered_user.user_id
            ):
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_target_phone_already_registered",
                    message="Phone-number change conflicts with an existing account.",
                    reason="phone_change_target_phone_already_registered",
                )

            existing_active_request_id = self._active_request_id_by_user.get(principal.user_id)
            if existing_active_request_id is not None:
                existing_active_request = self._requests_by_id.get(existing_active_request_id)
                if (
                    existing_active_request is not None
                    and existing_active_request.phone_change_state == "pending_confirmation"
                ):
                    if (
                        existing_active_request.new_phone_number_normalized
                        == request_model.new_phone_number
                    ):
                        now = datetime.now(UTC)
                        otp_policy = get_auth_otp_policy_for_purpose("phone_change_confirm")
                        existing_challenge = phone_verification_store.get_challenge(
                            challenge_id=existing_active_request.step_up_challenge_id
                        )
                        challenge_still_valid = (
                            existing_challenge is not None
                            and existing_challenge.consumed_at is None
                            and now < existing_challenge.expires_at
                        )
                        if challenge_still_valid:
                            return PhoneChangeRequestResponse(
                                status="pending_confirmation",
                                request_id=existing_active_request.request_id,
                                phone_change_state="pending_confirmation",
                                step_up_challenge_id=existing_active_request.step_up_challenge_id,
                                step_up_expires_at=existing_active_request.step_up_expires_at,
                            )
                        # Challenge expired — issue a fresh one and update the request record.
                        challenge_request_fingerprint = (
                            "phone_change_step_up:"
                            f"{principal.user_id}:{principal.tenant_id}"
                            f":{request_model.new_phone_number}"
                        )
                        new_challenge = phone_verification_store.issue_challenge(
                            purpose="phone_change_confirm",
                            phone_number_normalized=request_model.new_phone_number,
                            idempotency_key=_build_phone_change_challenge_idempotency_key(
                                request_fingerprint=challenge_request_fingerprint,
                            ),
                            request_fingerprint=challenge_request_fingerprint,
                            issued_at=now,
                            expires_at=now + timedelta(seconds=otp_policy.ttl_seconds),
                            max_attempts=otp_policy.max_attempts,
                            resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
                            resend_max_per_window=otp_policy.resend_max_per_window,
                            resend_window_seconds=otp_policy.resend_window_seconds,
                            cooldown_seconds=otp_policy.cooldown_seconds,
                        )
                        refreshed_record = PhoneChangeRequestRecord(
                            request_id=existing_active_request.request_id,
                            user_id=existing_active_request.user_id,
                            tenant_id=existing_active_request.tenant_id,
                            requested_at=existing_active_request.requested_at,
                            current_phone_number_normalized=existing_active_request.current_phone_number_normalized,
                            new_phone_number_normalized=existing_active_request.new_phone_number_normalized,
                            phone_change_state="pending_confirmation",
                            step_up_challenge_id=new_challenge.challenge_id,
                            step_up_expires_at=new_challenge.expires_at,
                            request_idempotency_key=existing_active_request.request_idempotency_key,
                            confirmed_at=None,
                        )
                        self._requests_by_id[existing_active_request.request_id] = refreshed_record
                        return PhoneChangeRequestResponse(
                            status="pending_confirmation",
                            request_id=refreshed_record.request_id,
                            phone_change_state="pending_confirmation",
                            step_up_challenge_id=refreshed_record.step_up_challenge_id,
                            step_up_expires_at=refreshed_record.step_up_expires_at,
                        )
                    # Different target number — supersede the stale pending request.
                    self._active_request_id_by_user.pop(principal.user_id, None)
                    self._requests_by_id[existing_active_request.request_id] = replace(
                        existing_active_request,
                        phone_change_state="superseded",
                    )

            now = datetime.now(UTC)
            otp_policy = get_auth_otp_policy_for_purpose("phone_change_confirm")
            challenge_request_fingerprint = (
                "phone_change_step_up:"
                f"{principal.user_id}:{principal.tenant_id}:{request_model.new_phone_number}"
            )
            challenge_response = phone_verification_store.issue_challenge(
                purpose="phone_change_confirm",
                phone_number_normalized=request_model.new_phone_number,
                idempotency_key=_build_phone_change_challenge_idempotency_key(
                    request_fingerprint=challenge_request_fingerprint,
                ),
                request_fingerprint=challenge_request_fingerprint,
                issued_at=now,
                expires_at=now + timedelta(seconds=otp_policy.ttl_seconds),
                max_attempts=otp_policy.max_attempts,
                resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
                resend_max_per_window=otp_policy.resend_max_per_window,
                resend_window_seconds=otp_policy.resend_window_seconds,
                cooldown_seconds=otp_policy.cooldown_seconds,
            )

            requested_at = _utc_iso(now)
            request_id = uuid4()
            request_record = PhoneChangeRequestRecord(
                request_id=request_id,
                user_id=registered_user.user_id,
                tenant_id=principal.tenant_id,
                requested_at=requested_at,
                current_phone_number_normalized=registered_user.phone_number_normalized,
                new_phone_number_normalized=request_model.new_phone_number,
                phone_change_state="pending_confirmation",
                step_up_challenge_id=challenge_response.challenge_id,
                step_up_expires_at=challenge_response.expires_at,
                request_idempotency_key=idempotency_key,
                confirmed_at=None,
            )
            response = PhoneChangeRequestResponse(
                status="pending_confirmation",
                request_id=request_record.request_id,
                phone_change_state="pending_confirmation",
                step_up_challenge_id=request_record.step_up_challenge_id,
                step_up_expires_at=request_record.step_up_expires_at,
            )
            self._requests_by_id[request_id] = request_record
            self._active_request_id_by_user[principal.user_id] = request_id
            self._request_idempotency_records[idempotency_key] = _RequestIdempotencyRecord(
                request_fingerprint=request_fingerprint,
                response=response,
            )
            self._append_audit_record_locked(
                event_type="phone_change_requested",
                user_id=principal.user_id,
                request_id=request_id,
                phone_change_state="pending_confirmation",
                occurred_at=requested_at,
                correlation_id=correlation_id,
                trace_ref=request_fingerprint,
            )
            return response

    def create_or_replay_confirmation(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeConfirmRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeConfirmResponse:
        with self._lock:
            existing_idempotency_record = self._confirm_idempotency_records.get(idempotency_key)
            if existing_idempotency_record is not None:
                if existing_idempotency_record.request_fingerprint != request_fingerprint:
                    raise PhoneChangeError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message=(
                            "Idempotency key conflicts with an existing phone-change confirmation."
                        ),
                        reason="idempotency_key_conflict",
                    )
                return existing_idempotency_record.response

            request_record = self._requests_by_id.get(request_model.request_id)
            if request_record is None:
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_request_invalid",
                    message="Phone-number change request is not in a valid state.",
                    reason="phone_change_request_invalid",
                )
            if (
                request_record.user_id != principal.user_id
                or request_record.tenant_id != principal.tenant_id
            ):
                raise PhoneChangeError(
                    status_code=403,
                    error_code="phone_change_unauthorized",
                    message="Phone-number change is not authorized for this identity context.",
                    reason="phone_change_unauthorized",
                )
            if request_record.phone_change_state != "pending_confirmation":
                if request_record.phone_change_state == "confirmed":
                    raise PhoneChangeError(
                        status_code=409,
                        error_code="phone_change_request_already_confirmed",
                        message="Phone-number change request was already confirmed.",
                        reason="phone_change_request_already_confirmed",
                    )
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_request_invalid",
                    message="Phone-number change request is not in a valid state.",
                    reason="phone_change_request_invalid",
                    details={
                        "current_state": request_record.phone_change_state,
                        "requested_state": "confirmed",
                    },
                )

            registered_user = registration_store.get_user_by_id(user_id=principal.user_id)
            if registered_user is None:
                raise PhoneChangeError(
                    status_code=401,
                    error_code="phone_change_unauthorized",
                    message="Authentication is required for phone-number change.",
                    reason="phone_change_unauthorized",
                )
            if registered_user.account_state != "active":
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_not_allowed_for_state",
                    message="Phone-number change request is not in a valid state.",
                    reason="phone_change_not_allowed_for_state",
                    details={
                        "current_state": registered_user.account_state,
                        "requested_state": "confirmed",
                    },
                )
            if (
                registered_user.phone_number_normalized
                != request_record.current_phone_number_normalized
            ):
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_request_invalid",
                    message="Phone-number change request is not in a valid state.",
                    reason="phone_change_request_invalid",
                    details={
                        "current_state": "phone_context_mismatch",
                        "requested_state": "confirmed",
                    },
                )
            if request_model.step_up_challenge_id != request_record.step_up_challenge_id:
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_step_up_invalid",
                    message="Phone-number change step-up proof is invalid.",
                    reason="phone_change_step_up_invalid",
                )

            challenge_record = phone_verification_store.get_challenge(
                challenge_id=request_model.step_up_challenge_id
            )
            if (
                challenge_record is None
                or challenge_record.consumed_at is not None
                or challenge_record.purpose != "phone_change_confirm"
                or challenge_record.phone_number_normalized
                != request_record.new_phone_number_normalized
            ):
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_step_up_invalid",
                    message="Phone-number change step-up proof is invalid.",
                    reason="phone_change_step_up_invalid",
                )

            now = datetime.now(UTC)
            if (
                now >= challenge_record.expires_at
                or challenge_record.failed_attempt_count >= challenge_record.max_attempts
            ):
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_step_up_expired",
                    message="Phone-number change step-up proof has expired.",
                    reason="phone_change_step_up_expired",
                )
            if request_model.step_up_otp_code != challenge_record.otp_code:
                phone_verification_store.increment_failed_attempt_count(
                    challenge_id=challenge_record.challenge_id
                )
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_step_up_invalid",
                    message="Phone-number change step-up proof is invalid.",
                    reason="phone_change_step_up_invalid",
                )

            existing_phone_owner = registration_store.get_user_by_phone(
                phone_number_normalized=request_record.new_phone_number_normalized
            )
            if (
                existing_phone_owner is not None
                and existing_phone_owner.user_id != principal.user_id
            ):
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_target_phone_already_registered",
                    message="Phone-number change conflicts with an existing account.",
                    reason="phone_change_target_phone_already_registered",
                )
            try:
                registration_store.update_user_phone_number(
                    user_id=principal.user_id,
                    phone_number_normalized=request_record.new_phone_number_normalized,
                )
            except RegistrationConflictError as error:
                raise PhoneChangeError(
                    status_code=409,
                    error_code="phone_change_target_phone_already_registered",
                    message="Phone-number change conflicts with an existing account.",
                    reason="phone_change_target_phone_already_registered",
                ) from error
            phone_verification_store.mark_challenge_consumed(
                challenge_id=challenge_record.challenge_id,
                consumed_at=now,
            )

            confirmed_at = _utc_iso(now)
            updated_request = PhoneChangeRequestRecord(
                request_id=request_record.request_id,
                user_id=request_record.user_id,
                tenant_id=request_record.tenant_id,
                requested_at=request_record.requested_at,
                current_phone_number_normalized=request_record.current_phone_number_normalized,
                new_phone_number_normalized=request_record.new_phone_number_normalized,
                phone_change_state="confirmed",
                step_up_challenge_id=request_record.step_up_challenge_id,
                step_up_expires_at=request_record.step_up_expires_at,
                request_idempotency_key=request_record.request_idempotency_key,
                confirmed_at=confirmed_at,
            )
            self._requests_by_id[request_model.request_id] = updated_request
            self._active_request_id_by_user.pop(principal.user_id, None)

            response = PhoneChangeConfirmResponse(
                status="phone_updated",
                request_id=request_model.request_id,
                phone_change_state="confirmed",
                updated_phone_number=request_record.new_phone_number_normalized,
                updated_at=confirmed_at,
            )
            self._confirm_idempotency_records[idempotency_key] = _ConfirmIdempotencyRecord(
                request_fingerprint=request_fingerprint,
                response=response,
            )
            self._append_audit_record_locked(
                event_type="phone_change_confirmed",
                user_id=principal.user_id,
                request_id=request_model.request_id,
                phone_change_state="confirmed",
                occurred_at=confirmed_at,
                correlation_id=correlation_id,
                trace_ref=request_fingerprint,
            )
            return response

    def get_request_by_id(self, *, request_id: UUID) -> PhoneChangeRequestRecord | None:
        with self._lock:
            return self._requests_by_id.get(request_id)

    def get_audit_events_for_user(self, *, user_id: UUID) -> list[PhoneChangeAuditRecord]:
        with self._lock:
            return [*self._audit_by_user.get(user_id, [])]

    def _append_audit_record_locked(
        self,
        *,
        event_type: PhoneChangeAuditAction,
        user_id: UUID,
        request_id: UUID,
        phone_change_state: PhoneChangeState,
        occurred_at: str,
        correlation_id: str | None,
        trace_ref: str,
    ) -> None:
        digest = sha256(
            (
                f"{event_type}:{user_id}:{request_id}:{phone_change_state}:{occurred_at}:"
                f"{correlation_id or ''}:{trace_ref}"
            ).encode()
        ).hexdigest()
        record = PhoneChangeAuditRecord(
            audit_evidence_id=digest,
            event_id=digest,
            event_type=event_type,
            user_id=user_id,
            request_id=request_id,
            phone_change_state=phone_change_state,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            trace_ref=trace_ref,
        )
        existing_records = self._audit_by_user.get(user_id, [])
        self._audit_by_user[user_id] = [*existing_records, record]


class UnavailablePhoneChangeStore:
    """Fail closed when production phone-change persistence is unavailable."""

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
        principal: AuthPrincipal,
        request_model: PhoneChangeRequestCreateRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeRequestResponse:
        del (
            principal,
            request_model,
            idempotency_key,
            request_fingerprint,
            correlation_id,
            registration_store,
            phone_verification_store,
        )
        raise self._error()

    def create_or_replay_confirmation(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeConfirmRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeConfirmResponse:
        del (
            principal,
            request_model,
            idempotency_key,
            request_fingerprint,
            correlation_id,
            registration_store,
            phone_verification_store,
        )
        raise self._error()

    def get_request_by_id(self, *, request_id: UUID) -> PhoneChangeRequestRecord | None:
        del request_id
        raise self._error()

    def get_audit_events_for_user(self, *, user_id: UUID) -> list[PhoneChangeAuditRecord]:
        del user_id
        raise self._error()

    def _error(self) -> PhoneChangeError:
        return PhoneChangeError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentPhoneChangeStore:
    """Persist phone-change lifecycle state in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def create_or_replay_request(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeRequestCreateRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeRequestResponse:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            request_id,
                            user_id,
                            tenant_id,
                            requested_at,
                            current_phone_number_normalized,
                            new_phone_number_normalized,
                            phone_change_state,
                            step_up_challenge_id,
                            step_up_expires_at,
                            request_idempotency_key,
                            confirmed_at,
                            request_fingerprint,
                            confirm_idempotency_key,
                            confirm_request_fingerprint
                        FROM auth_phone_change_requests
                        WHERE request_idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        if str(existing_row[11]) != request_fingerprint:
                            raise PhoneChangeError(
                                status_code=409,
                                error_code="idempotency_key_conflict",
                                message=(
                                    "Idempotency key conflicts with an existing "
                                    "phone-change request."
                                ),
                                reason="idempotency_key_conflict",
                            )
                        existing_record = _row_to_phone_change_request_record(row=existing_row)
                        return PhoneChangeRequestResponse(
                            status="pending_confirmation",
                            request_id=existing_record.request_id,
                            phone_change_state="pending_confirmation",
                            step_up_challenge_id=existing_record.step_up_challenge_id,
                            step_up_expires_at=existing_record.step_up_expires_at,
                        )

                    registered_user = registration_store.get_user_by_id(user_id=principal.user_id)
                    if registered_user is None:
                        raise PhoneChangeError(
                            status_code=401,
                            error_code="phone_change_unauthorized",
                            message="Authentication is required for phone-number change.",
                            reason="phone_change_unauthorized",
                        )
                    if registered_user.account_state != "active":
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_not_allowed_for_state",
                            message="Phone-number change is not allowed for current account state.",
                            reason="phone_change_not_allowed_for_state",
                            details={
                                "current_state": registered_user.account_state,
                                "requested_state": "pending_confirmation",
                            },
                        )
                    if not verify_password_against_hash(
                        password=request_model.current_password,
                        password_hash=registered_user.password_hash,
                    ):
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_step_up_invalid",
                            message="Phone-number change step-up proof is invalid.",
                            reason="phone_change_step_up_invalid",
                        )
                    if request_model.new_phone_number == registered_user.phone_number_normalized:
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_request_invalid",
                            message="Phone-number change request is not in a valid state.",
                            reason="phone_change_request_invalid",
                            details={
                                "current_state": "phone_unchanged",
                                "requested_state": "pending_confirmation",
                            },
                        )
                    existing_phone_owner = registration_store.get_user_by_phone(
                        phone_number_normalized=request_model.new_phone_number
                    )
                    if (
                        existing_phone_owner is not None
                        and existing_phone_owner.user_id != registered_user.user_id
                    ):
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_target_phone_already_registered",
                            message="Phone-number change conflicts with an existing account.",
                            reason="phone_change_target_phone_already_registered",
                        )

                    cursor.execute(
                        """
                        SELECT
                            request_id,
                            phone_change_state,
                            new_phone_number_normalized,
                            step_up_challenge_id,
                            step_up_expires_at
                        FROM auth_phone_change_requests
                        WHERE user_id = %s
                          AND phone_change_state = 'pending_confirmation'
                        ORDER BY requested_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        (principal.user_id,),
                    )
                    active_row = cursor.fetchone()
                    if active_row is not None:
                        active_new_phone = str(active_row[2])
                        if active_new_phone == request_model.new_phone_number:
                            active_request_id = UUID(str(active_row[0]))
                            active_challenge_id = UUID(str(active_row[3]))
                            active_expires_at = (
                                _to_datetime(str(active_row[4]))
                                if isinstance(active_row[4], str)
                                else active_row[4].astimezone(UTC)  # type: ignore[union-attr]
                            )
                            now = datetime.now(UTC)
                            otp_policy = get_auth_otp_policy_for_purpose("phone_change_confirm")
                            existing_challenge = phone_verification_store.get_challenge(
                                challenge_id=active_challenge_id
                            )
                            challenge_still_valid = (
                                existing_challenge is not None
                                and existing_challenge.consumed_at is None
                                and now < existing_challenge.expires_at
                            )
                            if challenge_still_valid:
                                return PhoneChangeRequestResponse(
                                    status="pending_confirmation",
                                    request_id=active_request_id,
                                    phone_change_state="pending_confirmation",
                                    step_up_challenge_id=active_challenge_id,
                                    step_up_expires_at=_utc_iso(active_expires_at),
                                )
                            # Challenge expired — issue a fresh one and update the row.
                            challenge_request_fingerprint = (
                                "phone_change_step_up:"
                                f"{principal.user_id}:{principal.tenant_id}"
                                f":{request_model.new_phone_number}"
                            )
                            new_challenge = phone_verification_store.issue_challenge(
                                purpose="phone_change_confirm",
                                phone_number_normalized=request_model.new_phone_number,
                                idempotency_key=
                                _build_phone_change_challenge_idempotency_key(
                                    request_fingerprint=challenge_request_fingerprint,
                                ),
                                request_fingerprint=challenge_request_fingerprint,
                                issued_at=now,
                                expires_at=now + timedelta(seconds=otp_policy.ttl_seconds),
                                max_attempts=otp_policy.max_attempts,
                                resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
                                resend_max_per_window=otp_policy.resend_max_per_window,
                                resend_window_seconds=otp_policy.resend_window_seconds,
                                cooldown_seconds=otp_policy.cooldown_seconds,
                            )
                            cursor.execute(
                                """
                                UPDATE auth_phone_change_requests
                                SET step_up_challenge_id = %s,
                                    step_up_expires_at = %s
                                WHERE request_id = %s
                                """,
                                (
                                    new_challenge.challenge_id,
                                    now + timedelta(seconds=otp_policy.ttl_seconds),
                                    active_request_id,
                                ),
                            )
                            return PhoneChangeRequestResponse(
                                status="pending_confirmation",
                                request_id=active_request_id,
                                phone_change_state="pending_confirmation",
                                step_up_challenge_id=new_challenge.challenge_id,
                                step_up_expires_at=new_challenge.expires_at,
                            )
                        # Different target number — supersede the stale pending request.
                        cursor.execute(
                            """
                            UPDATE auth_phone_change_requests
                            SET phone_change_state = 'superseded'
                            WHERE request_id = %s
                            """,
                            (active_row[0],),
                        )

                    now = datetime.now(UTC)
                    otp_policy = get_auth_otp_policy_for_purpose("phone_change_confirm")
                    challenge_request_fingerprint = (
                        "phone_change_step_up:"
                        f"{principal.user_id}:{principal.tenant_id}:{request_model.new_phone_number}"
                    )
                    connection.commit()
                    challenge_response = phone_verification_store.issue_challenge(
                        purpose="phone_change_confirm",
                        phone_number_normalized=request_model.new_phone_number,
                        idempotency_key=_build_phone_change_challenge_idempotency_key(
                            request_fingerprint=challenge_request_fingerprint,
                        ),
                        request_fingerprint=challenge_request_fingerprint,
                        issued_at=now,
                        expires_at=now + timedelta(seconds=otp_policy.ttl_seconds),
                        max_attempts=otp_policy.max_attempts,
                        resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
                        resend_max_per_window=otp_policy.resend_max_per_window,
                        resend_window_seconds=otp_policy.resend_window_seconds,
                        cooldown_seconds=otp_policy.cooldown_seconds,
                    )
                    requested_at = now
                    request_id = uuid4()
                    cursor.execute(
                        """
                        INSERT INTO auth_phone_change_requests (
                            request_id,
                            user_id,
                            tenant_id,
                            requested_at,
                            current_phone_number_normalized,
                            new_phone_number_normalized,
                            phone_change_state,
                            step_up_challenge_id,
                            step_up_expires_at,
                            request_idempotency_key,
                            request_fingerprint,
                            confirmed_at,
                            confirm_idempotency_key,
                            confirm_request_fingerprint
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, 'pending_confirmation',
                            %s, %s, %s, %s, NULL, NULL, NULL
                        )
                        """,
                        (
                            request_id,
                            registered_user.user_id,
                            principal.tenant_id,
                            requested_at,
                            registered_user.phone_number_normalized,
                            request_model.new_phone_number,
                            challenge_response.challenge_id,
                            _to_datetime(challenge_response.expires_at),
                            idempotency_key,
                            request_fingerprint,
                        ),
                    )
                    self._append_audit_event(
                        cursor=cursor,
                        user_id=principal.user_id,
                        request_id=request_id,
                        event_type="phone_change_requested",
                        phone_change_state="pending_confirmation",
                        occurred_at=requested_at,
                        correlation_id=correlation_id,
                        trace_ref=request_fingerprint,
                    )
                connection.commit()
        except PhoneChangeError:
            raise
        except psycopg.Error as error:
            raise _phone_change_persistence_unavailable() from error
        return PhoneChangeRequestResponse(
            status="pending_confirmation",
            request_id=request_id,
            phone_change_state="pending_confirmation",
            step_up_challenge_id=challenge_response.challenge_id,
            step_up_expires_at=challenge_response.expires_at,
        )

    def create_or_replay_confirmation(
        self,
        *,
        principal: AuthPrincipal,
        request_model: PhoneChangeConfirmRequest,
        idempotency_key: str,
        request_fingerprint: str,
        correlation_id: str | None,
        registration_store: RegistrationStoreProtocol,
        phone_verification_store: PhoneVerificationStoreProtocol,
    ) -> PhoneChangeConfirmResponse:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            request_id,
                            user_id,
                            tenant_id,
                            requested_at,
                            current_phone_number_normalized,
                            new_phone_number_normalized,
                            phone_change_state,
                            step_up_challenge_id,
                            step_up_expires_at,
                            request_idempotency_key,
                            confirmed_at,
                            request_fingerprint,
                            confirm_idempotency_key,
                            confirm_request_fingerprint
                        FROM auth_phone_change_requests
                        WHERE confirm_idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        if str(existing_row[13]) != request_fingerprint:
                            raise PhoneChangeError(
                                status_code=409,
                                error_code="idempotency_key_conflict",
                                message=(
                                    "Idempotency key conflicts with an existing "
                                    "phone-change confirmation."
                                ),
                                reason="idempotency_key_conflict",
                            )
                        existing_record = _row_to_phone_change_request_record(row=existing_row)
                        if existing_record.confirmed_at is None:
                            raise _phone_change_missing_state()
                        return PhoneChangeConfirmResponse(
                            status="phone_updated",
                            request_id=existing_record.request_id,
                            phone_change_state="confirmed",
                            updated_phone_number=existing_record.new_phone_number_normalized,
                            updated_at=existing_record.confirmed_at,
                        )

                    cursor.execute(
                        """
                        SELECT
                            request_id,
                            user_id,
                            tenant_id,
                            requested_at,
                            current_phone_number_normalized,
                            new_phone_number_normalized,
                            phone_change_state,
                            step_up_challenge_id,
                            step_up_expires_at,
                            request_idempotency_key,
                            confirmed_at,
                            request_fingerprint,
                            confirm_idempotency_key,
                            confirm_request_fingerprint
                        FROM auth_phone_change_requests
                        WHERE request_id = %s
                        FOR UPDATE
                        """,
                        (request_model.request_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_request_invalid",
                            message="Phone-number change request is not in a valid state.",
                            reason="phone_change_request_invalid",
                        )
                    request_record = _row_to_phone_change_request_record(row=row)
                    if (
                        request_record.user_id != principal.user_id
                        or request_record.tenant_id != principal.tenant_id
                    ):
                        raise PhoneChangeError(
                            status_code=403,
                            error_code="phone_change_unauthorized",
                            message=(
                                "Phone-number change is not authorized for this identity context."
                            ),
                            reason="phone_change_unauthorized",
                        )
                    if request_record.phone_change_state != "pending_confirmation":
                        if request_record.phone_change_state == "confirmed":
                            raise PhoneChangeError(
                                status_code=409,
                                error_code="phone_change_request_already_confirmed",
                                message="Phone-number change request was already confirmed.",
                                reason="phone_change_request_already_confirmed",
                            )
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_request_invalid",
                            message="Phone-number change request is not in a valid state.",
                            reason="phone_change_request_invalid",
                            details={
                                "current_state": request_record.phone_change_state,
                                "requested_state": "confirmed",
                            },
                        )

                    registered_user = registration_store.get_user_by_id(user_id=principal.user_id)
                    if registered_user is None:
                        raise PhoneChangeError(
                            status_code=401,
                            error_code="phone_change_unauthorized",
                            message="Authentication is required for phone-number change.",
                            reason="phone_change_unauthorized",
                        )
                    if registered_user.account_state != "active":
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_not_allowed_for_state",
                            message="Phone-number change request is not in a valid state.",
                            reason="phone_change_not_allowed_for_state",
                            details={
                                "current_state": registered_user.account_state,
                                "requested_state": "confirmed",
                            },
                        )
                    if (
                        registered_user.phone_number_normalized
                        != request_record.current_phone_number_normalized
                    ):
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_request_invalid",
                            message="Phone-number change request is not in a valid state.",
                            reason="phone_change_request_invalid",
                            details={
                                "current_state": "phone_context_mismatch",
                                "requested_state": "confirmed",
                            },
                        )
                    if request_model.step_up_challenge_id != request_record.step_up_challenge_id:
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_step_up_invalid",
                            message="Phone-number change step-up proof is invalid.",
                            reason="phone_change_step_up_invalid",
                        )

                    challenge_record = phone_verification_store.get_challenge(
                        challenge_id=request_model.step_up_challenge_id
                    )
                    if (
                        challenge_record is None
                        or challenge_record.consumed_at is not None
                        or challenge_record.purpose != "phone_change_confirm"
                        or challenge_record.phone_number_normalized
                        != request_record.new_phone_number_normalized
                    ):
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_step_up_invalid",
                            message="Phone-number change step-up proof is invalid.",
                            reason="phone_change_step_up_invalid",
                        )

                    now = datetime.now(UTC)
                    if (
                        now >= challenge_record.expires_at
                        or challenge_record.failed_attempt_count >= challenge_record.max_attempts
                    ):
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_step_up_expired",
                            message="Phone-number change step-up proof has expired.",
                            reason="phone_change_step_up_expired",
                        )
                    if request_model.step_up_otp_code != challenge_record.otp_code:
                        phone_verification_store.increment_failed_attempt_count(
                            challenge_id=challenge_record.challenge_id
                        )
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_step_up_invalid",
                            message="Phone-number change step-up proof is invalid.",
                            reason="phone_change_step_up_invalid",
                        )

                    existing_phone_owner = registration_store.get_user_by_phone(
                        phone_number_normalized=request_record.new_phone_number_normalized
                    )
                    if (
                        existing_phone_owner is not None
                        and existing_phone_owner.user_id != principal.user_id
                    ):
                        raise PhoneChangeError(
                            status_code=409,
                            error_code="phone_change_target_phone_already_registered",
                            message="Phone-number change conflicts with an existing account.",
                            reason="phone_change_target_phone_already_registered",
                        )
                    cursor.execute(
                        """
                        UPDATE users
                        SET phone_number_encrypted = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            request_record.new_phone_number_normalized,
                            principal.user_id,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE auth_otp_challenges
                        SET consumed_at = %s
                        WHERE channel = %s
                          AND challenge_id = %s
                        """,
                        (
                            now,
                            PHONE_VERIFICATION_CHANNEL,
                            challenge_record.challenge_id,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE auth_phone_change_requests
                        SET phone_change_state = 'confirmed',
                            confirmed_at = %s,
                            confirm_idempotency_key = %s,
                            confirm_request_fingerprint = %s
                        WHERE request_id = %s
                        """,
                        (
                            now,
                            idempotency_key,
                            request_fingerprint,
                            request_model.request_id,
                        ),
                    )
                    self._append_audit_event(
                        cursor=cursor,
                        user_id=principal.user_id,
                        request_id=request_model.request_id,
                        event_type="phone_change_confirmed",
                        phone_change_state="confirmed",
                        occurred_at=now,
                        correlation_id=correlation_id,
                        trace_ref=request_fingerprint,
                    )
                connection.commit()
        except PhoneChangeError:
            raise
        except psycopg.Error as error:
            raise _phone_change_persistence_unavailable() from error
        return PhoneChangeConfirmResponse(
            status="phone_updated",
            request_id=request_model.request_id,
            phone_change_state="confirmed",
            updated_phone_number=request_record.new_phone_number_normalized,
            updated_at=_utc_iso(now),
        )

    def get_request_by_id(self, *, request_id: UUID) -> PhoneChangeRequestRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            request_id,
                            user_id,
                            tenant_id,
                            requested_at,
                            current_phone_number_normalized,
                            new_phone_number_normalized,
                            phone_change_state,
                            step_up_challenge_id,
                            step_up_expires_at,
                            request_idempotency_key,
                            confirmed_at,
                            request_fingerprint,
                            confirm_idempotency_key,
                            confirm_request_fingerprint
                        FROM auth_phone_change_requests
                        WHERE request_id = %s
                        """,
                        (request_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _phone_change_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_phone_change_request_record(row=row)

    def get_audit_events_for_user(self, *, user_id: UUID) -> list[PhoneChangeAuditRecord]:
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
                            phone_change_state,
                            occurred_at,
                            correlation_id,
                            trace_ref
                        FROM auth_phone_change_audit_events
                        WHERE user_id = %s
                        ORDER BY occurred_at ASC, created_at ASC
                        """,
                        (user_id,),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise _phone_change_persistence_unavailable() from error
        return [_row_to_phone_change_audit_record(row=row) for row in rows]

    def _append_audit_event(
        self,
        *,
        cursor: psycopg.Cursor[object],
        user_id: UUID,
        request_id: UUID,
        event_type: PhoneChangeAuditAction,
        phone_change_state: PhoneChangeState,
        occurred_at: datetime,
        correlation_id: str | None,
        trace_ref: str,
    ) -> None:
        digest = sha256(
            (
                f"{event_type}:{user_id}:{request_id}:{phone_change_state}:{_utc_iso(occurred_at)}:"
                f"{correlation_id or ''}:{trace_ref}"
            ).encode()
        ).hexdigest()
        cursor.execute(
            """
            INSERT INTO auth_phone_change_audit_events (
                audit_evidence_id,
                event_id,
                event_type,
                user_id,
                request_id,
                phone_change_state,
                occurred_at,
                correlation_id,
                trace_ref
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (audit_evidence_id) DO NOTHING
            """,
            (
                digest,
                digest,
                event_type,
                user_id,
                request_id,
                phone_change_state,
                occurred_at,
                correlation_id,
                trace_ref,
            ),
        )


_PHONE_CHANGE_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "auth_phone_change_requests": (
        "request_id",
        "user_id",
        "tenant_id",
        "requested_at",
        "current_phone_number_normalized",
        "new_phone_number_normalized",
        "phone_change_state",
        "step_up_challenge_id",
        "step_up_expires_at",
        "request_idempotency_key",
        "request_fingerprint",
        "confirmed_at",
        "confirm_idempotency_key",
        "confirm_request_fingerprint",
    ),
    "auth_phone_change_audit_events": (
        "audit_evidence_id",
        "event_id",
        "event_type",
        "user_id",
        "request_id",
        "phone_change_state",
        "occurred_at",
        "correlation_id",
        "trace_ref",
    ),
}


def _phone_change_persistence_unavailable() -> PhoneChangeError:
    return PhoneChangeError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _phone_change_missing_state() -> PhoneChangeError:
    return PhoneChangeError(
        status_code=503,
        error_code="auth_persistence_missing_state",
        message="Required auth persistence state is missing.",
        reason="auth_persistence_missing_state",
    )


def _row_to_phone_change_request_record(*, row: tuple[object, ...]) -> PhoneChangeRequestRecord:
    return PhoneChangeRequestRecord(
        request_id=UUID(str(row[0])),
        user_id=UUID(str(row[1])),
        tenant_id=str(row[2]),
        requested_at=_utc_iso(_coerce_datetime(row[3])),
        current_phone_number_normalized=str(row[4]),
        new_phone_number_normalized=str(row[5]),
        phone_change_state=str(row[6]),  # type: ignore[arg-type]
        step_up_challenge_id=UUID(str(row[7])),
        step_up_expires_at=_utc_iso(_coerce_datetime(row[8])),
        request_idempotency_key=str(row[9]),
        confirmed_at=None if row[10] is None else _utc_iso(_coerce_datetime(row[10])),
    )


def _row_to_phone_change_audit_record(*, row: tuple[object, ...]) -> PhoneChangeAuditRecord:
    return PhoneChangeAuditRecord(
        audit_evidence_id=str(row[0]),
        event_id=str(row[1]),
        event_type=str(row[2]),  # type: ignore[arg-type]
        user_id=UUID(str(row[3])),
        request_id=UUID(str(row[4])),
        phone_change_state=str(row[5]),  # type: ignore[arg-type]
        occurred_at=_utc_iso(_coerce_datetime(row[6])),
        correlation_id=None if row[7] is None else str(row[7]),
        trace_ref=str(row[8]),
    )


def _coerce_datetime(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def _to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _build_phone_change_challenge_idempotency_key(*, request_fingerprint: str) -> str:
    return sha256(f"phone_change_confirm:{request_fingerprint}".encode()).hexdigest()


def build_default_phone_change_store() -> PhoneChangeStoreProtocol:
    """Build the phone-change store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryPhoneChangeStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailablePhoneChangeStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentPhoneChangeStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailablePhoneChangeStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailablePhoneChangeStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_phone_change_store = build_default_phone_change_store()


def get_default_phone_change_store() -> PhoneChangeStoreProtocol:
    """Return deterministic process-local phone-change store."""

    return _default_phone_change_store


def reset_default_phone_change_store() -> None:
    """Reset process-local phone-change store for isolated tests."""

    global _default_phone_change_store
    _default_phone_change_store = build_default_phone_change_store()


def parse_phone_change_request_payload(payload: object) -> PhoneChangeRequestCreateRequest:
    """Parse deterministic phone-change request payload."""

    if not isinstance(payload, dict):
        raise PhoneChangeError(
            status_code=400,
            error_code="phone_change_request_invalid",
            message="Invalid phone-change request payload.",
            reason="phone_change_request_invalid",
        )
    try:
        request_model = PhoneChangeRequestCreateRequest.model_validate(payload)
    except Exception as error:
        raise PhoneChangeError(
            status_code=400,
            error_code="phone_change_request_invalid",
            message="Invalid phone-change request payload.",
            reason="phone_change_request_invalid",
        ) from error

    normalized_phone_number = _normalize_kenyan_phone_number(request_model.new_phone_number)
    if _PHONE_PATTERN.fullmatch(normalized_phone_number) is None:
        raise PhoneChangeError(
            status_code=400,
            error_code="phone_change_target_phone_invalid",
            message="Phone-number format is invalid for phone-change workflow.",
            reason="phone_change_target_phone_invalid",
        )
    normalized_password = request_model.current_password.strip()
    if not normalized_password:
        raise PhoneChangeError(
            status_code=403,
            error_code="phone_change_step_up_required",
            message="Phone-number change step-up proof is required.",
            reason="phone_change_step_up_required",
        )

    return PhoneChangeRequestCreateRequest(
        new_phone_number=normalized_phone_number,
        current_password=normalized_password,
    )


def parse_phone_change_confirm_payload(payload: object) -> PhoneChangeConfirmRequest:
    """Parse deterministic phone-change confirmation payload."""

    if not isinstance(payload, dict):
        raise PhoneChangeError(
            status_code=400,
            error_code="phone_change_request_invalid",
            message="Invalid phone-change confirmation payload.",
            reason="phone_change_request_invalid",
        )
    try:
        request_model = PhoneChangeConfirmRequest.model_validate(payload)
    except Exception as error:
        raise PhoneChangeError(
            status_code=400,
            error_code="phone_change_request_invalid",
            message="Invalid phone-change confirmation payload.",
            reason="phone_change_request_invalid",
        ) from error

    normalized_otp_code = request_model.step_up_otp_code.strip()
    if not normalized_otp_code:
        raise PhoneChangeError(
            status_code=403,
            error_code="phone_change_step_up_required",
            message="Phone-number change step-up proof is required.",
            reason="phone_change_step_up_required",
        )
    if _OTP_PATTERN.fullmatch(normalized_otp_code) is None:
        raise PhoneChangeError(
            status_code=409,
            error_code="phone_change_step_up_invalid",
            message="Phone-number change step-up proof is invalid.",
            reason="phone_change_step_up_invalid",
        )

    return PhoneChangeConfirmRequest(
        request_id=request_model.request_id,
        step_up_challenge_id=request_model.step_up_challenge_id,
        step_up_otp_code=normalized_otp_code,
    )


def parse_authenticated_principal(*, authorization_header: str | None) -> AuthPrincipal:
    """Parse deterministic authenticated principal from Authorization header."""

    if authorization_header is None:
        raise PhoneChangeError(
            status_code=401,
            error_code="phone_change_unauthorized",
            message="Authentication is required for phone-number change.",
            reason="phone_change_unauthorized",
        )
    normalized_header = authorization_header.strip()
    if not normalized_header.startswith("Bearer "):
        raise PhoneChangeError(
            status_code=401,
            error_code="phone_change_unauthorized",
            message="Authentication is required for phone-number change.",
            reason="phone_change_unauthorized",
        )

    encoded_context = normalized_header.removeprefix("Bearer ").strip()
    if not encoded_context:
        raise PhoneChangeError(
            status_code=401,
            error_code="phone_change_unauthorized",
            message="Authentication is required for phone-number change.",
            reason="phone_change_unauthorized",
        )
    segments = [segment.strip() for segment in encoded_context.split(";") if segment.strip()]
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
        raise PhoneChangeError(
            status_code=401,
            error_code="phone_change_unauthorized",
            message="Authentication is required for phone-number change.",
            reason="phone_change_unauthorized",
        )
    try:
        user_id = UUID(user_id_raw)
    except ValueError as error:
        raise PhoneChangeError(
            status_code=401,
            error_code="phone_change_unauthorized",
            message="Authentication is required for phone-number change.",
            reason="phone_change_unauthorized",
        ) from error

    return AuthPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
    )


def create_phone_change_request(
    *,
    payload: object,
    authorization_header: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    registration_store: RegistrationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
    phone_change_store: PhoneChangeStoreProtocol,
    sms_delivery_adapter: SmsOtpDeliveryAdapterProtocol | None = None,
) -> PhoneChangeRequestResponse:
    """Create deterministic phone-change request with bound step-up challenge context."""

    request_model = parse_phone_change_request_payload(payload)
    principal = parse_authenticated_principal(authorization_header=authorization_header)
    request_fingerprint = (
        f"phone_change_request:{principal.user_id}:{principal.tenant_id}:{principal.role}:"
        f"{request_model.new_phone_number}"
    )
    response = phone_change_store.create_or_replay_request(
        principal=principal,
        request_model=request_model,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        correlation_id=correlation_id,
        registration_store=registration_store,
        phone_verification_store=phone_verification_store,
    )
    challenge_record = phone_verification_store.get_challenge(
        challenge_id=response.step_up_challenge_id
    )
    if challenge_record is None:
        raise PhoneChangeError(
            status_code=503,
            error_code="auth_persistence_missing_state",
            message="Required auth persistence state is missing.",
            reason="auth_persistence_missing_state",
        )
    resolved_sms_adapter = (
        sms_delivery_adapter
        if sms_delivery_adapter is not None
        else get_default_sms_otp_delivery_adapter()
    )
    delivery_result = normalize_sms_delivery_outcome(
        outcome=resolved_sms_adapter.send_otp_challenge(
            purpose="phone_change_confirm",
            phone_number_normalized=request_model.new_phone_number,
            otp_code=challenge_record.otp_code,
        )
    )
    if delivery_result.status != "delivered":
        raise PhoneChangeError(
            status_code=409,
            error_code="phone_change_otp_delivery_failed",
            message="Phone-number change OTP could not be delivered.",
            reason="phone_change_otp_delivery_failed",
            details={"provider_ref": delivery_result.provider_ref or ""},
        )
    return response


def confirm_phone_change_request(
    *,
    payload: object,
    authorization_header: str | None,
    idempotency_key: str,
    correlation_id: str | None,
    registration_store: RegistrationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
    phone_change_store: PhoneChangeStoreProtocol,
) -> PhoneChangeConfirmResponse:
    """Confirm deterministic phone-change request with ownership and OTP proof checks."""

    request_model = parse_phone_change_confirm_payload(payload)
    principal = parse_authenticated_principal(authorization_header=authorization_header)
    request_fingerprint = (
        f"phone_change_confirm:{principal.user_id}:{principal.tenant_id}:{principal.role}:"
        f"{request_model.request_id}:{request_model.step_up_challenge_id}"
    )
    return phone_change_store.create_or_replay_confirmation(
        principal=principal,
        request_model=request_model,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        correlation_id=correlation_id,
        registration_store=registration_store,
        phone_verification_store=phone_verification_store,
    )


def _normalize_kenyan_phone_number(phone_number: str) -> str:
    cleaned = _PHONE_CLEAN_PATTERN.sub("", phone_number.strip())
    if not cleaned:
        return ""
    if cleaned.startswith("+254"):
        national_number = cleaned[4:]
    elif cleaned.startswith("254"):
        national_number = cleaned[3:]
    elif cleaned.startswith("0"):
        national_number = cleaned[1:]
    else:
        return ""
    if _KENYA_NATIONAL_PHONE_PATTERN.fullmatch(national_number) is None:
        return ""
    return f"+254{national_number}"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
