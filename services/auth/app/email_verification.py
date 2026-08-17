"""Implement deterministic email-verification challenge and verify lifecycle."""

from __future__ import annotations

import re
from html import escape
from uuid import UUID
from uuid import uuid4
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

from services.auth.app.config import get_auth_otp_policy_for_purpose
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import PersistentRegistrationStore
from services.auth.app.registration import _row_to_registered_user_record
from services.auth.app.account_lifecycle import AccountStateError
from services.auth.app.account_lifecycle import require_account_action_allowed
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import AuthCockroachTransactionError
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import validate_auth_database_connection
from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome
from services.auth.app.otp_delivery_adapters import EmailOtpDeliveryAdapterProtocol
from services.auth.app.otp_delivery_adapters import normalize_email_delivery_outcome
from services.auth.app.otp_delivery_adapters import get_default_email_otp_delivery_adapter

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_PATTERN = re.compile(r"^\d{4,12}$")
EMAIL_VERIFICATION_CHANNEL = "email"
AUTH_LOG_EVENT_EMAIL_VERIFICATION = "auth.otp.email"
_SUPPORTED_OTP_ISSUANCE_PURPOSES: frozenset[str] = frozenset(
    {
        "registration_verify",
        "login_step_up",
        "recovery",
        "account_deletion_confirm",
    }
)


class EmailVerificationChallengeEnvelope(BaseModel):
    """Represent challenge-issuance envelope for email verification."""

    status: Literal["challenge_issued"]
    challenge_id: UUID
    expires_at: str


class EmailVerificationVerifyEnvelope(BaseModel):
    """Represent successful email-verification response envelope."""

    status: Literal["verified"]
    verification_status: Literal["verified"]


class EmailVerificationChallengeRequest(BaseModel):
    """Represent challenge issuance request payload."""

    purpose: str
    channel: str
    email: str


class EmailVerificationVerifyRequest(BaseModel):
    """Represent email-verification token verify payload."""

    challenge_id: UUID
    otp_code: str


@dataclass(frozen=True)
class EmailVerificationChallengeRecord:
    """Represent one persisted email-verification challenge record."""

    challenge_id: UUID
    purpose: str
    email_normalized: str
    otp_code: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    failed_attempt_count: int
    max_attempts: int
    cooldown_seconds: int
    cooldown_expires_at: datetime | None = None
    request_fingerprint: str | None = None


@dataclass(frozen=True)
class _ChallengeIdempotencyRecord:
    """Represent deterministic idempotency replay record for challenge issuance."""

    request_fingerprint: str
    response: EmailVerificationChallengeEnvelope


class EmailVerificationError(ValueError):
    """Represent deterministic email-verification request/verify failure."""

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


class EmailVerificationStoreProtocol(Protocol):
    """Define persistence boundary for email-verification challenge records."""

    def issue_challenge(
        self,
        *,
        purpose: str,
        email_normalized: str,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
        resend_min_interval_seconds: int = 0,
        resend_max_per_window: int = 1000,
        resend_window_seconds: int = 3600,
        cooldown_seconds: int = 1800,
    ) -> EmailVerificationChallengeEnvelope:
        """Create or replay challenge issuance deterministically."""

        ...

    def get_challenge(self, *, challenge_id: UUID) -> EmailVerificationChallengeRecord | None:
        """Return challenge record by identifier when present."""

        ...

    def mark_challenge_consumed(
        self,
        *,
        challenge_id: UUID,
        consumed_at: datetime,
    ) -> None:
        """Mark one challenge as consumed to prevent replay."""

        ...

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
        attempted_at: datetime | None = None,
    ) -> EmailVerificationChallengeRecord:
        """Increment failed attempt count for one challenge and return updated record."""

        ...

    def get_active_cooldown_expires_at(
        self,
        *,
        purpose: str,
        email_normalized: str,
        as_of: datetime,
    ) -> datetime | None:
        """Return active cooldown expiry for one purpose/subject scope when present."""

        ...


class InMemoryEmailVerificationStore:
    """Persist email-verification challenges in memory for deterministic baseline."""

    def __init__(self) -> None:
        self._challenges_by_id: dict[UUID, EmailVerificationChallengeRecord] = {}
        self._idempotency_records: dict[str, _ChallengeIdempotencyRecord] = {}
        self._resend_history_by_subject: dict[tuple[str, str], list[datetime]] = {}
        self._cooldown_by_subject: dict[tuple[str, str], datetime] = {}
        self._lock = Lock()

    def issue_challenge(
        self,
        *,
        purpose: str,
        email_normalized: str,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
        resend_min_interval_seconds: int = 0,
        resend_max_per_window: int = 1000,
        resend_window_seconds: int = 3600,
        cooldown_seconds: int = 1800,
    ) -> EmailVerificationChallengeEnvelope:
        with self._lock:
            existing_idempotency_record = self._idempotency_records.get(idempotency_key)
            if existing_idempotency_record is not None:
                if existing_idempotency_record.request_fingerprint != request_fingerprint:
                    raise EmailVerificationError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message=("Idempotency key conflicts with an existing challenge request."),
                        reason="idempotency_key_reused_with_different_request",
                    )
                return existing_idempotency_record.response

            subject_key = (purpose, email_normalized)
            active_cooldown_expires_at = self._get_active_cooldown_expires_at_locked(
                subject_key=subject_key,
                as_of=issued_at,
            )
            if active_cooldown_expires_at is not None:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_cooldown_active",
                    message="OTP challenge cooldown is active.",
                    reason="otp_cooldown_active",
                    details={
                        "retry_after_seconds": _remaining_seconds(
                            from_time=issued_at,
                            until_time=active_cooldown_expires_at,
                        ),
                        "window_expires_at": _utc_iso(active_cooldown_expires_at),
                        "attempts_remaining": 0,
                    },
                )

            resend_history = self._prune_resend_history_locked(
                subject_key=subject_key,
                issued_at=issued_at,
                resend_window_seconds=resend_window_seconds,
            )
            resend_count_in_window = max(0, len(resend_history) - 1)
            if resend_count_in_window >= resend_max_per_window:
                window_expires_at = resend_history[0] + timedelta(seconds=resend_window_seconds)
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_resend_limit_reached",
                    message="OTP challenge resend limit is reached.",
                    reason="otp_resend_limit_reached",
                    details={
                        "retry_after_seconds": _remaining_seconds(
                            from_time=issued_at,
                            until_time=window_expires_at,
                        ),
                        "resend_remaining_count": 0,
                        "window_expires_at": _utc_iso(window_expires_at),
                    },
                )

            latest_issue = resend_history[-1] if resend_history else None
            if latest_issue is not None:
                elapsed_seconds = (issued_at - latest_issue).total_seconds()
                if elapsed_seconds < resend_min_interval_seconds:
                    retry_after_seconds = max(
                        1,
                        int(resend_min_interval_seconds - elapsed_seconds + 0.999),
                    )
                    window_expires_at = resend_history[0] + timedelta(seconds=resend_window_seconds)
                    raise EmailVerificationError(
                        status_code=409,
                        error_code="otp_resend_throttled",
                        message="Email verification challenge resend is throttled.",
                        reason="otp_resend_throttled",
                        details={
                            "retry_after_seconds": retry_after_seconds,
                            "resend_remaining_count": max(
                                0,
                                resend_max_per_window - resend_count_in_window,
                            ),
                            "window_expires_at": _utc_iso(window_expires_at),
                        },
                    )

            self._invalidate_active_challenges_locked(
                purpose=purpose,
                email_normalized=email_normalized,
                consumed_at=issued_at,
            )
            challenge_id = uuid4()
            otp_code = _build_otp_code(
                challenge_id=challenge_id,
                email_normalized=email_normalized,
                issued_at=issued_at,
            )
            challenge_record = EmailVerificationChallengeRecord(
                challenge_id=challenge_id,
                purpose=purpose,
                email_normalized=email_normalized,
                otp_code=otp_code,
                issued_at=issued_at,
                expires_at=expires_at,
                consumed_at=None,
                failed_attempt_count=0,
                max_attempts=max_attempts,
                cooldown_seconds=cooldown_seconds,
            )
            response = EmailVerificationChallengeEnvelope(
                status="challenge_issued",
                challenge_id=challenge_record.challenge_id,
                expires_at=_utc_iso(challenge_record.expires_at),
            )
            resend_history.append(issued_at)
            self._resend_history_by_subject[subject_key] = resend_history
            self._challenges_by_id[challenge_id] = challenge_record
            self._idempotency_records[idempotency_key] = _ChallengeIdempotencyRecord(
                request_fingerprint=request_fingerprint,
                response=response,
            )
            return response

    def get_challenge(self, *, challenge_id: UUID) -> EmailVerificationChallengeRecord | None:
        with self._lock:
            return self._challenges_by_id.get(challenge_id)

    def mark_challenge_consumed(
        self,
        *,
        challenge_id: UUID,
        consumed_at: datetime,
    ) -> None:
        with self._lock:
            existing_record = self._challenges_by_id[challenge_id]
            self._challenges_by_id[challenge_id] = EmailVerificationChallengeRecord(
                challenge_id=existing_record.challenge_id,
                purpose=existing_record.purpose,
                email_normalized=existing_record.email_normalized,
                otp_code=existing_record.otp_code,
                issued_at=existing_record.issued_at,
                expires_at=existing_record.expires_at,
                consumed_at=consumed_at,
                failed_attempt_count=existing_record.failed_attempt_count,
                max_attempts=existing_record.max_attempts,
                cooldown_seconds=existing_record.cooldown_seconds,
            )

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
        attempted_at: datetime | None = None,
    ) -> EmailVerificationChallengeRecord:
        with self._lock:
            existing_record = self._challenges_by_id[challenge_id]
            now = attempted_at or datetime.now(UTC)
            updated_record = EmailVerificationChallengeRecord(
                challenge_id=existing_record.challenge_id,
                purpose=existing_record.purpose,
                email_normalized=existing_record.email_normalized,
                otp_code=existing_record.otp_code,
                issued_at=existing_record.issued_at,
                expires_at=existing_record.expires_at,
                consumed_at=existing_record.consumed_at,
                failed_attempt_count=existing_record.failed_attempt_count + 1,
                max_attempts=existing_record.max_attempts,
                cooldown_seconds=existing_record.cooldown_seconds,
                cooldown_expires_at=(
                    now + timedelta(seconds=existing_record.cooldown_seconds)
                    if existing_record.failed_attempt_count + 1 >= existing_record.max_attempts
                    else existing_record.cooldown_expires_at
                ),
                request_fingerprint=existing_record.request_fingerprint,
            )
            if updated_record.failed_attempt_count >= updated_record.max_attempts:
                self._cooldown_by_subject[
                    (updated_record.purpose, updated_record.email_normalized)
                ] = now + timedelta(seconds=updated_record.cooldown_seconds)
            self._challenges_by_id[challenge_id] = updated_record
            return updated_record

    def get_active_cooldown_expires_at(
        self,
        *,
        purpose: str,
        email_normalized: str,
        as_of: datetime,
    ) -> datetime | None:
        with self._lock:
            return self._get_active_cooldown_expires_at_locked(
                subject_key=(purpose, email_normalized),
                as_of=as_of,
            )

    def get_otp_code_for_challenge(self, *, challenge_id: UUID) -> str:
        """Return challenge OTP code for deterministic local tests only."""

        with self._lock:
            return self._challenges_by_id[challenge_id].otp_code

    def force_expire_challenge(self, *, challenge_id: UUID) -> None:
        """Force one challenge into expired state for deterministic local tests only."""

        with self._lock:
            existing_record = self._challenges_by_id[challenge_id]
            self._challenges_by_id[challenge_id] = EmailVerificationChallengeRecord(
                challenge_id=existing_record.challenge_id,
                purpose=existing_record.purpose,
                email_normalized=existing_record.email_normalized,
                otp_code=existing_record.otp_code,
                issued_at=existing_record.issued_at,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                consumed_at=existing_record.consumed_at,
                failed_attempt_count=existing_record.failed_attempt_count,
                max_attempts=existing_record.max_attempts,
                cooldown_seconds=existing_record.cooldown_seconds,
            )

    def force_backdate_subject_state(
        self,
        *,
        purpose: str,
        email_normalized: str,
        seconds: int,
    ) -> None:
        """Backdate resend/cooldown timestamps for deterministic local tests only."""

        with self._lock:
            subject_key = (purpose, email_normalized)
            history = self._resend_history_by_subject.get(subject_key, [])
            if history:
                self._resend_history_by_subject[subject_key] = [
                    issued_at - timedelta(seconds=seconds) for issued_at in history
                ]
            cooldown_expires_at = self._cooldown_by_subject.get(subject_key)
            if cooldown_expires_at is not None:
                self._cooldown_by_subject[subject_key] = cooldown_expires_at - timedelta(
                    seconds=seconds
                )

    def _invalidate_active_challenges_locked(
        self,
        *,
        purpose: str,
        email_normalized: str,
        consumed_at: datetime,
    ) -> None:
        for challenge_id, challenge_record in self._challenges_by_id.items():
            if (
                challenge_record.purpose != purpose
                or challenge_record.email_normalized != email_normalized
                or challenge_record.consumed_at is not None
            ):
                continue
            self._challenges_by_id[challenge_id] = EmailVerificationChallengeRecord(
                challenge_id=challenge_record.challenge_id,
                purpose=challenge_record.purpose,
                email_normalized=challenge_record.email_normalized,
                otp_code=challenge_record.otp_code,
                issued_at=challenge_record.issued_at,
                expires_at=challenge_record.expires_at,
                consumed_at=consumed_at,
                failed_attempt_count=challenge_record.failed_attempt_count,
                max_attempts=challenge_record.max_attempts,
                cooldown_seconds=challenge_record.cooldown_seconds,
            )

    def _prune_resend_history_locked(
        self,
        *,
        subject_key: tuple[str, str],
        issued_at: datetime,
        resend_window_seconds: int,
    ) -> list[datetime]:
        history = self._resend_history_by_subject.get(subject_key, [])
        if not history:
            return []
        pruned = [
            timestamp
            for timestamp in history
            if (issued_at - timestamp).total_seconds() < resend_window_seconds
        ]
        self._resend_history_by_subject[subject_key] = pruned
        return pruned

    def _get_active_cooldown_expires_at_locked(
        self,
        *,
        subject_key: tuple[str, str],
        as_of: datetime,
    ) -> datetime | None:
        cooldown_expires_at = self._cooldown_by_subject.get(subject_key)
        if cooldown_expires_at is None:
            return None
        if cooldown_expires_at <= as_of:
            self._cooldown_by_subject.pop(subject_key, None)
            return None
        return cooldown_expires_at


class UnavailableEmailVerificationStore:
    """Fail closed when production email-verification persistence is unavailable."""

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

    def issue_challenge(
        self,
        *,
        purpose: str,
        email_normalized: str,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
        resend_min_interval_seconds: int = 0,
        resend_max_per_window: int = 1000,
        resend_window_seconds: int = 3600,
        cooldown_seconds: int = 1800,
    ) -> EmailVerificationChallengeEnvelope:
        del (
            purpose,
            email_normalized,
            idempotency_key,
            request_fingerprint,
            issued_at,
            expires_at,
            max_attempts,
            resend_min_interval_seconds,
            resend_max_per_window,
            resend_window_seconds,
            cooldown_seconds,
        )
        raise self._error()

    def get_challenge(self, *, challenge_id: UUID) -> EmailVerificationChallengeRecord | None:
        del challenge_id
        raise self._error()

    def mark_challenge_consumed(
        self,
        *,
        challenge_id: UUID,
        consumed_at: datetime,
    ) -> None:
        del challenge_id, consumed_at
        raise self._error()

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
        attempted_at: datetime | None = None,
    ) -> EmailVerificationChallengeRecord:
        del challenge_id, attempted_at
        raise self._error()

    def get_active_cooldown_expires_at(
        self,
        *,
        purpose: str,
        email_normalized: str,
        as_of: datetime,
    ) -> datetime | None:
        del purpose, email_normalized, as_of
        raise self._error()

    def get_otp_code_for_challenge(self, *, challenge_id: UUID) -> str:
        del challenge_id
        raise self._error()

    def force_expire_challenge(self, *, challenge_id: UUID) -> None:
        del challenge_id
        raise self._error()

    def force_backdate_subject_state(
        self,
        *,
        purpose: str,
        email_normalized: str,
        seconds: int,
    ) -> None:
        del purpose, email_normalized, seconds
        raise self._error()

    def _error(self) -> EmailVerificationError:
        return EmailVerificationError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentEmailVerificationStore:
    """Persist email-verification challenges in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def issue_challenge(
        self,
        *,
        purpose: str,
        email_normalized: str,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
        resend_min_interval_seconds: int = 0,
        resend_max_per_window: int = 1000,
        resend_window_seconds: int = 3600,
        cooldown_seconds: int = 1800,
    ) -> EmailVerificationChallengeEnvelope:
        challenge_id = uuid4()
        otp_code = _build_otp_code(
            challenge_id=challenge_id,
            email_normalized=email_normalized,
            issued_at=issued_at,
        )

        def _transaction(
            connection: psycopg.Connection[object],
        ) -> EmailVerificationChallengeEnvelope:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        challenge_id,
                        purpose,
                        subject_normalized,
                        otp_code,
                        issued_at,
                        expires_at,
                        consumed_at,
                        failed_attempt_count,
                        max_attempts,
                        cooldown_seconds,
                        cooldown_expires_at,
                        request_fingerprint
                    FROM auth_otp_challenges
                    WHERE channel = %s
                      AND idempotency_key = %s
                    """,
                    (EMAIL_VERIFICATION_CHANNEL, idempotency_key),
                )
                existing_row = cursor.fetchone()
                if existing_row is not None:
                    existing_record = _row_to_email_verification_record(row=existing_row)
                    if existing_record.request_fingerprint != request_fingerprint:
                        raise EmailVerificationError(
                            status_code=409,
                            error_code="idempotency_key_conflict",
                            message=(
                                "Idempotency key conflicts with an existing challenge request."
                            ),
                            reason="idempotency_key_reused_with_different_request",
                        )
                    return EmailVerificationChallengeEnvelope(
                        status="challenge_issued",
                        challenge_id=existing_record.challenge_id,
                        expires_at=_utc_iso(existing_record.expires_at),
                    )

                cursor.execute(
                    """
                    SELECT cooldown_expires_at
                    FROM auth_otp_challenges
                    WHERE channel = %s
                      AND purpose = %s
                      AND subject_normalized = %s
                      AND cooldown_expires_at IS NOT NULL
                      AND cooldown_expires_at > %s
                    ORDER BY cooldown_expires_at DESC
                    LIMIT 1
                    """,
                    (
                        EMAIL_VERIFICATION_CHANNEL,
                        purpose,
                        email_normalized,
                        issued_at,
                    ),
                )
                cooldown_row = cursor.fetchone()
                if cooldown_row is not None:
                    cooldown_expires_at = _coerce_datetime(cooldown_row[0])
                    raise EmailVerificationError(
                        status_code=409,
                        error_code="otp_cooldown_active",
                        message="OTP challenge cooldown is active.",
                        reason="otp_cooldown_active",
                        details={
                            "retry_after_seconds": _remaining_seconds(
                                from_time=issued_at,
                                until_time=cooldown_expires_at,
                            ),
                            "window_expires_at": _utc_iso(cooldown_expires_at),
                            "attempts_remaining": 0,
                        },
                    )

                cursor.execute(
                    """
                    SELECT issued_at
                    FROM auth_otp_challenges
                    WHERE channel = %s
                      AND purpose = %s
                      AND subject_normalized = %s
                      AND issued_at >= %s
                    ORDER BY issued_at ASC
                    """,
                    (
                        EMAIL_VERIFICATION_CHANNEL,
                        purpose,
                        email_normalized,
                        issued_at - timedelta(seconds=resend_window_seconds),
                    ),
                )
                resend_history = [
                    _coerce_datetime(row[0])
                    for row in cursor.fetchall()
                    if row and row[0] is not None
                ]
                resend_count_in_window = max(0, len(resend_history) - 1)
                if resend_count_in_window >= resend_max_per_window:
                    window_expires_at = resend_history[0] + timedelta(
                        seconds=resend_window_seconds
                    )
                    raise EmailVerificationError(
                        status_code=409,
                        error_code="otp_resend_limit_reached",
                        message="OTP challenge resend limit is reached.",
                        reason="otp_resend_limit_reached",
                        details={
                            "retry_after_seconds": _remaining_seconds(
                                from_time=issued_at,
                                until_time=window_expires_at,
                            ),
                            "resend_remaining_count": 0,
                            "window_expires_at": _utc_iso(window_expires_at),
                        },
                    )

                latest_issue = resend_history[-1] if resend_history else None
                if latest_issue is not None:
                    elapsed_seconds = (issued_at - latest_issue).total_seconds()
                    if elapsed_seconds < resend_min_interval_seconds:
                        retry_after_seconds = max(
                            1,
                            int(resend_min_interval_seconds - elapsed_seconds + 0.999),
                        )
                        window_expires_at = resend_history[0] + timedelta(
                            seconds=resend_window_seconds
                        )
                        raise EmailVerificationError(
                            status_code=409,
                            error_code="otp_resend_throttled",
                            message="Email verification challenge resend is throttled.",
                            reason="otp_resend_throttled",
                            details={
                                "retry_after_seconds": retry_after_seconds,
                                "resend_remaining_count": max(
                                    0,
                                    resend_max_per_window - resend_count_in_window,
                                ),
                                "window_expires_at": _utc_iso(window_expires_at),
                            },
                        )

                cursor.execute(
                    """
                    INSERT INTO auth_otp_challenges (
                        challenge_id,
                        channel,
                        purpose,
                        subject_normalized,
                        otp_code,
                        issued_at,
                        expires_at,
                        consumed_at,
                        failed_attempt_count,
                        max_attempts,
                        cooldown_seconds,
                        cooldown_expires_at,
                        idempotency_key,
                        request_fingerprint
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, 0, %s, %s, NULL, %s, %s)
                    ON CONFLICT (channel, idempotency_key) DO NOTHING
                    RETURNING challenge_id
                    """,
                    (
                        challenge_id,
                        EMAIL_VERIFICATION_CHANNEL,
                        purpose,
                        email_normalized,
                        otp_code,
                        issued_at,
                        expires_at,
                        max_attempts,
                        cooldown_seconds,
                        idempotency_key,
                        request_fingerprint,
                    ),
                )
                inserted_row = cursor.fetchone()
                if inserted_row is None:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            purpose,
                            subject_normalized,
                            otp_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            cooldown_seconds,
                            cooldown_expires_at,
                            request_fingerprint
                        FROM auth_otp_challenges
                        WHERE channel = %s
                          AND idempotency_key = %s
                        """,
                        (EMAIL_VERIFICATION_CHANNEL, idempotency_key),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is None:
                        raise _email_verification_missing_state()
                    existing_record = _row_to_email_verification_record(row=existing_row)
                    if existing_record.request_fingerprint != request_fingerprint:
                        raise EmailVerificationError(
                            status_code=409,
                            error_code="idempotency_key_conflict",
                            message=(
                                "Idempotency key conflicts with an existing challenge request."
                            ),
                            reason="idempotency_key_reused_with_different_request",
                        )
                    return EmailVerificationChallengeEnvelope(
                        status="challenge_issued",
                        challenge_id=existing_record.challenge_id,
                        expires_at=_utc_iso(existing_record.expires_at),
                    )

                cursor.execute(
                    """
                    UPDATE auth_otp_challenges
                    SET consumed_at = %s
                    WHERE channel = %s
                      AND purpose = %s
                      AND subject_normalized = %s
                      AND consumed_at IS NULL
                      AND challenge_id <> %s
                    """,
                    (
                        issued_at,
                        EMAIL_VERIFICATION_CHANNEL,
                        purpose,
                        email_normalized,
                        challenge_id,
                    ),
                )
            return EmailVerificationChallengeEnvelope(
                status="challenge_issued",
                challenge_id=challenge_id,
                expires_at=_utc_iso(expires_at),
            )

        def _reconcile() -> EmailVerificationChallengeEnvelope | None:
            try:
                with connect_auth_database(self._database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT
                                challenge_id,
                                purpose,
                                subject_normalized,
                                otp_code,
                                issued_at,
                                expires_at,
                                consumed_at,
                                failed_attempt_count,
                                max_attempts,
                                cooldown_seconds,
                                cooldown_expires_at,
                                request_fingerprint
                            FROM auth_otp_challenges
                            WHERE channel = %s
                              AND idempotency_key = %s
                            """,
                            (EMAIL_VERIFICATION_CHANNEL, idempotency_key),
                        )
                        existing_row = cursor.fetchone()
            except psycopg.Error:
                return None
            if existing_row is None:
                return None
            existing_record = _row_to_email_verification_record(row=existing_row)
            if existing_record.request_fingerprint != request_fingerprint:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="idempotency_key_conflict",
                    message="Idempotency key conflicts with an existing challenge request.",
                    reason="idempotency_key_reused_with_different_request",
                )
            return EmailVerificationChallengeEnvelope(
                status="challenge_issued",
                challenge_id=existing_record.challenge_id,
                expires_at=_utc_iso(existing_record.expires_at),
            )

        try:
            return execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction,
                reconcile_callback=_reconcile,
            )
        except EmailVerificationError:
            raise
        except AuthCockroachTransactionError as error:
            raise _email_verification_persistence_unavailable() from error
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error

    def get_challenge(self, *, challenge_id: UUID) -> EmailVerificationChallengeRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            purpose,
                            subject_normalized,
                            otp_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            cooldown_seconds,
                            cooldown_expires_at,
                            request_fingerprint
                        FROM auth_otp_challenges
                        WHERE channel = %s
                          AND challenge_id = %s
                        """,
                        (EMAIL_VERIFICATION_CHANNEL, challenge_id),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_email_verification_record(row=row)

    def mark_challenge_consumed(
        self,
        *,
        challenge_id: UUID,
        consumed_at: datetime,
    ) -> None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE auth_otp_challenges
                        SET consumed_at = %s
                        WHERE channel = %s
                          AND challenge_id = %s
                        """,
                        (consumed_at, EMAIL_VERIFICATION_CHANNEL, challenge_id),
                    )
                    if cursor.rowcount == 0:
                        raise _email_verification_missing_state()
                connection.commit()
        except EmailVerificationError:
            raise
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
        attempted_at: datetime | None = None,
    ) -> EmailVerificationChallengeRecord:
        now = attempted_at or datetime.now(UTC)
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            purpose,
                            subject_normalized,
                            otp_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            cooldown_seconds,
                            cooldown_expires_at,
                            request_fingerprint
                        FROM auth_otp_challenges
                        WHERE channel = %s
                          AND challenge_id = %s
                        FOR UPDATE
                        """,
                        (EMAIL_VERIFICATION_CHANNEL, challenge_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise _email_verification_missing_state()
                    existing_record = _row_to_email_verification_record(row=row)
                    updated_failed_attempt_count = existing_record.failed_attempt_count + 1
                    cooldown_expires_at = (
                        now + timedelta(seconds=existing_record.cooldown_seconds)
                        if updated_failed_attempt_count >= existing_record.max_attempts
                        else existing_record.cooldown_expires_at
                    )
                    cursor.execute(
                        """
                        UPDATE auth_otp_challenges
                        SET failed_attempt_count = %s,
                            cooldown_expires_at = %s
                        WHERE channel = %s
                          AND challenge_id = %s
                        RETURNING
                            challenge_id,
                            purpose,
                            subject_normalized,
                            otp_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            cooldown_seconds,
                            cooldown_expires_at,
                            request_fingerprint
                        """,
                        (
                            updated_failed_attempt_count,
                            cooldown_expires_at,
                            EMAIL_VERIFICATION_CHANNEL,
                            challenge_id,
                        ),
                    )
                    updated_row = cursor.fetchone()
                connection.commit()
        except EmailVerificationError:
            raise
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error
        if updated_row is None:
            raise _email_verification_missing_state()
        return _row_to_email_verification_record(row=updated_row)

    def get_active_cooldown_expires_at(
        self,
        *,
        purpose: str,
        email_normalized: str,
        as_of: datetime,
    ) -> datetime | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT cooldown_expires_at
                        FROM auth_otp_challenges
                        WHERE channel = %s
                          AND purpose = %s
                          AND subject_normalized = %s
                          AND cooldown_expires_at IS NOT NULL
                          AND cooldown_expires_at > %s
                        ORDER BY cooldown_expires_at DESC
                        LIMIT 1
                        """,
                        (
                            EMAIL_VERIFICATION_CHANNEL,
                            purpose,
                            email_normalized,
                            as_of,
                        ),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error
        if row is None:
            return None
        return _coerce_datetime(row[0])

    def get_otp_code_for_challenge(self, *, challenge_id: UUID) -> str:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT otp_code
                        FROM auth_otp_challenges
                        WHERE channel = %s
                          AND challenge_id = %s
                        """,
                        (EMAIL_VERIFICATION_CHANNEL, challenge_id),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error
        if row is None:
            raise _email_verification_missing_state()
        return str(row[0])

    def force_expire_challenge(self, *, challenge_id: UUID) -> None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE auth_otp_challenges
                        SET expires_at = %s
                        WHERE channel = %s
                          AND challenge_id = %s
                        """,
                        (
                            datetime.now(UTC) - timedelta(seconds=1),
                            EMAIL_VERIFICATION_CHANNEL,
                            challenge_id,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise _email_verification_missing_state()
                connection.commit()
        except EmailVerificationError:
            raise
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error

    def force_backdate_subject_state(
        self,
        *,
        purpose: str,
        email_normalized: str,
        seconds: int,
    ) -> None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    delta = timedelta(seconds=seconds)
                    cursor.execute(
                        """
                        UPDATE auth_otp_challenges
                        SET issued_at = issued_at - %s::interval,
                            cooldown_expires_at = CASE
                                WHEN cooldown_expires_at IS NULL THEN NULL
                                ELSE cooldown_expires_at - %s::interval
                            END
                        WHERE channel = %s
                          AND purpose = %s
                          AND subject_normalized = %s
                        """,
                        (
                            f"{int(delta.total_seconds())} seconds",
                            f"{int(delta.total_seconds())} seconds",
                            EMAIL_VERIFICATION_CHANNEL,
                            purpose,
                            email_normalized,
                        ),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise _email_verification_persistence_unavailable() from error


_EMAIL_VERIFICATION_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "auth_otp_challenges": (
        "challenge_id",
        "channel",
        "purpose",
        "subject_normalized",
        "otp_code",
        "issued_at",
        "expires_at",
        "consumed_at",
        "failed_attempt_count",
        "max_attempts",
        "cooldown_seconds",
        "cooldown_expires_at",
        "idempotency_key",
        "request_fingerprint",
    ),
}


def _email_verification_persistence_unavailable() -> EmailVerificationError:
    return EmailVerificationError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _email_verification_missing_state() -> EmailVerificationError:
    return EmailVerificationError(
        status_code=503,
        error_code="auth_persistence_missing_state",
        message="Required auth persistence state is missing.",
        reason="auth_persistence_missing_state",
    )


def _row_to_email_verification_record(
    *,
    row: tuple[object, ...],
) -> EmailVerificationChallengeRecord:
    return EmailVerificationChallengeRecord(
        challenge_id=UUID(str(row[0])),
        purpose=str(row[1]),
        email_normalized=str(row[2]),
        otp_code=str(row[3]),
        issued_at=_coerce_datetime(row[4]),
        expires_at=_coerce_datetime(row[5]),
        consumed_at=_coerce_optional_datetime(row[6]),
        failed_attempt_count=_coerce_int(row[7]),
        max_attempts=_coerce_int(row[8]),
        cooldown_seconds=_coerce_int(row[9]),
        cooldown_expires_at=_coerce_optional_datetime(row[10]),
        request_fingerprint=None if row[11] is None else str(row[11]),
    )


def _coerce_datetime(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def _coerce_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value)


def _coerce_int(value: object) -> int:
    return int(str(value))


def build_default_email_verification_store() -> EmailVerificationStoreProtocol:
    """Build the email-verification store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryEmailVerificationStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableEmailVerificationStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentEmailVerificationStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableEmailVerificationStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailableEmailVerificationStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_email_verification_store = build_default_email_verification_store()


def get_default_email_verification_store() -> EmailVerificationStoreProtocol:
    """Return deterministic process-local email-verification store."""

    return _default_email_verification_store


def reset_default_email_verification_store() -> None:
    """Reset process-local email-verification store for isolated tests."""

    global _default_email_verification_store
    _default_email_verification_store = build_default_email_verification_store()


def parse_email_verification_challenge_request(
    payload: object,
) -> EmailVerificationChallengeRequest:
    """Parse deterministic email-verification challenge issuance request."""

    if not isinstance(payload, dict):
        raise EmailVerificationError(
            status_code=400,
            error_code="invalid_otp_challenge_request",
            message="Invalid OTP challenge request payload.",
            reason="invalid_otp_challenge_request",
        )
    try:
        request_model = EmailVerificationChallengeRequest.model_validate(payload)
    except Exception as error:
        raise EmailVerificationError(
            status_code=400,
            error_code="invalid_otp_challenge_request",
            message="Invalid OTP challenge request payload.",
            reason="invalid_otp_challenge_request",
        ) from error

    normalized_purpose = request_model.purpose.strip()
    if normalized_purpose == "verify":
        normalized_purpose = "registration_verify"

    if normalized_purpose not in _SUPPORTED_OTP_ISSUANCE_PURPOSES:
        raise EmailVerificationError(
            status_code=400,
            error_code="invalid_otp_challenge_request",
            message="Unsupported OTP challenge purpose or context.",
            reason="unsupported_otp_challenge_context",
        )

    if request_model.channel.strip().lower() != "email":
        raise EmailVerificationError(
            status_code=400,
            error_code="invalid_otp_challenge_request",
            message="Unsupported OTP challenge purpose or context.",
            reason="unsupported_otp_challenge_context",
        )

    email_normalized = request_model.email.strip().lower()
    if _EMAIL_PATTERN.fullmatch(email_normalized) is None:
        raise EmailVerificationError(
            status_code=400,
            error_code="invalid_otp_challenge_request",
            message="Invalid OTP challenge request payload.",
            reason="invalid_otp_challenge_request",
        )
    return EmailVerificationChallengeRequest(
        purpose=normalized_purpose,
        channel="email",
        email=email_normalized,
    )


def issue_email_verification_challenge(
    *,
    request_model: EmailVerificationChallengeRequest,
    idempotency_key: str,
    email_verification_store: EmailVerificationStoreProtocol,
    email_delivery_adapter: EmailOtpDeliveryAdapterProtocol | None = None,
) -> EmailVerificationChallengeEnvelope:
    """Issue deterministic email-verification challenge with idempotency replay."""

    issued_at = datetime.now(UTC)
    otp_policy = get_auth_otp_policy_for_purpose(request_model.purpose)
    expires_at = issued_at + timedelta(seconds=otp_policy.ttl_seconds)
    resolved_email_delivery_adapter = (
        email_delivery_adapter or get_default_email_otp_delivery_adapter()
    )
    request_fingerprint = (
        f"email_verification:{request_model.purpose}:{request_model.channel}:{request_model.email}"
    )
    response = email_verification_store.issue_challenge(
        purpose=request_model.purpose,
        email_normalized=request_model.email,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        max_attempts=otp_policy.max_attempts,
        resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
        resend_max_per_window=otp_policy.resend_max_per_window,
        resend_window_seconds=otp_policy.resend_window_seconds,
        cooldown_seconds=otp_policy.cooldown_seconds,
    )
    challenge_record = email_verification_store.get_challenge(challenge_id=response.challenge_id)
    if challenge_record is None:
        raise _email_verification_missing_state()
    delivery_outcome = normalize_email_delivery_outcome(
        outcome=resolved_email_delivery_adapter.send_otp_challenge(
            message=_build_email_otp_message(
                purpose=request_model.purpose,
                email_normalized=request_model.email,
                otp_code=challenge_record.otp_code,
                challenge_id=response.challenge_id,
                expires_at=challenge_record.expires_at,
            )
        )
    )
    if delivery_outcome.status != "delivered":
        raise _build_email_delivery_failure_error(delivery_outcome=delivery_outcome)
    return response


def parse_email_verification_verify_request(
    payload: object,
) -> EmailVerificationVerifyRequest:
    """Parse deterministic email-verification verify request payload."""

    if not isinstance(payload, dict):
        raise EmailVerificationError(
            status_code=400,
            error_code="otp_challenge_invalid",
            message="OTP challenge is invalid.",
            reason="otp_challenge_invalid",
        )
    try:
        verify_request = EmailVerificationVerifyRequest.model_validate(payload)
    except Exception as error:
        raise EmailVerificationError(
            status_code=400,
            error_code="otp_challenge_invalid",
            message="OTP challenge is invalid.",
            reason="otp_challenge_invalid",
        ) from error
    if _OTP_PATTERN.fullmatch(verify_request.otp_code.strip()) is None:
        raise EmailVerificationError(
            status_code=400,
            error_code="otp_challenge_invalid",
            message="OTP challenge is invalid.",
            reason="otp_challenge_invalid",
        )
    return EmailVerificationVerifyRequest(
        challenge_id=verify_request.challenge_id,
        otp_code=verify_request.otp_code.strip(),
    )


def verify_email_verification_challenge(
    *,
    verify_request: EmailVerificationVerifyRequest,
    email_verification_store: EmailVerificationStoreProtocol,
    registration_store: RegistrationStoreProtocol,
) -> EmailVerificationVerifyEnvelope:
    """Verify one email-verification challenge and transition account state."""

    if isinstance(email_verification_store, PersistentEmailVerificationStore) and isinstance(
        registration_store, PersistentRegistrationStore
    ):
        return _verify_email_verification_challenge_persisted(
            verify_request=verify_request,
            email_verification_store=email_verification_store,
            registration_store=registration_store,
        )

    challenge_record = email_verification_store.get_challenge(
        challenge_id=verify_request.challenge_id
    )
    if challenge_record is None:
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_challenge_invalid",
            message="OTP challenge is invalid.",
            reason="otp_challenge_invalid",
        )

    if challenge_record.consumed_at is not None:
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_already_used",
            message="OTP challenge was already used.",
            reason="otp_already_used",
        )
    if challenge_record.purpose != "registration_verify":
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_challenge_context_mismatch",
            message="OTP challenge context does not match verification request.",
            reason="otp_challenge_context_mismatch",
        )

    now = datetime.now(UTC)
    active_cooldown_expires_at = email_verification_store.get_active_cooldown_expires_at(
        purpose=challenge_record.purpose,
        email_normalized=challenge_record.email_normalized,
        as_of=now,
    )
    if active_cooldown_expires_at is not None:
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_cooldown_active",
            message="OTP challenge cooldown is active.",
            reason="otp_cooldown_active",
            details={
                "retry_after_seconds": _remaining_seconds(
                    from_time=now,
                    until_time=active_cooldown_expires_at,
                ),
                "window_expires_at": _utc_iso(active_cooldown_expires_at),
                "attempts_remaining": 0,
            },
        )
    if now >= challenge_record.expires_at:
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_expired",
            message="OTP challenge has expired.",
            reason="otp_expired",
        )

    if challenge_record.failed_attempt_count >= challenge_record.max_attempts:
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_attempt_limit_exceeded",
            message="OTP challenge attempt limit is exceeded.",
            reason="otp_attempt_limit_exceeded",
            details={
                "attempts_remaining": 0,
            },
        )

    if verify_request.otp_code != challenge_record.otp_code:
        updated_record = email_verification_store.increment_failed_attempt_count(
            challenge_id=challenge_record.challenge_id,
            attempted_at=now,
        )
        attempts_remaining = max(
            0,
            updated_record.max_attempts - updated_record.failed_attempt_count,
        )
        if updated_record.failed_attempt_count >= updated_record.max_attempts:
            cooldown_expires_at = email_verification_store.get_active_cooldown_expires_at(
                purpose=updated_record.purpose,
                email_normalized=updated_record.email_normalized,
                as_of=now,
            )
            details: dict[str, object] = {
                "attempts_remaining": 0,
            }
            if cooldown_expires_at is not None:
                details["retry_after_seconds"] = _remaining_seconds(
                    from_time=now,
                    until_time=cooldown_expires_at,
                )
                details["window_expires_at"] = _utc_iso(cooldown_expires_at)
            raise EmailVerificationError(
                status_code=409,
                error_code="otp_attempt_limit_exceeded",
                message="OTP challenge attempt limit is exceeded.",
                reason="otp_attempt_limit_exceeded",
                details=details,
            )
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_invalid",
            message="OTP code is invalid.",
            reason="otp_invalid",
            details={
                "attempts_remaining": attempts_remaining,
            },
        )

    registered_user = registration_store.get_user_by_email(
        email_normalized=challenge_record.email_normalized
    )
    if registered_user is None:
        raise EmailVerificationError(
            status_code=409,
            error_code="otp_challenge_context_mismatch",
            message="OTP challenge context does not match verification request.",
            reason="otp_challenge_context_mismatch",
        )
    try:
        require_account_action_allowed(
            action="verify_email",
            current_state=registered_user.account_state,
        )
    except AccountStateError as error:
        raise EmailVerificationError(
            status_code=409,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details={
                "current_state": error.current_state,
                "requested_state": error.requested_state,
            },
        ) from error

    email_verification_store.mark_challenge_consumed(
        challenge_id=challenge_record.challenge_id,
        consumed_at=now,
    )
    try:
        registration_store.mark_user_email_verified(
            user_id=registered_user.user_id,
            verified_at=_utc_iso(now),
        )
    except AccountStateError as error:
        raise EmailVerificationError(
            status_code=409,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details={
                "current_state": error.current_state,
                "requested_state": error.requested_state,
            },
        ) from error
    return EmailVerificationVerifyEnvelope(
        status="verified",
        verification_status="verified",
    )


def _verify_email_verification_challenge_persisted(
    *,
    verify_request: EmailVerificationVerifyRequest,
    email_verification_store: PersistentEmailVerificationStore,
    registration_store: PersistentRegistrationStore,
) -> EmailVerificationVerifyEnvelope:
    now = datetime.now(UTC)

    def _transaction(
        connection: psycopg.Connection[object],
    ) -> EmailVerificationVerifyEnvelope:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    challenge_id,
                    purpose,
                    subject_normalized,
                    otp_code,
                    issued_at,
                    expires_at,
                    consumed_at,
                    failed_attempt_count,
                    max_attempts,
                    cooldown_seconds,
                    cooldown_expires_at,
                    request_fingerprint
                FROM auth_otp_challenges
                WHERE channel = %s
                  AND challenge_id = %s
                FOR UPDATE
                """,
                (EMAIL_VERIFICATION_CHANNEL, verify_request.challenge_id),
            )
            challenge_row = cursor.fetchone()
            if challenge_row is None:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_challenge_invalid",
                    message="OTP challenge is invalid.",
                    reason="otp_challenge_invalid",
                )
            challenge_record = _row_to_email_verification_record(row=challenge_row)
            if challenge_record.consumed_at is not None:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_already_used",
                    message="OTP challenge was already used.",
                    reason="otp_already_used",
                )
            if challenge_record.purpose != "registration_verify":
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_challenge_context_mismatch",
                    message="OTP challenge context does not match verification request.",
                    reason="otp_challenge_context_mismatch",
                )

            cursor.execute(
                """
                SELECT
                    id,
                    email_encrypted,
                    phone_number_encrypted,
                    kra_pin_encrypted,
                    password_hash,
                    role,
                    created_at,
                    account_state,
                    verification_state,
                    verified_at,
                    credentials_invalidated_at,
                    deletion_lifecycle_state,
                    anonymized_at,
                    password_history_hashes
                FROM users
                WHERE email_encrypted = %s
                FOR UPDATE
                """,
                (challenge_record.email_normalized,),
            )
            user_row = cursor.fetchone()
            if user_row is None:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_challenge_context_mismatch",
                    message="OTP challenge context does not match verification request.",
                    reason="otp_challenge_context_mismatch",
                )
            registered_user = _row_to_registered_user_record(row=user_row)
            try:
                require_account_action_allowed(
                    action="verify_email",
                    current_state=registered_user.account_state,
                )
            except AccountStateError as error:
                raise EmailVerificationError(
                    status_code=409,
                    error_code=error.error_code,
                    message=error.message,
                    reason=error.reason,
                    details={
                        "current_state": error.current_state,
                        "requested_state": error.requested_state,
                    },
                ) from error

            active_cooldown_expires_at = _lookup_email_cooldown_expires_at_locked(
                cursor=cursor,
                purpose=challenge_record.purpose,
                email_normalized=challenge_record.email_normalized,
                as_of=now,
            )
            if active_cooldown_expires_at is not None:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_cooldown_active",
                    message="OTP challenge cooldown is active.",
                    reason="otp_cooldown_active",
                    details={
                        "retry_after_seconds": _remaining_seconds(
                            from_time=now,
                            until_time=active_cooldown_expires_at,
                        ),
                        "window_expires_at": _utc_iso(active_cooldown_expires_at),
                        "attempts_remaining": 0,
                    },
                )
            if now >= challenge_record.expires_at:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_expired",
                    message="OTP challenge has expired.",
                    reason="otp_expired",
                )
            if challenge_record.failed_attempt_count >= challenge_record.max_attempts:
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_attempt_limit_exceeded",
                    message="OTP challenge attempt limit is exceeded.",
                    reason="otp_attempt_limit_exceeded",
                    details={"attempts_remaining": 0},
                )

            if verify_request.otp_code != challenge_record.otp_code:
                cursor.execute(
                    """
                    UPDATE auth_otp_challenges
                        SET failed_attempt_count = failed_attempt_count + 1,
                        cooldown_expires_at = CASE
                            WHEN failed_attempt_count + 1 >= max_attempts
                            THEN %s + (cooldown_seconds * interval '1 second')
                            ELSE cooldown_expires_at
                        END
                    WHERE channel = %s
                      AND challenge_id = %s
                    RETURNING
                        challenge_id,
                        purpose,
                        subject_normalized,
                        otp_code,
                        issued_at,
                        expires_at,
                        consumed_at,
                        failed_attempt_count,
                        max_attempts,
                        cooldown_seconds,
                        cooldown_expires_at,
                        request_fingerprint
                    """,
                    (
                        now,
                        EMAIL_VERIFICATION_CHANNEL,
                        challenge_record.challenge_id,
                    ),
                )
                updated_row = cursor.fetchone()
                if updated_row is None:
                    raise EmailVerificationError(
                        status_code=409,
                        error_code="otp_challenge_invalid",
                        message="OTP challenge is invalid.",
                        reason="otp_challenge_invalid",
                    )
                updated_record = _row_to_email_verification_record(row=updated_row)
                attempts_remaining = max(
                    0,
                    updated_record.max_attempts - updated_record.failed_attempt_count,
                )
                if updated_record.failed_attempt_count >= updated_record.max_attempts:
                    cooldown_expires_at = updated_record.cooldown_expires_at
                    details: dict[str, object] = {"attempts_remaining": 0}
                    if cooldown_expires_at is not None:
                        details["retry_after_seconds"] = _remaining_seconds(
                            from_time=now,
                            until_time=cooldown_expires_at,
                        )
                        details["window_expires_at"] = _utc_iso(cooldown_expires_at)
                    raise EmailVerificationError(
                        status_code=409,
                        error_code="otp_attempt_limit_exceeded",
                        message="OTP challenge attempt limit is exceeded.",
                        reason="otp_attempt_limit_exceeded",
                        details=details,
                    )
                raise EmailVerificationError(
                    status_code=409,
                    error_code="otp_invalid",
                    message="OTP code is invalid.",
                    reason="otp_invalid",
                    details={"attempts_remaining": attempts_remaining},
                )

            cursor.execute(
                """
                UPDATE auth_otp_challenges
                SET consumed_at = %s
                WHERE channel = %s
                  AND challenge_id = %s
                """,
                (now, EMAIL_VERIFICATION_CHANNEL, challenge_record.challenge_id),
            )
            cursor.execute(
                """
                UPDATE users
                SET account_state = 'active',
                    verification_state = 'verified',
                    verified_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    now,
                    now,
                    registered_user.user_id,
                ),
            )
        return EmailVerificationVerifyEnvelope(
            status="verified",
            verification_status="verified",
        )

    def _reconcile() -> EmailVerificationVerifyEnvelope | None:
        challenge_record = email_verification_store.get_challenge(
            challenge_id=verify_request.challenge_id
        )
        if challenge_record is None or challenge_record.consumed_at is None:
            return None
        registered_user = registration_store.get_user_by_email(
            email_normalized=challenge_record.email_normalized
        )
        if registered_user is None:
            return None
        if registered_user.account_state != "active":
            return None
        if registered_user.verification_state != "verified":
            return None
        if registered_user.verified_at is None:
            return None
        return EmailVerificationVerifyEnvelope(
            status="verified",
            verification_status="verified",
        )

    try:
        return execute_auth_database_transaction(
            database_url=email_verification_store._database_url,
            transaction_callback=_transaction,
            reconcile_callback=_reconcile,
        )
    except EmailVerificationError:
        raise
    except AuthCockroachTransactionError as error:
        raise _email_verification_persistence_unavailable() from error


def _lookup_email_cooldown_expires_at_locked(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    purpose: str,
    email_normalized: str,
    as_of: datetime,
) -> datetime | None:
    cursor.execute(
        """
        SELECT cooldown_expires_at
        FROM auth_otp_challenges
        WHERE channel = %s
          AND purpose = %s
          AND subject_normalized = %s
          AND cooldown_expires_at IS NOT NULL
          AND cooldown_expires_at > %s
        ORDER BY cooldown_expires_at DESC
        LIMIT 1
        """,
        (EMAIL_VERIFICATION_CHANNEL, purpose, email_normalized, as_of),
    )
    cooldown_row = cursor.fetchone()
    if cooldown_row is None:
        return None
    return _coerce_datetime(cooldown_row[0])


def _build_otp_code(
    *,
    challenge_id: UUID,
    email_normalized: str,
    issued_at: datetime,
) -> str:
    digest = sha256(f"{challenge_id}:{email_normalized}:{issued_at.isoformat()}".encode()).digest()
    numeric = int.from_bytes(digest[:4], byteorder="big") % 1_000_000
    return f"{numeric:06d}"


def _build_email_otp_message(
    *,
    purpose: str,
    email_normalized: str,
    otp_code: str,
    challenge_id: UUID,
    expires_at: datetime,
) -> EmailOtpMessage:
    normalized_purpose = purpose.strip().lower()
    subject = {
        "registration_verify": "Verify your KODI account",
        "login_step_up": "Your KODI login verification code",
        "recovery": "Your KODI recovery code",
        "account_deletion_confirm": "Confirm your KODI account deletion",
        "phone_change_confirm": "Confirm your KODI phone change",
    }.get(normalized_purpose, "Your KODI verification code")
    action_label = {
        "registration_verify": "verify your email address",
        "login_step_up": "finish signing in",
        "recovery": "continue your recovery request",
        "account_deletion_confirm": "confirm your account deletion request",
        "phone_change_confirm": "confirm your phone-number change request",
    }.get(normalized_purpose, "complete your request")
    escaped_otp_code = escape(otp_code)
    escaped_expires_at = escape(_utc_iso(expires_at))
    escaped_challenge_id = escape(str(challenge_id))
    content = (
        "<html><body>"
        f"<p>Use this KODI one-time code to {action_label}.</p>"
        f"<p><strong>{escaped_otp_code}</strong></p>"
        f"<p>This code expires at {escaped_expires_at}.</p>"
        f"<p>Challenge reference: {escaped_challenge_id}</p>"
        "</body></html>"
    )
    return EmailOtpMessage(
        purpose=normalized_purpose,
        email_normalized=email_normalized,
        subject=subject,
        content=content,
        challenge_id=str(challenge_id),
    )


def _build_email_delivery_failure_error(
    *,
    delivery_outcome: OtpDeliveryOutcome,
) -> EmailVerificationError:
    reason_code = delivery_outcome.reason_code
    status = delivery_outcome.status
    provider_ref = delivery_outcome.provider_ref
    details: dict[str, object] = {
        "delivery_failure_class": status,
    }
    if isinstance(provider_ref, str) and provider_ref:
        details["provider_ref"] = provider_ref

    if reason_code == "otp_delivery_provider_misconfigured":
        return EmailVerificationError(
            status_code=409,
            error_code="otp_delivery_provider_misconfigured",
            message="OTP delivery provider is misconfigured.",
            reason="otp_delivery_provider_misconfigured",
            details=details,
        )
    if reason_code == "email_delivery_provider_timeout":
        details["retry_after_seconds"] = 60
        return EmailVerificationError(
            status_code=409,
            error_code="otp_email_delivery_provider_timeout",
            message="OTP challenge email delivery timed out.",
            reason="otp_email_delivery_provider_timeout",
            details=details,
        )
    if reason_code == "email_delivery_provider_unavailable":
        return EmailVerificationError(
            status_code=409,
            error_code="otp_email_delivery_provider_unavailable",
            message="OTP challenge email delivery provider is unavailable.",
            reason="otp_email_delivery_provider_unavailable",
            details=details,
        )
    if reason_code == "email_delivery_provider_rejected":
        return EmailVerificationError(
            status_code=409,
            error_code="otp_email_delivery_provider_rejected",
            message="OTP challenge email delivery was rejected.",
            reason="otp_email_delivery_provider_rejected",
            details=details,
        )
    return EmailVerificationError(
        status_code=409,
        error_code="otp_email_delivery_failed",
        message="OTP challenge email delivery failed.",
        reason="otp_email_delivery_failed",
        details=details,
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _remaining_seconds(*, from_time: datetime, until_time: datetime) -> int:
    return max(1, int((until_time - from_time).total_seconds()))
