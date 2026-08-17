"""Implement deterministic password setup/reset initiation and confirmation flow."""

from __future__ import annotations

import re
from html import escape
import json
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

from services.auth.app.config import get_password_reset_ttl_seconds
from services.auth.app.config import get_auth_password_history_depth
from services.auth.app.config import get_password_reset_max_attempts
from services.auth.app.registration import build_password_hash
from services.auth.app.registration import validate_password_policy
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import PersistentRegistrationStore
from services.auth.app.registration import RegistrationValidationError
from services.auth.app.registration import verify_password_against_hash
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import AuthCockroachTransactionError
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import validate_auth_database_connection
from services.auth.app.persistence_support import AuthCockroachTransactionAmbiguousCommitError
from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome
from services.auth.app.otp_delivery_adapters import EmailOtpDeliveryAdapterProtocol
from services.auth.app.otp_delivery_adapters import normalize_email_delivery_outcome
from services.auth.app.otp_delivery_adapters import get_default_email_otp_delivery_adapter

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")
_PHONE_CLEAN_PATTERN = re.compile(r"[\s\-\(\)]")
_RESET_CODE_PATTERN = re.compile(r"^\d{4,12}$")
_ALLOWED_PURPOSES: frozenset[str] = frozenset({"password_reset", "password_setup"})
_ALLOWED_CHANNELS: frozenset[str] = frozenset({"email", "sms"})
PASSWORD_RESET_METRICS_PURPOSE = "recovery"
AUTH_LOG_EVENT_PASSWORD_RESET = "auth.password_reset"


class PasswordResetChallengeEnvelope(BaseModel):
    """Represent deterministic password-reset challenge issuance response."""

    status: Literal["challenge_issued"]
    challenge_id: UUID
    expires_at: str


class PasswordResetConfirmEnvelope(BaseModel):
    """Represent deterministic password-reset confirmation response."""

    status: Literal["password_updated"]
    updated_at: str


class PasswordResetInitiateRequest(BaseModel):
    """Represent password-reset initiation request payload."""

    purpose: str
    channel: str
    email: str | None = None
    phone_number: str | None = None


class PasswordResetConfirmRequest(BaseModel):
    """Represent password-reset confirmation request payload."""

    challenge_id: UUID
    reset_code: str
    new_password: str


@dataclass(frozen=True)
class PasswordResetChallengeRecord:
    """Represent one persisted password-reset challenge record."""

    challenge_id: UUID
    purpose: str
    channel: Literal["email", "sms"]
    subject_normalized: str
    user_id: UUID | None
    reset_code: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    failed_attempt_count: int
    max_attempts: int
    idempotency_key: str
    request_fingerprint: str
    created_at: datetime


@dataclass(frozen=True)
class _ChallengeIdempotencyRecord:
    """Represent deterministic idempotency replay record for reset initiation."""

    request_fingerprint: str
    response: PasswordResetChallengeEnvelope


class PasswordResetError(ValueError):
    """Represent deterministic password-reset initiation/confirmation failure."""

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


class PasswordResetStoreProtocol(Protocol):
    """Define persistence boundary for password-reset challenge records."""

    def issue_challenge(
        self,
        *,
        purpose: str,
        channel: Literal["email", "sms"],
        subject_normalized: str,
        user_id: UUID | None,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
    ) -> PasswordResetChallengeEnvelope:
        """Create or replay one challenge deterministically."""

        ...

    def get_challenge(self, *, challenge_id: UUID) -> PasswordResetChallengeRecord | None:
        """Return challenge by identifier when present."""

        ...

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
    ) -> PasswordResetChallengeRecord:
        """Increment failed-attempt counter for one challenge."""

        ...

    def mark_challenge_consumed(
        self,
        *,
        challenge_id: UUID,
        consumed_at: datetime,
    ) -> None:
        """Mark one challenge as consumed to block replay."""

        ...


class InMemoryPasswordResetStore:
    """Persist password-reset challenge records in memory."""

    def __init__(self) -> None:
        self._challenges_by_id: dict[UUID, PasswordResetChallengeRecord] = {}
        self._idempotency_records: dict[str, _ChallengeIdempotencyRecord] = {}
        self._lock = Lock()

    def issue_challenge(
        self,
        *,
        purpose: str,
        channel: Literal["email", "sms"],
        subject_normalized: str,
        user_id: UUID | None,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
    ) -> PasswordResetChallengeEnvelope:
        with self._lock:
            existing_idempotency_record = self._idempotency_records.get(idempotency_key)
            if existing_idempotency_record is not None:
                if existing_idempotency_record.request_fingerprint != request_fingerprint:
                    raise PasswordResetError(
                        status_code=409,
                        error_code="idempotency_key_conflict",
                        message="Idempotency key conflicts with existing password-reset request.",
                        reason="idempotency_key_reused_with_different_request",
                    )
                return existing_idempotency_record.response

            challenge_id = uuid4()
            reset_code = _build_reset_code(
                challenge_id=challenge_id,
                subject_normalized=subject_normalized,
                issued_at=issued_at,
            )
            challenge_record = PasswordResetChallengeRecord(
                challenge_id=challenge_id,
                purpose=purpose,
                channel=channel,
                subject_normalized=subject_normalized,
                user_id=user_id,
                reset_code=reset_code,
                issued_at=issued_at,
                expires_at=expires_at,
                consumed_at=None,
                failed_attempt_count=0,
                max_attempts=max_attempts,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                created_at=issued_at,
            )
            response = PasswordResetChallengeEnvelope(
                status="challenge_issued",
                challenge_id=challenge_record.challenge_id,
                expires_at=_utc_iso(challenge_record.expires_at),
            )
            self._challenges_by_id[challenge_id] = challenge_record
            self._idempotency_records[idempotency_key] = _ChallengeIdempotencyRecord(
                request_fingerprint=request_fingerprint,
                response=response,
            )
            return response

    def get_challenge(self, *, challenge_id: UUID) -> PasswordResetChallengeRecord | None:
        with self._lock:
            return self._challenges_by_id.get(challenge_id)

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
    ) -> PasswordResetChallengeRecord:
        with self._lock:
            existing_record = self._challenges_by_id[challenge_id]
            updated_record = PasswordResetChallengeRecord(
                challenge_id=existing_record.challenge_id,
                purpose=existing_record.purpose,
                channel=existing_record.channel,
                subject_normalized=existing_record.subject_normalized,
                user_id=existing_record.user_id,
                reset_code=existing_record.reset_code,
                issued_at=existing_record.issued_at,
                expires_at=existing_record.expires_at,
                consumed_at=existing_record.consumed_at,
                failed_attempt_count=existing_record.failed_attempt_count + 1,
                max_attempts=existing_record.max_attempts,
                idempotency_key=existing_record.idempotency_key,
                request_fingerprint=existing_record.request_fingerprint,
                created_at=existing_record.created_at,
            )
            self._challenges_by_id[challenge_id] = updated_record
            return updated_record

    def mark_challenge_consumed(
        self,
        *,
        challenge_id: UUID,
        consumed_at: datetime,
    ) -> None:
        with self._lock:
            existing_record = self._challenges_by_id[challenge_id]
            self._challenges_by_id[challenge_id] = PasswordResetChallengeRecord(
                challenge_id=existing_record.challenge_id,
                purpose=existing_record.purpose,
                channel=existing_record.channel,
                subject_normalized=existing_record.subject_normalized,
                user_id=existing_record.user_id,
                reset_code=existing_record.reset_code,
                issued_at=existing_record.issued_at,
                expires_at=existing_record.expires_at,
                consumed_at=consumed_at,
                failed_attempt_count=existing_record.failed_attempt_count,
                max_attempts=existing_record.max_attempts,
                idempotency_key=existing_record.idempotency_key,
                request_fingerprint=existing_record.request_fingerprint,
                created_at=existing_record.created_at,
            )

    def get_reset_code_for_challenge(self, *, challenge_id: UUID) -> str:
        """Return challenge code for deterministic local tests only."""

        with self._lock:
            return self._challenges_by_id[challenge_id].reset_code

    def force_expire_challenge(self, *, challenge_id: UUID) -> None:
        """Force one challenge into expired state for deterministic local tests only."""

        with self._lock:
            existing_record = self._challenges_by_id[challenge_id]
            self._challenges_by_id[challenge_id] = PasswordResetChallengeRecord(
                challenge_id=existing_record.challenge_id,
                purpose=existing_record.purpose,
                channel=existing_record.channel,
                subject_normalized=existing_record.subject_normalized,
                user_id=existing_record.user_id,
                reset_code=existing_record.reset_code,
                issued_at=existing_record.issued_at,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                consumed_at=existing_record.consumed_at,
                failed_attempt_count=existing_record.failed_attempt_count,
                max_attempts=existing_record.max_attempts,
                idempotency_key=existing_record.idempotency_key,
                request_fingerprint=existing_record.request_fingerprint,
                created_at=existing_record.created_at,
            )


class UnavailablePasswordResetStore:
    """Fail closed when production password-reset persistence is unavailable."""

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
        channel: Literal["email", "sms"],
        subject_normalized: str,
        user_id: UUID | None,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
    ) -> PasswordResetChallengeEnvelope:
        del (
            purpose,
            channel,
            subject_normalized,
            user_id,
            idempotency_key,
            request_fingerprint,
            issued_at,
            expires_at,
            max_attempts,
        )
        raise self._error()

    def get_challenge(self, *, challenge_id: UUID) -> PasswordResetChallengeRecord | None:
        del challenge_id
        raise self._error()

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
    ) -> PasswordResetChallengeRecord:
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

    def get_reset_code_for_challenge(self, *, challenge_id: UUID) -> str:
        del challenge_id
        raise self._error()

    def force_expire_challenge(self, *, challenge_id: UUID) -> None:
        del challenge_id
        raise self._error()

    def _error(self) -> PasswordResetError:
        return PasswordResetError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentPasswordResetStore:
    """Persist password-reset challenges in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def issue_challenge(
        self,
        *,
        purpose: str,
        channel: Literal["email", "sms"],
        subject_normalized: str,
        user_id: UUID | None,
        idempotency_key: str,
        request_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        max_attempts: int,
    ) -> PasswordResetChallengeEnvelope:
        challenge_id = uuid4()
        reset_code = _build_reset_code(
            challenge_id=challenge_id,
            subject_normalized=subject_normalized,
            issued_at=issued_at,
        )

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> PasswordResetChallengeEnvelope:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            purpose,
                            channel,
                            subject_normalized,
                            user_id,
                            reset_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            idempotency_key,
                            request_fingerprint,
                            created_at
                        FROM auth_password_reset_challenges
                        WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        existing_record = _row_to_password_reset_record(row=existing_row)
                        if existing_record.request_fingerprint != request_fingerprint:
                            raise PasswordResetError(
                                status_code=409,
                                error_code="idempotency_key_conflict",
                                message=(
                                    "Idempotency key conflicts with existing "
                                    "password-reset request."
                                ),
                                reason="idempotency_key_reused_with_different_request",
                            )
                        return PasswordResetChallengeEnvelope(
                            status="challenge_issued",
                            challenge_id=existing_record.challenge_id,
                            expires_at=_utc_iso(existing_record.expires_at),
                        )

                    cursor.execute(
                        """
                        INSERT INTO auth_password_reset_challenges (
                            challenge_id,
                            purpose,
                            channel,
                            subject_normalized,
                            user_id,
                            reset_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            idempotency_key,
                            request_fingerprint
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 0, %s, %s, %s)
                        """,
                        (
                            challenge_id,
                            purpose,
                            channel,
                            subject_normalized,
                            user_id,
                            reset_code,
                            issued_at,
                            expires_at,
                            max_attempts,
                            idempotency_key,
                            request_fingerprint,
                        ),
                    )
            except PasswordResetError:
                raise
            except psycopg.Error as error:
                raise _password_reset_persistence_unavailable() from error
            return PasswordResetChallengeEnvelope(
                status="challenge_issued",
                challenge_id=challenge_id,
                expires_at=_utc_iso(expires_at),
            )

        def _reconcile_callback() -> PasswordResetChallengeEnvelope | None:
            try:
                with connect_auth_database(self._database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT
                                challenge_id,
                                purpose,
                                channel,
                                subject_normalized,
                                user_id,
                                reset_code,
                                issued_at,
                                expires_at,
                                consumed_at,
                                failed_attempt_count,
                                max_attempts,
                                idempotency_key,
                                request_fingerprint,
                                created_at
                            FROM auth_password_reset_challenges
                            WHERE idempotency_key = %s
                            """,
                            (idempotency_key,),
                        )
                        row = cursor.fetchone()
            except psycopg.Error:
                return None
            if row is None:
                return None
            existing_record = _row_to_password_reset_record(row=row)
            if existing_record.request_fingerprint != request_fingerprint:
                raise PasswordResetError(
                    status_code=409,
                    error_code="idempotency_key_conflict",
                    message=(
                        "Idempotency key conflicts with existing "
                        "password-reset request."
                    ),
                    reason="idempotency_key_reused_with_different_request",
                )
            return PasswordResetChallengeEnvelope(
                status="challenge_issued",
                challenge_id=existing_record.challenge_id,
                expires_at=_utc_iso(existing_record.expires_at),
            )

        try:
            return execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except PasswordResetError:
            raise
        except AuthCockroachTransactionAmbiguousCommitError as error:
            raise _password_reset_ambiguous_result() from error
        except AuthCockroachTransactionError as error:
            raise _password_reset_persistence_unavailable() from error
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error

    def get_challenge(self, *, challenge_id: UUID) -> PasswordResetChallengeRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            purpose,
                            channel,
                            subject_normalized,
                            user_id,
                            reset_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            idempotency_key,
                            request_fingerprint,
                            created_at
                        FROM auth_password_reset_challenges
                        WHERE challenge_id = %s
                        """,
                        (challenge_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error
        if row is None:
            return None
        return _row_to_password_reset_record(row=row)

    def increment_failed_attempt_count(
        self,
        *,
        challenge_id: UUID,
    ) -> PasswordResetChallengeRecord:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE auth_password_reset_challenges
                        SET failed_attempt_count = failed_attempt_count + 1
                        WHERE challenge_id = %s
                        RETURNING
                            challenge_id,
                            purpose,
                            channel,
                            subject_normalized,
                            user_id,
                            reset_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            idempotency_key,
                            request_fingerprint,
                            created_at
                        """,
                        (challenge_id,),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error
        if row is None:
            raise _password_reset_missing_state()
        return _row_to_password_reset_record(row=row)

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
                        UPDATE auth_password_reset_challenges
                        SET consumed_at = %s
                        WHERE challenge_id = %s
                          AND consumed_at IS NULL
                          AND expires_at > %s
                          AND failed_attempt_count < max_attempts
                        """,
                        (consumed_at, challenge_id, consumed_at),
                    )
                    if cursor.rowcount == 0:
                        raise _password_reset_missing_state()
                connection.commit()
        except PasswordResetError:
            raise
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error

    def get_reset_code_for_challenge(self, *, challenge_id: UUID) -> str:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT reset_code
                        FROM auth_password_reset_challenges
                        WHERE challenge_id = %s
                        """,
                        (challenge_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error
        if row is None:
            raise _password_reset_missing_state()
        return str(row[0])

    def force_expire_challenge(self, *, challenge_id: UUID) -> None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE auth_password_reset_challenges
                        SET expires_at = %s
                        WHERE challenge_id = %s
                        """,
                        (datetime.now(UTC) - timedelta(seconds=1), challenge_id),
                    )
                    if cursor.rowcount == 0:
                        raise _password_reset_missing_state()
                connection.commit()
        except PasswordResetError:
            raise
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error


_PASSWORD_RESET_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "auth_password_reset_challenges": (
        "challenge_id",
        "purpose",
        "channel",
        "subject_normalized",
        "user_id",
        "reset_code",
        "issued_at",
        "expires_at",
        "consumed_at",
        "failed_attempt_count",
        "max_attempts",
        "idempotency_key",
        "request_fingerprint",
        "created_at",
    ),
}


def _password_reset_persistence_unavailable() -> PasswordResetError:
    return PasswordResetError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _password_reset_missing_state() -> PasswordResetError:
    return PasswordResetError(
        status_code=503,
        error_code="auth_persistence_missing_state",
        message="Required auth persistence state is missing.",
        reason="auth_persistence_missing_state",
    )


def _password_reset_ambiguous_result() -> PasswordResetError:
    return PasswordResetError(
        status_code=500,
        error_code="auth_persistence_ambiguous_result",
        message="Auth persistence outcome is ambiguous.",
        reason="auth_persistence_ambiguous_result",
    )


def _row_to_password_reset_record(*, row: tuple[object, ...]) -> PasswordResetChallengeRecord:
    user_id_raw = row[4]
    return PasswordResetChallengeRecord(
        challenge_id=UUID(str(row[0])),
        purpose=str(row[1]),
        channel=cast_channel(str(row[2])),
        subject_normalized=str(row[3]),
        user_id=None if user_id_raw is None else UUID(str(user_id_raw)),
        reset_code=str(row[5]),
        issued_at=_coerce_datetime(row[6]),
        expires_at=_coerce_datetime(row[7]),
        consumed_at=_coerce_optional_datetime(row[8]),
        failed_attempt_count=_coerce_int(row[9]),
        max_attempts=_coerce_int(row[10]),
        idempotency_key=str(row[11]),
        request_fingerprint=str(row[12]),
        created_at=_coerce_datetime(row[13]),
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


def build_default_password_reset_store() -> PasswordResetStoreProtocol:
    """Build the password-reset store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryPasswordResetStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailablePasswordResetStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentPasswordResetStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailablePasswordResetStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailablePasswordResetStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_password_reset_store = build_default_password_reset_store()


def get_default_password_reset_store() -> PasswordResetStoreProtocol:
    """Return deterministic process-local password-reset store."""

    return _default_password_reset_store


def reset_default_password_reset_store() -> None:
    """Reset process-local password-reset store for isolated tests."""

    global _default_password_reset_store
    _default_password_reset_store = build_default_password_reset_store()


def parse_password_reset_initiate_request(payload: object) -> PasswordResetInitiateRequest:
    """Parse deterministic password-reset initiation payload."""
    if not isinstance(payload, dict):
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset request payload.",
            reason="invalid_password_reset_request",
        )
    try:
        request_model = PasswordResetInitiateRequest.model_validate(payload)
    except Exception as error:
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset request payload.",
            reason="invalid_password_reset_request",
        ) from error

    normalized_purpose = request_model.purpose.strip().lower()
    if normalized_purpose not in _ALLOWED_PURPOSES:
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset request payload.",
            reason="invalid_password_reset_request",
        )

    normalized_channel = request_model.channel.strip().lower()
    if normalized_channel not in _ALLOWED_CHANNELS:
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset request payload.",
            reason="invalid_password_reset_request",
        )

    if normalized_channel == "email":
        email_value = (request_model.email or "").strip().lower()
        if _EMAIL_PATTERN.fullmatch(email_value) is None:
            raise PasswordResetError(
                status_code=400,
                error_code="invalid_password_reset_request",
                message="Invalid password-reset request payload.",
                reason="invalid_password_reset_request",
            )
        return PasswordResetInitiateRequest(
            purpose=normalized_purpose,
            channel=normalized_channel,
            email=email_value,
            phone_number=None,
        )

    phone_number_normalized = _normalize_phone_number(request_model.phone_number or "")
    if _PHONE_PATTERN.fullmatch(phone_number_normalized) is None:
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset request payload.",
            reason="invalid_password_reset_request",
        )
    return PasswordResetInitiateRequest(
        purpose=normalized_purpose,
        channel=normalized_channel,
        email=None,
        phone_number=phone_number_normalized,
    )


def initiate_password_reset_challenge(
    *,
    request_model: PasswordResetInitiateRequest,
    idempotency_key: str,
    registration_store: RegistrationStoreProtocol,
    password_reset_store: PasswordResetStoreProtocol,
    email_delivery_adapter: EmailOtpDeliveryAdapterProtocol | None = None,
) -> PasswordResetChallengeEnvelope:
    """Issue deterministic non-enumerating password-reset challenge."""

    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=get_password_reset_ttl_seconds())
    if request_model.channel == "email":
        subject_normalized = request_model.email or ""
        registered_user = registration_store.get_user_by_email(email_normalized=subject_normalized)
    else:
        subject_normalized = request_model.phone_number or ""
        registered_user = registration_store.get_user_by_phone(
            phone_number_normalized=subject_normalized
        )

    request_fingerprint = (
        f"password_reset:{request_model.purpose}:{request_model.channel}:{subject_normalized}"
    )
    response = password_reset_store.issue_challenge(
        purpose=request_model.purpose,
        channel=cast_channel(request_model.channel),
        subject_normalized=subject_normalized,
        user_id=None if registered_user is None else registered_user.user_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        max_attempts=get_password_reset_max_attempts(),
    )
    if request_model.channel != "email" or registered_user is None:
        return response

    challenge_record = password_reset_store.get_challenge(challenge_id=response.challenge_id)
    if challenge_record is None:
        raise _password_reset_missing_state()

    resolved_email_delivery_adapter = (
        email_delivery_adapter or get_default_email_otp_delivery_adapter()
    )
    delivery_outcome = normalize_email_delivery_outcome(
        outcome=resolved_email_delivery_adapter.send_otp_challenge(
            message=_build_password_reset_email_message(
                purpose=request_model.purpose,
                email_normalized=subject_normalized,
                reset_code=challenge_record.reset_code,
                challenge_id=response.challenge_id,
                expires_at=challenge_record.expires_at,
            )
        )
    )
    if delivery_outcome.status != "delivered":
        raise _build_password_reset_delivery_failure_error(delivery_outcome=delivery_outcome)
    return response


def parse_password_reset_confirm_request(payload: object) -> PasswordResetConfirmRequest:
    """Parse deterministic password-reset confirmation request."""

    if not isinstance(payload, dict):
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset confirmation payload.",
            reason="invalid_password_reset_request",
        )
    try:
        request_model = PasswordResetConfirmRequest.model_validate(payload)
    except Exception as error:
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset confirmation payload.",
            reason="invalid_password_reset_request",
        ) from error

    reset_code = request_model.reset_code.strip()
    if _RESET_CODE_PATTERN.fullmatch(reset_code) is None:
        raise PasswordResetError(
            status_code=400,
            error_code="invalid_password_reset_request",
            message="Invalid password-reset confirmation payload.",
            reason="invalid_password_reset_request",
        )
    return PasswordResetConfirmRequest(
        challenge_id=request_model.challenge_id,
        reset_code=reset_code,
        new_password=request_model.new_password,
    )


def resolve_password_reset_metrics_purpose(*, reset_purpose: str) -> str:
    """Return deterministic metrics purpose tag for password-reset challenge flows."""

    _ = reset_purpose
    return PASSWORD_RESET_METRICS_PURPOSE


def confirm_password_reset_challenge(
    *,
    request_model: PasswordResetConfirmRequest,
    registration_store: RegistrationStoreProtocol,
    password_reset_store: PasswordResetStoreProtocol,
) -> PasswordResetConfirmEnvelope:
    """Verify deterministic challenge and atomically update credential + consume token."""

    challenge_record = password_reset_store.get_challenge(challenge_id=request_model.challenge_id)
    if challenge_record is None:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_token_invalid",
            message="Password reset token is invalid.",
            reason="password_reset_token_invalid",
        )
    if challenge_record.consumed_at is not None:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_token_already_used",
            message="Password reset token was already used.",
            reason="password_reset_token_already_used",
        )

    now = datetime.now(UTC)
    if now >= challenge_record.expires_at:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_token_expired",
            message="Password reset token has expired.",
            reason="password_reset_token_expired",
        )
    if challenge_record.failed_attempt_count >= challenge_record.max_attempts:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_attempt_limit_exceeded",
            message="Password reset token attempt limit is exceeded.",
            reason="password_reset_attempt_limit_exceeded",
        )

    if request_model.reset_code != challenge_record.reset_code:
        updated_record = password_reset_store.increment_failed_attempt_count(
            challenge_id=challenge_record.challenge_id
        )
        if updated_record.failed_attempt_count >= updated_record.max_attempts:
            raise PasswordResetError(
                status_code=409,
                error_code="password_reset_attempt_limit_exceeded",
                message="Password reset token attempt limit is exceeded.",
                reason="password_reset_attempt_limit_exceeded",
            )
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_token_invalid",
            message="Password reset token is invalid.",
            reason="password_reset_token_invalid",
        )

    if challenge_record.user_id is None:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_token_invalid",
            message="Password reset token is invalid.",
            reason="password_reset_token_invalid",
        )

    registered_user = registration_store.get_user_by_id(user_id=challenge_record.user_id)
    if registered_user is None:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_token_invalid",
            message="Password reset token is invalid.",
            reason="password_reset_token_invalid",
        )
    if registered_user.account_state not in {"pending_verification", "active"}:
        raise PasswordResetError(
            status_code=409,
            error_code="password_reset_not_allowed_for_state",
            message="Password reset is not allowed for current account state.",
            reason="password_reset_not_allowed_for_state",
            details={
                "current_state": registered_user.account_state,
                "requested_state": registered_user.account_state,
            },
        )

    if (
        isinstance(password_reset_store, PersistentPasswordResetStore)
        and isinstance(registration_store, PersistentRegistrationStore)
    ):
        return _confirm_password_reset_challenge_persistent(
            request_model=request_model,
            challenge_record=challenge_record,
            registration_store=registration_store,
            password_reset_store=password_reset_store,
        )

    return _confirm_password_reset_challenge_in_memory(
        request_model=request_model,
        challenge_record=challenge_record,
        registration_store=registration_store,
        password_reset_store=password_reset_store,
        now=now,
    )


def _confirm_password_reset_challenge_in_memory(
    *,
    request_model: PasswordResetConfirmRequest,
    challenge_record: PasswordResetChallengeRecord,
    registration_store: RegistrationStoreProtocol,
    password_reset_store: PasswordResetStoreProtocol,
    now: datetime,
) -> PasswordResetConfirmEnvelope:
    try:
        validate_password_policy(request_model.new_password)
    except RegistrationValidationError as error:
        raise PasswordResetError(
            status_code=409,
            error_code="password_policy_violation",
            message=error.message,
            reason="password_policy_violation",
        ) from error

    history_depth = get_auth_password_history_depth()
    if registration_store.is_password_reused(
        user_id=challenge_record.user_id,
        password=request_model.new_password,
        history_depth=history_depth,
    ):
        raise PasswordResetError(
            status_code=409,
            error_code="password_reuse_not_allowed",
            message="Password reuse is not allowed by current password-history policy.",
            reason="password_reuse_not_allowed",
            details={"history_depth": history_depth},
        )

    password_hash = build_password_hash(password=request_model.new_password)
    registration_store.update_user_password_hash(
        user_id=challenge_record.user_id,
        password_hash=password_hash,
    )
    password_reset_store.mark_challenge_consumed(
        challenge_id=challenge_record.challenge_id,
        consumed_at=now,
    )
    return PasswordResetConfirmEnvelope(
        status="password_updated",
        updated_at=_utc_iso(now),
    )


def _confirm_password_reset_challenge_persistent(
    *,
    request_model: PasswordResetConfirmRequest,
    challenge_record: PasswordResetChallengeRecord,
    registration_store: RegistrationStoreProtocol,
    password_reset_store: PersistentPasswordResetStore,
) -> PasswordResetConfirmEnvelope:
    try:
        validate_password_policy(request_model.new_password)
    except RegistrationValidationError as error:
        raise PasswordResetError(
            status_code=409,
            error_code="password_policy_violation",
            message=error.message,
            reason="password_policy_violation",
        ) from error

    history_depth = get_auth_password_history_depth()
    if registration_store.is_password_reused(
        user_id=challenge_record.user_id,
        password=request_model.new_password,
        history_depth=history_depth,
    ):
        raise PasswordResetError(
            status_code=409,
            error_code="password_reuse_not_allowed",
            message="Password reuse is not allowed by current password-history policy.",
            reason="password_reuse_not_allowed",
            details={"history_depth": history_depth},
        )

    prepared_password_hash = build_password_hash(password=request_model.new_password)
    transaction_now = datetime.now(UTC)

    def _transaction_callback(
        connection: psycopg.Connection[object],
    ) -> PasswordResetConfirmEnvelope | PasswordResetError:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        challenge_id,
                        purpose,
                        channel,
                        subject_normalized,
                        user_id,
                        reset_code,
                        issued_at,
                        expires_at,
                        consumed_at,
                        failed_attempt_count,
                        max_attempts,
                        idempotency_key,
                        request_fingerprint,
                        created_at
                    FROM auth_password_reset_challenges
                    WHERE challenge_id = %s
                    FOR UPDATE
                    """,
                    (challenge_record.challenge_id,),
                )
                challenge_row = cursor.fetchone()
                if challenge_row is None:
                    raise _password_reset_invalid_token_error()

                locked_challenge = _row_to_password_reset_record(row=challenge_row)
                if locked_challenge.consumed_at is not None:
                    raise _password_reset_already_used_error()
                if transaction_now >= locked_challenge.expires_at:
                    raise _password_reset_expired_error()
                if locked_challenge.failed_attempt_count >= locked_challenge.max_attempts:
                    raise _password_reset_attempt_limit_error()
                if locked_challenge.user_id is None:
                    raise _password_reset_invalid_token_error()

                cursor.execute(
                    """
                    SELECT
                        id,
                        account_state,
                        password_hash,
                        password_history_hashes,
                        credentials_invalidated_at
                    FROM users
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (locked_challenge.user_id,),
                )
                user_row = cursor.fetchone()
                if user_row is None:
                    raise _password_reset_invalid_token_error()

                account_state = str(user_row[1])
                if account_state not in {"pending_verification", "active"}:
                    raise PasswordResetError(
                        status_code=409,
                        error_code="password_reset_not_allowed_for_state",
                        message="Password reset is not allowed for current account state.",
                        reason="password_reset_not_allowed_for_state",
                        details={
                            "current_state": account_state,
                            "requested_state": account_state,
                        },
                    )

                current_history = _coerce_password_history(user_row[3])
                if _is_password_reused_against_history(
                    password=request_model.new_password,
                    password_history_hashes=current_history,
                    history_depth=history_depth,
                ):
                    raise PasswordResetError(
                        status_code=409,
                        error_code="password_reuse_not_allowed",
                        message=(
                            "Password reuse is not allowed by current "
                            "password-history policy."
                        ),
                        reason="password_reuse_not_allowed",
                        details={"history_depth": history_depth},
                    )

                if request_model.reset_code != locked_challenge.reset_code:
                    updated_failed_attempt_count = _increment_failed_attempt_count_locked(
                        cursor=cursor,
                        challenge_id=locked_challenge.challenge_id,
                    )
                    if updated_failed_attempt_count >= locked_challenge.max_attempts:
                        return _password_reset_attempt_limit_error()
                    return _password_reset_invalid_token_error()

                updated_password_history = _build_password_history_state(
                    password_hash=prepared_password_hash,
                    current_history=current_history,
                    history_depth=history_depth,
                )
                cursor.execute(
                    """
                    UPDATE auth_password_reset_challenges
                    SET consumed_at = %s
                    WHERE challenge_id = %s
                      AND consumed_at IS NULL
                      AND expires_at > %s
                      AND failed_attempt_count < max_attempts
                    RETURNING challenge_id
                    """,
                    (
                        transaction_now,
                        locked_challenge.challenge_id,
                        transaction_now,
                    ),
                )
                consumed_row = cursor.fetchone()
                if consumed_row is None:
                    return _password_reset_outcome_from_locked_challenge(
                        challenge=locked_challenge,
                        now=transaction_now,
                    )

                cursor.execute(
                    """
                    UPDATE users
                    SET password_hash = %s,
                        password_history_hashes = %s::jsonb,
                        credentials_invalidated_at = NULL,
                        updated_at = now()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        prepared_password_hash,
                        json.dumps(list(updated_password_history)),
                        locked_challenge.user_id,
                    ),
                )
                updated_user_row = cursor.fetchone()
                if updated_user_row is None:
                    raise _password_reset_missing_state()
        except PasswordResetError as error:
            return error
        except psycopg.Error as error:
            raise _password_reset_persistence_unavailable() from error
        return PasswordResetConfirmEnvelope(
            status="password_updated",
            updated_at=_utc_iso(transaction_now),
        )

    def _reconcile_callback() -> PasswordResetConfirmEnvelope | PasswordResetError | None:
        try:
            with connect_auth_database(password_reset_store._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            purpose,
                            channel,
                            subject_normalized,
                            user_id,
                            reset_code,
                            issued_at,
                            expires_at,
                            consumed_at,
                            failed_attempt_count,
                            max_attempts,
                            idempotency_key,
                            request_fingerprint,
                            created_at
                        FROM auth_password_reset_challenges
                        WHERE challenge_id = %s
                        """,
                        (challenge_record.challenge_id,),
                    )
                    challenge_row = cursor.fetchone()
                    if challenge_row is None:
                        return None
                    reconciled_challenge = _row_to_password_reset_record(row=challenge_row)
                    cursor.execute(
                        """
                        SELECT
                            id,
                            account_state,
                            password_hash,
                            password_history_hashes,
                            credentials_invalidated_at
                        FROM users
                        WHERE id = %s
                        """,
                        (reconciled_challenge.user_id,),
                    )
                    user_row = cursor.fetchone()
        except psycopg.Error:
            return None

        if user_row is None or reconciled_challenge.user_id is None:
            return None

        if request_model.reset_code != challenge_record.reset_code:
            expected_failed_attempt_count = challenge_record.failed_attempt_count + 1
            if (
                reconciled_challenge.failed_attempt_count
                != expected_failed_attempt_count
            ):
                return None
            if reconciled_challenge.failed_attempt_count >= reconciled_challenge.max_attempts:
                return _password_reset_attempt_limit_error()
            return _password_reset_invalid_token_error()

        current_password_hash = str(user_row[2])
        current_history = _coerce_password_history(user_row[3])
        if current_password_hash != prepared_password_hash:
            return None
        if not current_history or current_history[0] != prepared_password_hash:
            return None
        if reconciled_challenge.consumed_at is None:
            return None
        return PasswordResetConfirmEnvelope(
            status="password_updated",
            updated_at=_utc_iso(transaction_now),
        )

    try:
        result = execute_auth_database_transaction(
            database_url=password_reset_store._database_url,
            transaction_callback=_transaction_callback,
            reconcile_callback=_reconcile_callback,
        )
    except AuthCockroachTransactionAmbiguousCommitError as error:
        raise _password_reset_ambiguous_result() from error
    except AuthCockroachTransactionError as error:
        raise _password_reset_persistence_unavailable() from error

    if isinstance(result, PasswordResetError):
        raise result
    return result


def _increment_failed_attempt_count_locked(
    *,
    cursor: psycopg.Cursor[tuple[object, ...]],
    challenge_id: UUID,
) -> int:
    cursor.execute(
        """
        UPDATE auth_password_reset_challenges
        SET failed_attempt_count = failed_attempt_count + 1
        WHERE challenge_id = %s
        RETURNING failed_attempt_count
        """,
        (challenge_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise _password_reset_missing_state()
    return _coerce_int(row[0])


def _password_reset_invalid_token_error() -> PasswordResetError:
    return PasswordResetError(
        status_code=409,
        error_code="password_reset_token_invalid",
        message="Password reset token is invalid.",
        reason="password_reset_token_invalid",
    )


def _password_reset_already_used_error() -> PasswordResetError:
    return PasswordResetError(
        status_code=409,
        error_code="password_reset_token_already_used",
        message="Password reset token was already used.",
        reason="password_reset_token_already_used",
    )


def _password_reset_expired_error() -> PasswordResetError:
    return PasswordResetError(
        status_code=409,
        error_code="password_reset_token_expired",
        message="Password reset token has expired.",
        reason="password_reset_token_expired",
    )


def _password_reset_attempt_limit_error() -> PasswordResetError:
    return PasswordResetError(
        status_code=409,
        error_code="password_reset_attempt_limit_exceeded",
        message="Password reset token attempt limit is exceeded.",
        reason="password_reset_attempt_limit_exceeded",
    )


def _password_reset_outcome_from_locked_challenge(
    *,
    challenge: PasswordResetChallengeRecord,
    now: datetime,
) -> PasswordResetError:
    if challenge.consumed_at is not None:
        return _password_reset_already_used_error()
    if now >= challenge.expires_at:
        return _password_reset_expired_error()
    if challenge.failed_attempt_count >= challenge.max_attempts:
        return _password_reset_attempt_limit_error()
    return _password_reset_invalid_token_error()


def _coerce_password_history(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if not isinstance(value, list):
        if isinstance(value, tuple):
            value = list(value)
        else:
            return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _build_password_history_state(
    *,
    password_hash: str,
    current_history: tuple[str, ...],
    history_depth: int,
) -> tuple[str, ...]:
    history = tuple(
        value for value in (password_hash, *current_history) if value
    )
    if history_depth <= 0:
        return ()
    return history[:history_depth]


def _is_password_reused_against_history(
    *,
    password: str,
    password_history_hashes: tuple[str, ...],
    history_depth: int,
) -> bool:
    if history_depth <= 0:
        return False
    for history_hash in password_history_hashes[:history_depth]:
        if verify_password_against_hash(password=password, password_hash=history_hash):
            return True
    return False


def cast_channel(value: str) -> Literal["email", "sms"]:
    normalized = value.strip().lower()
    if normalized == "email":
        return "email"
    return "sms"


def _normalize_phone_number(phone_number: str) -> str:
    cleaned = _PHONE_CLEAN_PATTERN.sub("", phone_number.strip())
    if cleaned.count("+") > 1:
        return cleaned
    if "+" in cleaned and not cleaned.startswith("+"):
        return cleaned
    return cleaned


def _build_reset_code(
    *,
    challenge_id: UUID,
    subject_normalized: str,
    issued_at: datetime,
) -> str:
    digest = sha256(
        f"{challenge_id}:{subject_normalized}:{issued_at.isoformat()}".encode()
    ).digest()
    numeric = int.from_bytes(digest[:4], byteorder="big") % 1_000_000
    return f"{numeric:06d}"


def _build_password_reset_email_message(
    *,
    purpose: str,
    email_normalized: str,
    reset_code: str,
    challenge_id: UUID,
    expires_at: datetime,
) -> EmailOtpMessage:
    normalized_purpose = purpose.strip().lower()
    subject = (
        "Complete your KODI password setup"
        if normalized_purpose == "password_setup"
        else "Your KODI password reset code"
    )
    action_label = (
        "finish setting your password"
        if normalized_purpose == "password_setup"
        else "continue your password reset"
    )
    content = (
        "<html><body>"
        f"<p>Use this KODI one-time code to {action_label}.</p>"
        f"<p><strong>{escape(reset_code)}</strong></p>"
        f"<p>This code expires at {escape(_utc_iso(expires_at))}.</p>"
        f"<p>Challenge reference: {escape(str(challenge_id))}</p>"
        "</body></html>"
    )
    return EmailOtpMessage(
        purpose="recovery",
        email_normalized=email_normalized,
        subject=subject,
        content=content,
        challenge_id=str(challenge_id),
    )


def _build_password_reset_delivery_failure_error(
    *,
    delivery_outcome: OtpDeliveryOutcome,
) -> PasswordResetError:
    reason_code = delivery_outcome.reason_code
    status = delivery_outcome.status
    provider_ref = delivery_outcome.provider_ref
    details: dict[str, object] = {"delivery_failure_class": status}
    if isinstance(provider_ref, str) and provider_ref:
        details["provider_ref"] = provider_ref

    if reason_code == "otp_delivery_provider_misconfigured":
        return PasswordResetError(
            status_code=409,
            error_code="otp_delivery_provider_misconfigured",
            message="OTP delivery provider is misconfigured.",
            reason="otp_delivery_provider_misconfigured",
            details=details,
        )
    if reason_code == "email_delivery_provider_timeout":
        details["retry_after_seconds"] = 60
        return PasswordResetError(
            status_code=409,
            error_code="otp_email_delivery_provider_timeout",
            message="Password-reset email delivery timed out.",
            reason="otp_email_delivery_provider_timeout",
            details=details,
        )
    if reason_code == "email_delivery_provider_unavailable":
        return PasswordResetError(
            status_code=409,
            error_code="otp_email_delivery_provider_unavailable",
            message="Password-reset email delivery provider is unavailable.",
            reason="otp_email_delivery_provider_unavailable",
            details=details,
        )
    if reason_code == "email_delivery_provider_rejected":
        return PasswordResetError(
            status_code=409,
            error_code="otp_email_delivery_provider_rejected",
            message="Password-reset email delivery was rejected.",
            reason="otp_email_delivery_provider_rejected",
            details=details,
        )
    return PasswordResetError(
        status_code=409,
        error_code="otp_email_delivery_failed",
        message="Password-reset email delivery failed.",
        reason="otp_email_delivery_failed",
        details=details,
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
