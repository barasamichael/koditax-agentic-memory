"""Implement deterministic credential login with mandatory OTP step-up gating."""

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
from dataclasses import dataclass
from collections.abc import Callable

import psycopg
from pydantic import BaseModel

from services.auth.app.config import get_auth_default_tenant_id
from services.auth.app.config import get_auth_otp_channel_policy_for_purpose
from services.auth.app.config import get_auth_otp_policy_for_purpose
from services.auth.app.config import get_auth_login_lockout_window_seconds
from services.auth.app.config import get_auth_login_lockout_max_failed_attempts
from services.auth.app.config import get_auth_login_lockout_attempt_window_seconds
from services.auth.app.registration import build_password_hash
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import is_legacy_password_hash
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import is_supported_password_hash
from services.auth.app.registration import verify_password_against_hash
from services.auth.app.session_issuance import SessionIssuanceError
from services.auth.app.session_issuance import SessionIssuanceStoreProtocol
from services.auth.app.email_verification import EmailVerificationStoreProtocol
from services.auth.app.email_verification import EmailVerificationChallengeRecord
from services.auth.app.phone_verification import PhoneVerificationStoreProtocol
from services.auth.app.phone_verification import PhoneVerificationChallengeRecord
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import AuthCockroachTransactionError
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import validate_auth_database_connection
from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome
from services.auth.app.otp_delivery_adapters import SmsOtpDeliveryAdapterProtocol
from services.auth.app.otp_delivery_adapters import normalize_sms_delivery_outcome
from services.auth.app.otp_delivery_adapters import EmailOtpDeliveryAdapterProtocol
from services.auth.app.otp_delivery_adapters import normalize_email_delivery_outcome
from services.auth.app.otp_delivery_adapters import get_default_sms_otp_delivery_adapter
from services.auth.app.otp_delivery_adapters import get_default_email_otp_delivery_adapter

_PHONE_CLEAN_PATTERN = re.compile(r"[\s\-\(\)]")
_KENYA_NATIONAL_PHONE_PATTERN = re.compile(r"^[17]\d{8}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_PATTERN = re.compile(r"^\d{4,12}$")
_DUMMY_PASSWORD_HASH = build_password_hash(
    password="kodi_auth_login_dummy_password"
)
AUTH_LOG_EVENT_LOGIN = "auth.login"
AUTH_LOG_EVENT_EMAIL_OTP_LOGIN = "auth.email_otp_login"


class LoginRequest(BaseModel):
    """Represent deterministic credential login request payload."""

    login_id: str
    password: str
    device_fingerprint: str | None = None
    step_up_challenge_id: UUID | None = None
    step_up_otp_code: str | None = None


class DelegationContextEnvelope(BaseModel):
    """Represent canonical non-delegated auth context for login baseline."""

    is_delegated: bool
    principal_user_id: str | None = None
    delegate_user_id: str | None = None
    delegation_id: str | None = None
    granted_at: str | None = None
    revoked_at: str | None = None


class SessionContextEnvelope(BaseModel):
    """Represent canonical auth session context in login response."""

    user_id: UUID
    tenant_id: str
    role: str
    session_id: UUID
    delegation_context: DelegationContextEnvelope


class LoginSuccessEnvelope(BaseModel):
    """Represent deterministic successful login response."""

    status: Literal["authenticated"]
    access_token: str
    refresh_token: str
    expires_at: str
    session: SessionContextEnvelope


class LoginStepUpPendingEnvelope(BaseModel):
    """Represent deterministic pending step-up login response."""

    login_status: Literal["pending_step_up"]
    status: Literal["pending_step_up"]
    step_up_required: Literal[True]
    step_up_purpose: Literal["login_step_up"]
    step_up_channel: Literal["email", "sms"]
    step_up_challenge_id: UUID
    step_up_expires_at: str


LoginResponseEnvelope = LoginSuccessEnvelope | LoginStepUpPendingEnvelope


class EmailOtpLoginRequest(BaseModel):
    """Represent passwordless email-OTP login request payload."""

    email: str
    device_fingerprint: str | None = None
    step_up_challenge_id: UUID | None = None
    step_up_otp_code: str | None = None


class EmailOtpLoginStepUpPendingEnvelope(BaseModel):
    """Represent pending step-up response for email-OTP login."""

    login_status: Literal["pending_step_up"]
    status: Literal["pending_step_up"]
    step_up_required: Literal[True]
    step_up_purpose: Literal["login_step_up"]
    step_up_channel: Literal["email"]
    step_up_challenge_id: UUID
    step_up_expires_at: str


EmailOtpLoginResponseEnvelope = LoginSuccessEnvelope | EmailOtpLoginStepUpPendingEnvelope


@dataclass(frozen=True)
class EmailOtpLoginRequestRecord:
    """Represent normalized email-OTP login request context."""

    email_normalized: str
    device_fingerprint: str | None
    step_up_challenge_id: UUID | None
    step_up_otp_code: str | None


@dataclass(frozen=True)
class LoginRequestRecord:
    """Represent normalized login request context."""

    login_id_normalized: str
    password: str
    device_fingerprint: str | None
    step_up_challenge_id: UUID | None
    step_up_otp_code: str | None


@dataclass(frozen=True)
class LoginLockoutState:
    """Represent one active login lockout state for deterministic responses."""

    lockout_expires_at: str
    lockout_remaining_seconds: int


@dataclass(frozen=True)
class LoginStepUpState:
    """Represent pending step-up challenge context for one login source."""

    challenge_id: UUID
    challenge_channel: Literal["email", "sms"]
    challenge_expires_at: str
    user_id: UUID | None = None
    issued_at: str | None = None
    consumed_at: str | None = None


@dataclass
class _LoginFailureState:
    """Track deterministic failed-login and lockout state by login-id and IP."""

    failed_attempt_count: int = 0
    last_failed_attempt_at: datetime | None = None
    lockout_expires_at: datetime | None = None


@dataclass(frozen=True)
class _LoginLockoutMutationResult:
    """Represent one durable lockout mutation result for reconciliation."""

    failed_attempt_count: int
    last_failed_attempt_at: datetime | None
    lockout_expires_at: datetime | None
    active_lockout: LoginLockoutState | None


class LoginError(ValueError):
    """Represent deterministic login failures."""

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


class LoginLockoutStoreProtocol(Protocol):
    """Define persistence boundary for deterministic login lockout tracking."""

    def get_active_lockout(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        """Return active lockout state when present and unexpired."""

        ...

    def register_failed_attempt(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        """Register failed attempt and return lockout state when threshold is reached."""

        ...

    def clear_failed_attempts(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        """Clear failed-attempt and lockout state after successful login."""

        ...


class LoginStepUpStoreProtocol(Protocol):
    """Define persistence boundary for deterministic login step-up state."""

    def get_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginStepUpState | None:
        """Return pending step-up state for one login-id/source-ip context."""

        ...

    def set_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        step_up_state: LoginStepUpState,
    ) -> None:
        """Persist pending step-up state for one login-id/source-ip context."""

        ...

    def clear_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        """Clear pending step-up state for one login-id/source-ip context."""

        ...

    def mark_step_up_state_consumed(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        consumed_at: str,
    ) -> None:
        """Persist consumed step-up state to reject replay deterministically."""

        ...


class InMemoryLoginLockoutStore:
    """Track failed-login counters and lockouts in-memory deterministically."""

    def __init__(
        self,
        *,
        max_failed_attempts: int | None = None,
        failed_attempt_window_seconds: int | None = None,
        lockout_window_seconds: int | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        resolved_max_failed_attempts = (
            max_failed_attempts
            if max_failed_attempts is not None and max_failed_attempts > 0
            else get_auth_login_lockout_max_failed_attempts()
        )
        resolved_failed_attempt_window_seconds = (
            failed_attempt_window_seconds
            if failed_attempt_window_seconds is not None
            and failed_attempt_window_seconds > 0
            else get_auth_login_lockout_attempt_window_seconds()
        )
        resolved_lockout_window_seconds = (
            lockout_window_seconds
            if lockout_window_seconds is not None and lockout_window_seconds > 0
            else get_auth_login_lockout_window_seconds()
        )
        self._max_failed_attempts = resolved_max_failed_attempts
        self._failed_attempt_window_seconds = (
            resolved_failed_attempt_window_seconds
        )
        self._lockout_window_seconds = resolved_lockout_window_seconds
        self._now_provider = now_provider or _utc_now
        self._lock = Lock()
        self._state_by_key: dict[tuple[str, str], _LoginFailureState] = {}

    def get_active_lockout(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        lockout_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            state = self._state_by_key.get(lockout_key)
            if state is None:
                return None
            return self._active_lockout_for_state(
                lockout_key=lockout_key, state=state
            )

    def register_failed_attempt(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        lockout_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            state = self._state_by_key.get(lockout_key)
            if state is None:
                state = _LoginFailureState()
                self._state_by_key[lockout_key] = state

            active_lockout = self._active_lockout_for_state(
                lockout_key=lockout_key, state=state
            )
            if active_lockout is not None:
                return active_lockout

            now = self._now_provider()
            if self._failed_attempt_window_elapsed(state=state, now=now):
                state.failed_attempt_count = 0

            state.failed_attempt_count += 1
            state.last_failed_attempt_at = now
            if state.failed_attempt_count < self._max_failed_attempts:
                return None

            lockout_expires_at = now + timedelta(
                seconds=self._lockout_window_seconds
            )
            state.failed_attempt_count = 0
            state.last_failed_attempt_at = None
            state.lockout_expires_at = lockout_expires_at
            return LoginLockoutState(
                lockout_expires_at=_utc_iso(lockout_expires_at),
                lockout_remaining_seconds=self._lockout_window_seconds,
            )

    def clear_failed_attempts(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        lockout_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            self._state_by_key.pop(lockout_key, None)

    def _active_lockout_for_state(
        self,
        *,
        lockout_key: tuple[str, str],
        state: _LoginFailureState,
    ) -> LoginLockoutState | None:
        expires_at = state.lockout_expires_at
        if expires_at is None:
            return None

        now = self._now_provider()
        if expires_at <= now:
            state.failed_attempt_count = 0
            state.last_failed_attempt_at = None
            state.lockout_expires_at = None
            if state.failed_attempt_count == 0:
                self._state_by_key.pop(lockout_key, None)
            return None

        remaining_seconds = max(1, int((expires_at - now).total_seconds()))
        return LoginLockoutState(
            lockout_expires_at=_utc_iso(expires_at),
            lockout_remaining_seconds=remaining_seconds,
        )

    def _failed_attempt_window_elapsed(
        self,
        *,
        state: _LoginFailureState,
        now: datetime,
    ) -> bool:
        last_failed_attempt_at = state.last_failed_attempt_at
        if last_failed_attempt_at is None:
            return False
        elapsed_seconds = int((now - last_failed_attempt_at).total_seconds())
        return elapsed_seconds >= self._failed_attempt_window_seconds

    @staticmethod
    def _build_key(
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> tuple[str, str]:
        return (
            login_id_normalized,
            _normalize_source_ip(source_ip),
        )


class UnavailableLoginLockoutStore:
    """Fail closed when production login-lockout persistence is unavailable."""

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

    def get_active_lockout(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        del login_id_normalized, source_ip
        raise self._error()

    def register_failed_attempt(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        del login_id_normalized, source_ip
        raise self._error()

    def clear_failed_attempts(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        del login_id_normalized, source_ip
        raise self._error()

    def _error(self) -> LoginError:
        return LoginError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentLoginLockoutStore:
    """Persist deterministic failed-login and lockout state in PostgreSQL."""

    def __init__(
        self,
        *,
        database_url: str,
        max_failed_attempts: int | None = None,
        failed_attempt_window_seconds: int | None = None,
        lockout_window_seconds: int | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_url = database_url
        self._max_failed_attempts = (
            max_failed_attempts
            if max_failed_attempts is not None and max_failed_attempts > 0
            else get_auth_login_lockout_max_failed_attempts()
        )
        self._failed_attempt_window_seconds = (
            failed_attempt_window_seconds
            if failed_attempt_window_seconds is not None
            and failed_attempt_window_seconds > 0
            else get_auth_login_lockout_attempt_window_seconds()
        )
        self._lockout_window_seconds = (
            lockout_window_seconds
            if lockout_window_seconds is not None and lockout_window_seconds > 0
            else get_auth_login_lockout_window_seconds()
        )
        self._now_provider = now_provider or _utc_now

    def get_active_lockout(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        state = self._load_state(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        if state is None:
            return None
        normalized_source_ip = _normalize_source_ip(source_ip)
        now = self._now_provider()
        expires_at = state.lockout_expires_at
        if expires_at is None:
            return None
        if expires_at <= now:
            self._delete_state(
                login_id_normalized=login_id_normalized,
                source_ip=normalized_source_ip,
            )
            return None
        remaining_seconds = max(1, int((expires_at - now).total_seconds()))
        return LoginLockoutState(
            lockout_expires_at=_utc_iso(expires_at),
            lockout_remaining_seconds=remaining_seconds,
        )

    def register_failed_attempt(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginLockoutState | None:
        normalized_source_ip = _normalize_source_ip(source_ip)
        now = self._now_provider()
        expected_result: _LoginLockoutMutationResult | None = None

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> _LoginLockoutMutationResult:
            nonlocal expected_result
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT failed_attempt_count, last_failed_attempt_at, lockout_expires_at
                    FROM auth_login_lockouts
                    WHERE login_id_normalized = %s
                      AND source_ip = %s
                    FOR UPDATE
                    """,
                    (login_id_normalized, normalized_source_ip),
                )
                row = cursor.fetchone()
                state = (
                    _LoginFailureState(
                        failed_attempt_count=int(row[0]),
                        last_failed_attempt_at=(
                            row[1] if isinstance(row[1], datetime) else None
                        ),
                        lockout_expires_at=(
                            row[2] if isinstance(row[2], datetime) else None
                        ),
                    )
                    if row is not None
                    else _LoginFailureState()
                )

                active_lockout = self._active_lockout_for_state(
                    state=state, now=now
                )
                if active_lockout is not None:
                    expected_result = _LoginLockoutMutationResult(
                        failed_attempt_count=state.failed_attempt_count,
                        last_failed_attempt_at=state.last_failed_attempt_at,
                        lockout_expires_at=state.lockout_expires_at,
                        active_lockout=active_lockout,
                    )
                    return expected_result

                if self._failed_attempt_window_elapsed(state=state, now=now):
                    state.failed_attempt_count = 0
                state.failed_attempt_count += 1
                state.last_failed_attempt_at = now
                if state.failed_attempt_count < self._max_failed_attempts:
                    state.lockout_expires_at = None
                    self._upsert_state(
                        cursor=cursor,
                        login_id_normalized=login_id_normalized,
                        source_ip=normalized_source_ip,
                        state=state,
                        updated_at=now,
                    )
                    expected_result = _LoginLockoutMutationResult(
                        failed_attempt_count=state.failed_attempt_count,
                        last_failed_attempt_at=state.last_failed_attempt_at,
                        lockout_expires_at=state.lockout_expires_at,
                        active_lockout=None,
                    )
                    return expected_result

                lockout_expires_at = now + timedelta(
                    seconds=self._lockout_window_seconds
                )
                state.failed_attempt_count = 0
                state.last_failed_attempt_at = None
                state.lockout_expires_at = lockout_expires_at
                self._upsert_state(
                    cursor=cursor,
                    login_id_normalized=login_id_normalized,
                    source_ip=normalized_source_ip,
                    state=state,
                    updated_at=now,
                )
                expected_result = _LoginLockoutMutationResult(
                    failed_attempt_count=state.failed_attempt_count,
                    last_failed_attempt_at=state.last_failed_attempt_at,
                    lockout_expires_at=state.lockout_expires_at,
                    active_lockout=LoginLockoutState(
                        lockout_expires_at=_utc_iso(lockout_expires_at),
                        lockout_remaining_seconds=self._lockout_window_seconds,
                    ),
                )
                return expected_result

        def _reconcile_callback() -> _LoginLockoutMutationResult | None:
            if expected_result is None:
                return None
            current_state = self._load_state(
                login_id_normalized=login_id_normalized,
                source_ip=normalized_source_ip,
            )
            if current_state is None:
                return None
            if (
                current_state.failed_attempt_count
                != expected_result.failed_attempt_count
                or current_state.last_failed_attempt_at
                != expected_result.last_failed_attempt_at
                or current_state.lockout_expires_at != expected_result.lockout_expires_at
            ):
                return None
            return expected_result

        try:
            result = execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AuthCockroachTransactionError as error:
            raise _login_lockout_persistence_unavailable() from error
        return result.active_lockout

    def clear_failed_attempts(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        normalized_source_ip = _normalize_source_ip(source_ip)

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> bool:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM auth_login_lockouts
                    WHERE login_id_normalized = %s
                      AND source_ip = %s
                    """,
                    (login_id_normalized, normalized_source_ip),
                )
            return True

        def _reconcile_callback() -> bool | None:
            current_state = self._load_state(
                login_id_normalized=login_id_normalized,
                source_ip=normalized_source_ip,
            )
            if current_state is not None:
                return None
            return True

        try:
            execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AuthCockroachTransactionError as error:
            raise _login_lockout_persistence_unavailable() from error

    def _load_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> _LoginFailureState | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT failed_attempt_count, last_failed_attempt_at, lockout_expires_at
                        FROM auth_login_lockouts
                        WHERE login_id_normalized = %s
                          AND source_ip = %s
                        """,
                        (login_id_normalized, _normalize_source_ip(source_ip)),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _login_lockout_persistence_unavailable() from error
        if row is None:
            return None
        return _LoginFailureState(
            failed_attempt_count=int(row[0]),
            last_failed_attempt_at=(
                row[1] if isinstance(row[1], datetime) else None
            ),
            lockout_expires_at=row[2] if isinstance(row[2], datetime) else None,
        )

    def _delete_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM auth_login_lockouts
                        WHERE login_id_normalized = %s
                          AND source_ip = %s
                        """,
                        (login_id_normalized, source_ip),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise _login_lockout_persistence_unavailable() from error

    def _upsert_state(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        login_id_normalized: str,
        source_ip: str,
        state: _LoginFailureState,
        updated_at: datetime,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO auth_login_lockouts (
                login_id_normalized,
                source_ip,
                failed_attempt_count,
                last_failed_attempt_at,
                lockout_expires_at,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (login_id_normalized, source_ip)
            DO UPDATE SET
                failed_attempt_count = EXCLUDED.failed_attempt_count,
                last_failed_attempt_at = EXCLUDED.last_failed_attempt_at,
                lockout_expires_at = EXCLUDED.lockout_expires_at,
                updated_at = EXCLUDED.updated_at
            """,
            (
                login_id_normalized,
                source_ip,
                state.failed_attempt_count,
                state.last_failed_attempt_at,
                state.lockout_expires_at,
                updated_at,
            ),
        )

    def _active_lockout_for_state(
        self,
        *,
        state: _LoginFailureState,
        now: datetime,
    ) -> LoginLockoutState | None:
        expires_at = state.lockout_expires_at
        if expires_at is None or expires_at <= now:
            return None
        remaining_seconds = max(1, int((expires_at - now).total_seconds()))
        return LoginLockoutState(
            lockout_expires_at=_utc_iso(expires_at),
            lockout_remaining_seconds=remaining_seconds,
        )

    def _failed_attempt_window_elapsed(
        self,
        *,
        state: _LoginFailureState,
        now: datetime,
    ) -> bool:
        last_failed_attempt_at = state.last_failed_attempt_at
        if last_failed_attempt_at is None:
            return False
        elapsed_seconds = int((now - last_failed_attempt_at).total_seconds())
        return elapsed_seconds >= self._failed_attempt_window_seconds


class InMemoryLoginStepUpStore:
    """Persist pending login step-up challenge state in memory."""

    def __init__(self) -> None:
        self._state_by_key: dict[tuple[str, str], LoginStepUpState] = {}
        self._lock = Lock()

    def get_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginStepUpState | None:
        state_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            return self._state_by_key.get(state_key)

    def set_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        step_up_state: LoginStepUpState,
    ) -> None:
        state_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            self._state_by_key[state_key] = step_up_state

    def clear_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        state_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            self._state_by_key.pop(state_key, None)

    def mark_step_up_state_consumed(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        consumed_at: str,
    ) -> None:
        state_key = self._build_key(
            login_id_normalized=login_id_normalized,
            source_ip=source_ip,
        )
        with self._lock:
            existing_state = self._state_by_key.get(state_key)
            if existing_state is None:
                return
            self._state_by_key[state_key] = LoginStepUpState(
                challenge_id=existing_state.challenge_id,
                challenge_channel=existing_state.challenge_channel,
                challenge_expires_at=existing_state.challenge_expires_at,
                user_id=existing_state.user_id,
                issued_at=existing_state.issued_at,
                consumed_at=consumed_at,
            )

    @staticmethod
    def _build_key(
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> tuple[str, str]:
        return (
            login_id_normalized,
            _normalize_source_ip(source_ip),
        )


class UnavailableLoginStepUpStore:
    """Fail closed when production login step-up persistence is unavailable."""

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

    def get_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginStepUpState | None:
        del login_id_normalized, source_ip
        raise self._error()

    def set_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        step_up_state: LoginStepUpState,
    ) -> None:
        del login_id_normalized, source_ip, step_up_state
        raise self._error()

    def clear_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        del login_id_normalized, source_ip
        raise self._error()

    def mark_step_up_state_consumed(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        consumed_at: str,
    ) -> None:
        del login_id_normalized, source_ip, consumed_at
        raise self._error()

    def _error(self) -> LoginError:
        return LoginError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentLoginStepUpStore:
    """Persist deterministic login step-up state in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> LoginStepUpState | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            challenge_id,
                            challenge_channel,
                            challenge_expires_at,
                            user_id,
                            issued_at,
                            consumed_at
                        FROM auth_login_step_up_states
                        WHERE login_id_normalized = %s
                          AND source_ip = %s
                        """,
                        (
                            login_id_normalized,
                            _normalize_source_ip(source_ip),
                        ),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _login_step_up_persistence_unavailable() from error
        if row is None:
            return None
        return LoginStepUpState(
            challenge_id=(
                row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
            ),
            challenge_channel=_coerce_step_up_channel(row[1]),
            challenge_expires_at=_utc_iso(_coerce_datetime(row[2])),
            user_id=_coerce_uuid(row[3]),
            issued_at=_optional_utc_iso(_coerce_optional_datetime(row[4])),
            consumed_at=_optional_utc_iso(_coerce_optional_datetime(row[5])),
        )

    def set_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        step_up_state: LoginStepUpState,
    ) -> None:
        issued_at = (
            _parse_utc_iso(step_up_state.issued_at)
            if step_up_state.issued_at
            else None
        )
        if issued_at is None or step_up_state.user_id is None:
            raise _login_step_up_missing_state()
        challenge_expires_at = _parse_utc_iso(
            step_up_state.challenge_expires_at
        )
        normalized_source_ip = _normalize_source_ip(source_ip)

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> LoginStepUpState:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auth_login_step_up_states (
                        login_id_normalized,
                        source_ip,
                        user_id,
                        challenge_id,
                        challenge_channel,
                        issued_at,
                        challenge_expires_at,
                        consumed_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, now())
                    ON CONFLICT (login_id_normalized, source_ip)
                    DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        challenge_id = EXCLUDED.challenge_id,
                        challenge_channel = EXCLUDED.challenge_channel,
                        issued_at = EXCLUDED.issued_at,
                        challenge_expires_at = EXCLUDED.challenge_expires_at,
                        consumed_at = NULL,
                        updated_at = now()
                    """,
                    (
                        login_id_normalized,
                        normalized_source_ip,
                        step_up_state.user_id,
                        step_up_state.challenge_id,
                        step_up_state.challenge_channel,
                        issued_at,
                        challenge_expires_at,
                    ),
                )
            return step_up_state

        def _reconcile_callback() -> LoginStepUpState | None:
            current_state = self.get_step_up_state(
                login_id_normalized=login_id_normalized,
                source_ip=normalized_source_ip,
            )
            if current_state != step_up_state:
                return None
            return current_state

        try:
            execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AuthCockroachTransactionError as error:
            raise _login_step_up_persistence_unavailable() from error

    def clear_step_up_state(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
    ) -> None:
        normalized_source_ip = _normalize_source_ip(source_ip)

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> bool:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM auth_login_step_up_states
                    WHERE login_id_normalized = %s
                      AND source_ip = %s
                    """,
                    (
                        login_id_normalized,
                        normalized_source_ip,
                    ),
                )
            return True

        def _reconcile_callback() -> bool | None:
            current_state = self.get_step_up_state(
                login_id_normalized=login_id_normalized,
                source_ip=normalized_source_ip,
            )
            if current_state is not None:
                return None
            return True

        try:
            execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AuthCockroachTransactionError as error:
            raise _login_step_up_persistence_unavailable() from error

    def mark_step_up_state_consumed(
        self,
        *,
        login_id_normalized: str,
        source_ip: str,
        consumed_at: str,
    ) -> None:
        consumed_at_value = _parse_utc_iso(consumed_at)
        normalized_source_ip = _normalize_source_ip(source_ip)

        current_state = self.get_step_up_state(
            login_id_normalized=login_id_normalized,
            source_ip=normalized_source_ip,
        )
        if current_state is None:
            raise _login_step_up_missing_state()
        expected_state = LoginStepUpState(
            challenge_id=current_state.challenge_id,
            challenge_channel=current_state.challenge_channel,
            challenge_expires_at=current_state.challenge_expires_at,
            user_id=current_state.user_id,
            issued_at=current_state.issued_at,
            consumed_at=_utc_iso(consumed_at_value),
        )

        def _transaction_callback(
            connection: psycopg.Connection[object],
        ) -> LoginStepUpState:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE auth_login_step_up_states
                    SET consumed_at = %s,
                        updated_at = now()
                    WHERE login_id_normalized = %s
                      AND source_ip = %s
                    """,
                    (
                        consumed_at_value,
                        login_id_normalized,
                        normalized_source_ip,
                    ),
                )
                updated_count = cursor.rowcount
            if updated_count != 1:
                raise _login_step_up_missing_state()
            return expected_state

        def _reconcile_callback() -> LoginStepUpState | None:
            reconciled_state = self.get_step_up_state(
                login_id_normalized=login_id_normalized,
                source_ip=normalized_source_ip,
            )
            if reconciled_state != expected_state:
                return None
            return reconciled_state

        try:
            execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction_callback,
                reconcile_callback=_reconcile_callback,
            )
        except AuthCockroachTransactionError as error:
            raise _login_step_up_persistence_unavailable() from error


def parse_login_request(payload: object) -> LoginRequestRecord:
    """Parse and normalize login payload deterministically."""

    if not isinstance(payload, dict):
        raise LoginError(
            status_code=400,
            error_code="invalid_login_request",
            message="Invalid login request payload.",
            reason="invalid_login_request",
        )
    try:
        request_model = LoginRequest.model_validate(payload)
    except Exception as error:
        raise LoginError(
            status_code=400,
            error_code="invalid_login_request",
            message="Invalid login request payload.",
            reason="invalid_login_request",
        ) from error

    login_id_normalized = _normalize_login_identifier(request_model.login_id)
    password = request_model.password.strip()
    device_fingerprint = _normalize_device_fingerprint(
        request_model.device_fingerprint
    )
    step_up_challenge_id = request_model.step_up_challenge_id
    step_up_otp_code = _normalize_step_up_otp_code(
        request_model.step_up_otp_code
    )
    if not password:
        raise LoginError(
            status_code=400,
            error_code="invalid_login_request",
            message="Invalid login request payload.",
            reason="invalid_login_request",
        )
    if not login_id_normalized:
        raise LoginError(
            status_code=400,
            error_code="login_identifier_invalid_format",
            message="Login identifier must be a valid phone number.",
            reason="login_identifier_invalid_format",
        )
    if (step_up_challenge_id is None) != (step_up_otp_code is None):
        raise LoginError(
            status_code=403,
            error_code="login_step_up_required",
            message="Login step-up verification is required.",
            reason="login_step_up_required",
        )
    if (
        step_up_otp_code is not None
        and _OTP_PATTERN.fullmatch(step_up_otp_code) is None
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_otp_invalid",
            message="Login step-up OTP proof is invalid.",
            reason="login_step_up_otp_invalid",
        )
    return LoginRequestRecord(
        login_id_normalized=login_id_normalized,
        password=password,
        device_fingerprint=device_fingerprint,
        step_up_challenge_id=step_up_challenge_id,
        step_up_otp_code=step_up_otp_code,
    )


def parse_email_otp_login_request(payload: object) -> EmailOtpLoginRequestRecord:
    """Parse and normalize email-OTP login payload deterministically."""

    if not isinstance(payload, dict):
        raise LoginError(
            status_code=400,
            error_code="invalid_login_request",
            message="Invalid login request payload.",
            reason="invalid_login_request",
        )
    try:
        request_model = EmailOtpLoginRequest.model_validate(payload)
    except Exception as error:
        raise LoginError(
            status_code=400,
            error_code="invalid_login_request",
            message="Invalid login request payload.",
            reason="invalid_login_request",
        ) from error

    email_normalized = request_model.email.strip().lower()
    if not email_normalized or _EMAIL_PATTERN.fullmatch(email_normalized) is None:
        raise LoginError(
            status_code=400,
            error_code="login_identifier_invalid_format",
            message="Login identifier must be a valid email address.",
            reason="login_identifier_invalid_format",
        )
    device_fingerprint = _normalize_device_fingerprint(request_model.device_fingerprint)
    step_up_challenge_id = request_model.step_up_challenge_id
    step_up_otp_code = _normalize_step_up_otp_code(request_model.step_up_otp_code)
    if (step_up_challenge_id is None) != (step_up_otp_code is None):
        raise LoginError(
            status_code=403,
            error_code="login_step_up_required",
            message="Login step-up verification is required.",
            reason="login_step_up_required",
        )
    if (
        step_up_otp_code is not None
        and _OTP_PATTERN.fullmatch(step_up_otp_code) is None
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_otp_invalid",
            message="Login step-up OTP proof is invalid.",
            reason="login_step_up_otp_invalid",
        )
    return EmailOtpLoginRequestRecord(
        email_normalized=email_normalized,
        device_fingerprint=device_fingerprint,
        step_up_challenge_id=step_up_challenge_id,
        step_up_otp_code=step_up_otp_code,
    )


def login_with_email_otp(
    *,
    payload: object,
    source_ip: str,
    registration_store: RegistrationStoreProtocol,
    session_issuance_store: SessionIssuanceStoreProtocol,
    login_lockout_store: LoginLockoutStoreProtocol,
    login_step_up_store: LoginStepUpStoreProtocol,
    email_verification_store: EmailVerificationStoreProtocol,
    email_delivery_adapter: EmailOtpDeliveryAdapterProtocol | None = None,
) -> EmailOtpLoginResponseEnvelope:
    """Authenticate via email + OTP only (passwordless)."""

    login_request = parse_email_otp_login_request(payload)
    active_lockout = login_lockout_store.get_active_lockout(
        login_id_normalized=login_request.email_normalized,
        source_ip=source_ip,
    )
    if active_lockout is not None:
        raise LoginError(
            status_code=403,
            error_code="login_lockout_active",
            message="Login is temporarily locked due to repeated failed attempts.",
            reason="login_lockout_active",
            details=_build_lockout_details(active_lockout=active_lockout),
        )

    user_record = registration_store.get_user_by_email(
        email_normalized=login_request.email_normalized
    )
    if user_record is None:
        raise LoginError(
            status_code=401,
            error_code="login_credentials_invalid",
            message="Login credentials are invalid.",
            reason="login_credentials_invalid",
        )

    if user_record.account_state == "locked":
        raise LoginError(
            status_code=403,
            error_code="login_account_locked",
            message="Login is blocked for locked account state.",
            reason="login_account_locked",
            details={
                "current_state": user_record.account_state,
                "requested_state": "active",
            },
        )

    if user_record.account_state == "pending_verification":
        raise LoginError(
            status_code=403,
            error_code="login_account_not_active",
            message="Login is not allowed until account is active.",
            reason="login_account_not_active",
            details={
                "current_state": user_record.account_state,
                "requested_state": "active",
            },
        )

    if user_record.account_state != "active":
        raise LoginError(
            status_code=403,
            error_code="login_forbidden_state",
            message="Login is forbidden for current account state.",
            reason="login_forbidden_state",
            details={
                "current_state": user_record.account_state,
                "requested_state": "active",
            },
        )

    if (
        login_request.step_up_challenge_id is None
        or login_request.step_up_otp_code is None
    ):
        return _issue_email_otp_step_up_challenge(
            login_request=login_request,
            source_ip=source_ip,
            user_record=user_record,
            login_step_up_store=login_step_up_store,
            email_verification_store=email_verification_store,
            email_delivery_adapter=email_delivery_adapter,
        )

    _validate_and_consume_email_otp_step_up_proof(
        login_request=login_request,
        source_ip=source_ip,
        user_record=user_record,
        login_step_up_store=login_step_up_store,
        email_verification_store=email_verification_store,
    )

    try:
        issuance_result = session_issuance_store.issue_session(
            user_id=user_record.user_id,
            tenant_id=get_auth_default_tenant_id(),
            role=user_record.role,
            device_fingerprint=login_request.device_fingerprint,
        )
    except SessionIssuanceError as error:
        raise LoginError(
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error

    login_lockout_store.clear_failed_attempts(
        login_id_normalized=login_request.email_normalized,
        source_ip=source_ip,
    )

    return LoginSuccessEnvelope(
        status="authenticated",
        access_token=issuance_result.access_token,
        refresh_token=issuance_result.refresh_token,
        expires_at=issuance_result.expires_at,
        session=SessionContextEnvelope(
            user_id=user_record.user_id,
            tenant_id=get_auth_default_tenant_id(),
            role=user_record.role,
            session_id=issuance_result.session_id,
            delegation_context=DelegationContextEnvelope(
                is_delegated=False,
            ),
        ),
    )


def _issue_email_otp_step_up_challenge(
    *,
    login_request: EmailOtpLoginRequestRecord,
    source_ip: str,
    user_record: RegisteredUserRecord,
    login_step_up_store: LoginStepUpStoreProtocol,
    email_verification_store: EmailVerificationStoreProtocol,
    email_delivery_adapter: EmailOtpDeliveryAdapterProtocol | None,
) -> EmailOtpLoginStepUpPendingEnvelope:
    existing_state = login_step_up_store.get_step_up_state(
        login_id_normalized=login_request.email_normalized,
        source_ip=source_ip,
    )
    if existing_state is not None:
        if (
            existing_state.consumed_at is None
            and (existing_state.user_id is None or existing_state.user_id == user_record.user_id)
        ):
            email_record = email_verification_store.get_challenge(
                challenge_id=existing_state.challenge_id
            )
            if (
                email_record is not None
                and email_record.consumed_at is None
                and _utc_now() < email_record.expires_at
                and email_record.failed_attempt_count < email_record.max_attempts
            ):
                return EmailOtpLoginStepUpPendingEnvelope(
                    login_status="pending_step_up",
                    status="pending_step_up",
                    step_up_required=True,
                    step_up_purpose="login_step_up",
                    step_up_channel="email",
                    step_up_challenge_id=existing_state.challenge_id,
                    step_up_expires_at=existing_state.challenge_expires_at,
                )
        login_step_up_store.clear_step_up_state(
            login_id_normalized=login_request.email_normalized,
            source_ip=source_ip,
        )

    issued_at = _utc_now()
    request_fingerprint = (
        f"login_step_up:{user_record.user_id}:{login_request.email_normalized}:"
        f"{_normalize_source_ip(source_ip)}:email"
    )
    idempotency_key = sha256(
        f"{request_fingerprint}:{issued_at.isoformat()}:{uuid4()}".encode()
    ).hexdigest()
    otp_policy = get_auth_otp_policy_for_purpose("login_step_up")

    challenge_response = email_verification_store.issue_challenge(
        purpose="login_step_up",
        email_normalized=user_record.email_normalized,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=otp_policy.ttl_seconds),
        max_attempts=otp_policy.max_attempts,
        resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
        resend_max_per_window=otp_policy.resend_max_per_window,
        resend_window_seconds=otp_policy.resend_window_seconds,
        cooldown_seconds=otp_policy.cooldown_seconds,
    )
    email_challenge_record = email_verification_store.get_challenge(
        challenge_id=challenge_response.challenge_id
    )
    if email_challenge_record is None:
        raise _login_step_up_missing_state()

    resolved_email_delivery_adapter: EmailOtpDeliveryAdapterProtocol = (
        email_delivery_adapter
        if email_delivery_adapter is not None
        else get_default_email_otp_delivery_adapter()
    )
    delivery_outcome = normalize_email_delivery_outcome(
        outcome=resolved_email_delivery_adapter.send_otp_challenge(
            message=_build_login_email_otp_message(
                email_normalized=user_record.email_normalized,
                otp_code=email_challenge_record.otp_code,
                challenge_id=challenge_response.challenge_id,
                expires_at=email_challenge_record.expires_at,
            )
        )
    )
    if delivery_outcome.status != "delivered":
        raise LoginError(
            status_code=409,
            error_code="login_step_up_email_delivery_failed",
            message="Login step-up email delivery failed.",
            reason="login_step_up_email_delivery_failed",
            details={
                "primary_channel": "email",
                "delivery_failure_class": delivery_outcome.status,
                "delivery_reason_code": delivery_outcome.reason_code,
            },
        )

    step_up_state = LoginStepUpState(
        challenge_id=challenge_response.challenge_id,
        challenge_channel="email",
        challenge_expires_at=challenge_response.expires_at,
        user_id=user_record.user_id,
        issued_at=_utc_iso(issued_at),
    )
    login_step_up_store.set_step_up_state(
        login_id_normalized=login_request.email_normalized,
        source_ip=source_ip,
        step_up_state=step_up_state,
    )
    return EmailOtpLoginStepUpPendingEnvelope(
        login_status="pending_step_up",
        status="pending_step_up",
        step_up_required=True,
        step_up_purpose="login_step_up",
        step_up_channel="email",
        step_up_challenge_id=step_up_state.challenge_id,
        step_up_expires_at=step_up_state.challenge_expires_at,
    )


def _validate_and_consume_email_otp_step_up_proof(
    *,
    login_request: EmailOtpLoginRequestRecord,
    source_ip: str,
    user_record: RegisteredUserRecord,
    login_step_up_store: LoginStepUpStoreProtocol,
    email_verification_store: EmailVerificationStoreProtocol,
) -> None:
    step_up_state = login_step_up_store.get_step_up_state(
        login_id_normalized=login_request.email_normalized,
        source_ip=source_ip,
    )
    if (
        step_up_state is None
        or login_request.step_up_challenge_id is None
        or login_request.step_up_challenge_id != step_up_state.challenge_id
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )
    if step_up_state.consumed_at is not None:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_already_used",
            message="Login step-up challenge has already been used.",
            reason="login_step_up_challenge_already_used",
        )
    if (
        step_up_state.user_id is not None
        and step_up_state.user_id != user_record.user_id
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )

    _validate_and_consume_email_step_up(
        challenge_id=step_up_state.challenge_id,
        otp_code=login_request.step_up_otp_code or "",
        user_record=user_record,
        email_verification_store=email_verification_store,
    )

    login_step_up_store.mark_step_up_state_consumed(
        login_id_normalized=login_request.email_normalized,
        source_ip=source_ip,
        consumed_at=_utc_iso(_utc_now()),
    )


def login_with_credentials(
    *,
    payload: object,
    source_ip: str,
    registration_store: RegistrationStoreProtocol,
    session_issuance_store: SessionIssuanceStoreProtocol,
    login_lockout_store: LoginLockoutStoreProtocol,
    login_step_up_store: LoginStepUpStoreProtocol,
    email_verification_store: EmailVerificationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
    sms_delivery_adapter: SmsOtpDeliveryAdapterProtocol | None = None,
) -> LoginResponseEnvelope:
    """Authenticate deterministic credentials with mandatory OTP step-up."""

    login_request = parse_login_request(payload)
    active_lockout = login_lockout_store.get_active_lockout(
        login_id_normalized=login_request.login_id_normalized,
        source_ip=source_ip,
    )
    if active_lockout is not None:
        raise LoginError(
            status_code=403,
            error_code="login_lockout_active",
            message="Login is temporarily locked due to repeated failed attempts.",
            reason="login_lockout_active",
            details=_build_lockout_details(active_lockout=active_lockout),
        )
    user_record = _resolve_user_by_login_identifier(
        login_id_normalized=login_request.login_id_normalized,
        registration_store=registration_store,
    )
    if user_record is not None and not is_supported_password_hash(
        password_hash=user_record.password_hash
    ):
        raise LoginError(
            status_code=401,
            error_code="password_hash_verification_failed",
            message="Login credentials are invalid.",
            reason="password_hash_verification_failed",
        )

    stored_hash = (
        user_record.password_hash
        if user_record is not None
        else _DUMMY_PASSWORD_HASH
    )
    password_valid = verify_password_against_hash(
        password=login_request.password,
        password_hash=stored_hash,
    )
    if user_record is None or not password_valid:
        threshold_lockout = login_lockout_store.register_failed_attempt(
            login_id_normalized=login_request.login_id_normalized,
            source_ip=source_ip,
        )
        if threshold_lockout is not None:
            raise LoginError(
                status_code=403,
                error_code="login_lockout_threshold_exceeded",
                message="Login is temporarily locked due to repeated failed attempts.",
                reason="login_lockout_threshold_exceeded",
                details=_build_lockout_details(
                    active_lockout=threshold_lockout
                ),
            )
        raise LoginError(
            status_code=401,
            error_code="login_invalid_credentials",
            message="Login credentials are invalid.",
            reason="login_invalid_credentials",
        )
    if is_legacy_password_hash(password_hash=user_record.password_hash):
        registration_store.update_user_password_hash(
            user_id=user_record.user_id,
            password_hash=build_password_hash(password=login_request.password),
        )
        user_record = registration_store.get_user_by_id(
            user_id=user_record.user_id
        )
        if user_record is None:
            raise LoginError(
                status_code=401,
                error_code="password_hash_verification_failed",
                message="Login credentials are invalid.",
                reason="password_hash_verification_failed",
            )

    if user_record.account_state == "locked":
        raise LoginError(
            status_code=403,
            error_code="login_account_locked",
            message="Login is blocked for locked account state.",
            reason="login_account_locked",
            details={
                "current_state": user_record.account_state,
                "requested_state": "active",
            },
        )

    if user_record.account_state == "pending_verification":
        raise LoginError(
            status_code=403,
            error_code="login_account_not_active",
            message="Login is not allowed until account is active.",
            reason="login_account_not_active",
            details={
                "current_state": user_record.account_state,
                "requested_state": "active",
            },
        )

    if (
        user_record.account_state != "active"
        or user_record.credentials_invalidated_at is not None
    ):
        raise LoginError(
            status_code=403,
            error_code="login_forbidden_state",
            message="Login is forbidden for current account state.",
            reason="login_forbidden_state",
            details={
                "current_state": user_record.account_state,
                "requested_state": "active",
            },
        )

    if (
        login_request.step_up_challenge_id is None
        or login_request.step_up_otp_code is None
    ):
        return _issue_or_reuse_step_up_challenge(
            login_request=login_request,
            source_ip=source_ip,
            user_record=user_record,
            login_step_up_store=login_step_up_store,
            email_verification_store=email_verification_store,
            phone_verification_store=phone_verification_store,
            sms_delivery_adapter=sms_delivery_adapter,
        )

    _validate_and_consume_step_up_proof(
        login_request=login_request,
        source_ip=source_ip,
        user_record=user_record,
        login_step_up_store=login_step_up_store,
        email_verification_store=email_verification_store,
        phone_verification_store=phone_verification_store,
    )

    try:
        issuance_result = session_issuance_store.issue_session(
            user_id=user_record.user_id,
            tenant_id=get_auth_default_tenant_id(),
            role=user_record.role,
            device_fingerprint=login_request.device_fingerprint,
        )
    except SessionIssuanceError as error:
        raise LoginError(
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error

    login_lockout_store.clear_failed_attempts(
        login_id_normalized=login_request.login_id_normalized,
        source_ip=source_ip,
    )

    return LoginSuccessEnvelope(
        status="authenticated",
        access_token=issuance_result.access_token,
        refresh_token=issuance_result.refresh_token,
        expires_at=issuance_result.expires_at,
        session=SessionContextEnvelope(
            user_id=user_record.user_id,
            tenant_id=get_auth_default_tenant_id(),
            role=user_record.role,
            session_id=issuance_result.session_id,
            delegation_context=DelegationContextEnvelope(
                is_delegated=False,
            ),
        ),
    )


def _issue_or_reuse_step_up_challenge(
    *,
    login_request: LoginRequestRecord,
    source_ip: str,
    user_record: RegisteredUserRecord,
    login_step_up_store: LoginStepUpStoreProtocol,
    email_verification_store: EmailVerificationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
    sms_delivery_adapter: SmsOtpDeliveryAdapterProtocol | None,
    email_delivery_adapter: EmailOtpDeliveryAdapterProtocol | None = None,
) -> LoginStepUpPendingEnvelope:
    existing_state = login_step_up_store.get_step_up_state(
        login_id_normalized=login_request.login_id_normalized,
        source_ip=source_ip,
    )
    if existing_state is not None:
        if _is_step_up_state_active(
            step_up_state=existing_state,
            user_record=user_record,
            email_verification_store=email_verification_store,
            phone_verification_store=phone_verification_store,
        ):
            return _build_step_up_pending_envelope(step_up_state=existing_state)
        login_step_up_store.clear_step_up_state(
            login_id_normalized=login_request.login_id_normalized,
            source_ip=source_ip,
        )

    channel = _resolve_step_up_channel(
        login_id_normalized=login_request.login_id_normalized
    )
    issued_at = _utc_now()
    request_fingerprint = (
        f"login_step_up:{user_record.user_id}:{login_request.login_id_normalized}:"
        f"{_normalize_source_ip(source_ip)}:{channel}"
    )
    idempotency_key = sha256(
        f"{request_fingerprint}:{issued_at.isoformat()}:{uuid4()}".encode()
    ).hexdigest()
    otp_policy = get_auth_otp_policy_for_purpose("login_step_up")

    if channel == "email":
        challenge_response = email_verification_store.issue_challenge(
            purpose="login_step_up",
            email_normalized=user_record.email_normalized,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=otp_policy.ttl_seconds),
            max_attempts=otp_policy.max_attempts,
            resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
            resend_max_per_window=otp_policy.resend_max_per_window,
            resend_window_seconds=otp_policy.resend_window_seconds,
            cooldown_seconds=otp_policy.cooldown_seconds,
        )
        email_challenge_record = email_verification_store.get_challenge(
            challenge_id=challenge_response.challenge_id
        )
        if email_challenge_record is None:
            raise _login_step_up_missing_state()
        resolved_email_delivery_adapter: EmailOtpDeliveryAdapterProtocol = (
            email_delivery_adapter
            if email_delivery_adapter is not None
            else get_default_email_otp_delivery_adapter()
        )
        email_delivery_outcome = normalize_email_delivery_outcome(
            outcome=resolved_email_delivery_adapter.send_otp_challenge(
                message=_build_login_email_otp_message(
                    email_normalized=user_record.email_normalized,
                    otp_code=email_challenge_record.otp_code,
                    challenge_id=challenge_response.challenge_id,
                    expires_at=email_challenge_record.expires_at,
                )
            )
        )
        if email_delivery_outcome.status != "delivered":
            raise LoginError(
                status_code=409,
                error_code="login_step_up_email_delivery_failed",
                message="Login step-up email delivery failed.",
                reason="login_step_up_email_delivery_failed",
                details={
                    "primary_channel": "email",
                    "delivery_failure_class": email_delivery_outcome.status,
                    "delivery_reason_code": email_delivery_outcome.reason_code,
                },
            )
    else:
        challenge_response = phone_verification_store.issue_challenge(
            purpose="login_step_up",
            phone_number_normalized=user_record.phone_number_normalized,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=otp_policy.ttl_seconds),
            max_attempts=otp_policy.max_attempts,
            resend_min_interval_seconds=otp_policy.resend_min_interval_seconds,
            resend_max_per_window=otp_policy.resend_max_per_window,
            resend_window_seconds=otp_policy.resend_window_seconds,
            cooldown_seconds=otp_policy.cooldown_seconds,
        )
        challenge_record = phone_verification_store.get_challenge(
            challenge_id=challenge_response.challenge_id
        )
        if challenge_record is None:
            raise _login_step_up_missing_state()
        resolved_sms_delivery_adapter: SmsOtpDeliveryAdapterProtocol = (
            sms_delivery_adapter
            if sms_delivery_adapter is not None
            else get_default_sms_otp_delivery_adapter()
        )
        sms_delivery_outcome: OtpDeliveryOutcome = (
            resolved_sms_delivery_adapter.send_otp_challenge(
                purpose="login_step_up",
                phone_number_normalized=user_record.phone_number_normalized,
                otp_code=challenge_record.otp_code,
            )
        )
        delivery_result = normalize_sms_delivery_outcome(
            outcome=sms_delivery_outcome
        )
        if delivery_result.status != "delivered":
            raise LoginError(
                status_code=409,
                error_code=_resolve_step_up_sms_delivery_error_code(
                    delivery_result=delivery_result
                ),
                message="Login step-up SMS delivery failed.",
                reason=_resolve_step_up_sms_delivery_reason(
                    delivery_result=delivery_result
                ),
                details={
                    "primary_channel": "sms",
                    "delivery_failure_class": delivery_result.status,
                    "delivery_reason_code": delivery_result.reason_code,
                    "provider_ref": delivery_result.provider_ref,
                },
            )

    step_up_state = LoginStepUpState(
        challenge_id=challenge_response.challenge_id,
        challenge_channel=channel,
        challenge_expires_at=challenge_response.expires_at,
        user_id=user_record.user_id,
        issued_at=_utc_iso(issued_at),
    )
    login_step_up_store.set_step_up_state(
        login_id_normalized=login_request.login_id_normalized,
        source_ip=source_ip,
        step_up_state=step_up_state,
    )
    return _build_step_up_pending_envelope(step_up_state=step_up_state)


def _validate_and_consume_step_up_proof(
    *,
    login_request: LoginRequestRecord,
    source_ip: str,
    user_record: RegisteredUserRecord,
    login_step_up_store: LoginStepUpStoreProtocol,
    email_verification_store: EmailVerificationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
) -> None:
    step_up_state = login_step_up_store.get_step_up_state(
        login_id_normalized=login_request.login_id_normalized,
        source_ip=source_ip,
    )
    if (
        step_up_state is None
        or login_request.step_up_challenge_id is None
        or login_request.step_up_challenge_id != step_up_state.challenge_id
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )
    if step_up_state.consumed_at is not None:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_already_used",
            message="Login step-up challenge has already been used.",
            reason="login_step_up_challenge_already_used",
        )
    if (
        step_up_state.user_id is not None
        and step_up_state.user_id != user_record.user_id
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )

    if step_up_state.challenge_channel == "email":
        _validate_and_consume_email_step_up(
            challenge_id=step_up_state.challenge_id,
            otp_code=login_request.step_up_otp_code or "",
            user_record=user_record,
            email_verification_store=email_verification_store,
        )
    else:
        _validate_and_consume_phone_step_up(
            challenge_id=step_up_state.challenge_id,
            otp_code=login_request.step_up_otp_code or "",
            user_record=user_record,
            phone_verification_store=phone_verification_store,
        )

    login_step_up_store.mark_step_up_state_consumed(
        login_id_normalized=login_request.login_id_normalized,
        source_ip=source_ip,
        consumed_at=_utc_iso(_utc_now()),
    )


def _validate_and_consume_email_step_up(
    *,
    challenge_id: UUID,
    otp_code: str,
    user_record: RegisteredUserRecord,
    email_verification_store: EmailVerificationStoreProtocol,
) -> None:
    challenge_record = email_verification_store.get_challenge(
        challenge_id=challenge_id
    )
    if challenge_record is None:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up challenge is invalid.",
            reason="login_step_up_challenge_invalid",
        )
    if challenge_record.consumed_at is not None:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_already_used",
            message="Login step-up challenge has already been used.",
            reason="login_step_up_challenge_already_used",
        )
    if challenge_record.purpose != "login_step_up":
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )
    if challenge_record.email_normalized != user_record.email_normalized:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )

    now = _utc_now()
    if now >= challenge_record.expires_at:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_expired",
            message="Login step-up proof has expired.",
            reason="login_step_up_challenge_expired",
        )
    if otp_code != challenge_record.otp_code:
        updated_challenge = email_verification_store.increment_failed_attempt_count(
            challenge_id=challenge_record.challenge_id
        )
        if updated_challenge.failed_attempt_count >= updated_challenge.max_attempts:
            raise LoginError(
                status_code=409,
                error_code="login_step_up_otp_attempt_limit_exceeded",
                message="Login step-up OTP attempt limit has been exceeded.",
                reason="login_step_up_otp_attempt_limit_exceeded",
            )
        raise LoginError(
            status_code=409,
            error_code="login_step_up_otp_invalid",
            message="Login step-up OTP proof is invalid.",
            reason="login_step_up_otp_invalid",
        )

    email_verification_store.mark_challenge_consumed(
        challenge_id=challenge_record.challenge_id,
        consumed_at=now,
    )


def _validate_and_consume_phone_step_up(
    *,
    challenge_id: UUID,
    otp_code: str,
    user_record: RegisteredUserRecord,
    phone_verification_store: PhoneVerificationStoreProtocol,
) -> None:
    challenge_record = phone_verification_store.get_challenge(
        challenge_id=challenge_id
    )
    if challenge_record is None:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up challenge is invalid.",
            reason="login_step_up_challenge_invalid",
        )
    if challenge_record.consumed_at is not None:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_already_used",
            message="Login step-up challenge has already been used.",
            reason="login_step_up_challenge_already_used",
        )
    if challenge_record.purpose != "login_step_up":
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )
    if (
        challenge_record.phone_number_normalized
        != user_record.phone_number_normalized
    ):
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_invalid",
            message="Login step-up proof context does not match login context.",
            reason="login_step_up_challenge_invalid",
        )

    now = _utc_now()
    if now >= challenge_record.expires_at:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_challenge_expired",
            message="Login step-up proof has expired.",
            reason="login_step_up_challenge_expired",
        )
    if challenge_record.failed_attempt_count >= challenge_record.max_attempts:
        raise LoginError(
            status_code=409,
            error_code="login_step_up_otp_attempt_limit_exceeded",
            message="Login step-up OTP attempt limit has been exceeded.",
            reason="login_step_up_otp_attempt_limit_exceeded",
        )
    if otp_code != challenge_record.otp_code:
        updated_challenge = (
            phone_verification_store.increment_failed_attempt_count(
                challenge_id=challenge_record.challenge_id
            )
        )
        if (
            updated_challenge.failed_attempt_count
            >= updated_challenge.max_attempts
        ):
            raise LoginError(
                status_code=409,
                error_code="login_step_up_otp_attempt_limit_exceeded",
                message="Login step-up OTP attempt limit has been exceeded.",
                reason="login_step_up_otp_attempt_limit_exceeded",
            )
        raise LoginError(
            status_code=409,
            error_code="login_step_up_otp_invalid",
            message="Login step-up OTP proof is invalid.",
            reason="login_step_up_otp_invalid",
        )

    phone_verification_store.mark_challenge_consumed(
        challenge_id=challenge_record.challenge_id,
        consumed_at=now,
    )


def _is_step_up_state_active(
    *,
    step_up_state: LoginStepUpState,
    user_record: RegisteredUserRecord,
    email_verification_store: EmailVerificationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
) -> bool:
    if step_up_state.challenge_channel == "email":
        challenge_record = email_verification_store.get_challenge(
            challenge_id=step_up_state.challenge_id
        )
        if challenge_record is None:
            return False
        return _is_active_email_step_up_record(
            step_up_state=step_up_state,
            challenge_record=challenge_record,
            user_record=user_record,
        )

    challenge_record = phone_verification_store.get_challenge(
        challenge_id=step_up_state.challenge_id
    )
    if challenge_record is None:
        return False
    return _is_active_phone_step_up_record(
        step_up_state=step_up_state,
        challenge_record=challenge_record,
        user_record=user_record,
    )


def _is_active_email_step_up_record(
    *,
    step_up_state: LoginStepUpState,
    challenge_record: EmailVerificationChallengeRecord,
    user_record: RegisteredUserRecord,
) -> bool:
    return (
        challenge_record.purpose == "login_step_up"
        and challenge_record.email_normalized == user_record.email_normalized
        and (
            step_up_state.user_id is None
            or step_up_state.user_id == user_record.user_id
        )
        and step_up_state.consumed_at is None
        and challenge_record.consumed_at is None
        and _utc_now() < challenge_record.expires_at
    )


def _is_active_phone_step_up_record(
    *,
    step_up_state: LoginStepUpState,
    challenge_record: PhoneVerificationChallengeRecord,
    user_record: RegisteredUserRecord,
) -> bool:
    return (
        challenge_record.purpose == "login_step_up"
        and challenge_record.phone_number_normalized
        == user_record.phone_number_normalized
        and (
            step_up_state.user_id is None
            or step_up_state.user_id == user_record.user_id
        )
        and step_up_state.consumed_at is None
        and challenge_record.consumed_at is None
        and _utc_now() < challenge_record.expires_at
        and challenge_record.failed_attempt_count
        < challenge_record.max_attempts
    )


def _resolve_step_up_channel(
    *, login_id_normalized: str
) -> Literal["email", "sms"]:
    _ = login_id_normalized
    channel_policy = get_auth_otp_channel_policy_for_purpose("login_step_up")
    if channel_policy.channel == "email":
        return "email"
    return "sms"


def _resolve_step_up_sms_delivery_error_code(
    *,
    delivery_result: OtpDeliveryOutcome,
) -> str:
    if delivery_result.status == "failed_retryable":
        return "login_step_up_sms_delivery_failed_retryable"
    return "login_step_up_sms_delivery_failed_non_retryable"


def _resolve_step_up_sms_delivery_reason(
    *,
    delivery_result: OtpDeliveryOutcome,
) -> str:
    if delivery_result.reason_code == "otp_delivery_provider_misconfigured":
        return "otp_delivery_provider_misconfigured"
    if delivery_result.reason_code == "sms_delivery_provider_timeout":
        return "login_step_up_sms_delivery_timeout"
    if delivery_result.reason_code == "sms_delivery_provider_unavailable":
        return "login_step_up_sms_delivery_unavailable"
    return "login_step_up_sms_delivery_rejected"


def _build_step_up_pending_envelope(
    *,
    step_up_state: LoginStepUpState,
) -> LoginStepUpPendingEnvelope:
    return LoginStepUpPendingEnvelope(
        login_status="pending_step_up",
        status="pending_step_up",
        step_up_required=True,
        step_up_purpose="login_step_up",
        step_up_channel=step_up_state.challenge_channel,
        step_up_challenge_id=step_up_state.challenge_id,
        step_up_expires_at=step_up_state.challenge_expires_at,
    )


def _build_login_email_otp_message(
    *,
    email_normalized: str,
    otp_code: str,
    challenge_id: UUID,
    expires_at: datetime,
) -> EmailOtpMessage:
    import html
    escaped_otp_code = html.escape(otp_code)
    escaped_expires_at = html.escape(_utc_iso(expires_at))
    escaped_challenge_id = html.escape(str(challenge_id))
    content = (
        "<html><body>"
        "<p>Use this KODI one-time code to finish signing in.</p>"
        f"<p><strong>{escaped_otp_code}</strong></p>"
        f"<p>This code expires at {escaped_expires_at}.</p>"
        f"<p>Challenge reference: {escaped_challenge_id}</p>"
        "</body></html>"
    )
    return EmailOtpMessage(
        purpose="login_step_up",
        email_normalized=email_normalized,
        subject="Your KODI login verification code",
        content=content,
        challenge_id=str(challenge_id),
    )


def _normalize_login_identifier(login_id: str) -> str:
    normalized = login_id.strip()
    if not normalized:
        return ""
    if "@" in normalized:
        raise LoginError(
            status_code=400,
            error_code="login_identifier_unsupported_type",
            message="Login identifier type is unsupported. Use phone number.",
            reason="login_identifier_unsupported_type",
        )
    cleaned = _PHONE_CLEAN_PATTERN.sub("", normalized)
    national_number: str
    if cleaned.startswith("+254"):
        national_number = cleaned[4:]
    elif cleaned.startswith("254"):
        national_number = cleaned[3:]
    elif cleaned.startswith("0"):
        national_number = cleaned[1:]
    else:
        raise LoginError(
            status_code=400,
            error_code="login_identifier_invalid_format",
            message="Login identifier must be a valid phone number.",
            reason="login_identifier_invalid_format",
        )

    if _KENYA_NATIONAL_PHONE_PATTERN.fullmatch(national_number) is None:
        raise LoginError(
            status_code=400,
            error_code="login_identifier_invalid_format",
            message="Login identifier must be a valid phone number.",
            reason="login_identifier_invalid_format",
        )
    return f"+254{national_number}"


def _normalize_device_fingerprint(device_fingerprint: str | None) -> str | None:
    if device_fingerprint is None:
        return None
    normalized = device_fingerprint.strip()
    if not normalized:
        return None
    return normalized


def _normalize_step_up_otp_code(step_up_otp_code: str | None) -> str | None:
    if step_up_otp_code is None:
        return None
    normalized = step_up_otp_code.strip()
    if not normalized:
        return None
    return normalized


def _normalize_source_ip(source_ip: str) -> str:
    normalized = source_ip.strip().lower()
    if not normalized:
        return "unknown"
    return normalized


def _resolve_user_by_login_identifier(
    *,
    login_id_normalized: str,
    registration_store: RegistrationStoreProtocol,
) -> RegisteredUserRecord | None:
    return registration_store.get_user_by_phone(
        phone_number_normalized=login_id_normalized
    )


def _build_lockout_details(
    *, active_lockout: LoginLockoutState
) -> dict[str, object]:
    return {
        "lockout_expires_at": active_lockout.lockout_expires_at,
        "lockout_remaining_seconds": active_lockout.lockout_remaining_seconds,
    }


def is_login_lockout_reason(*, reason: str) -> bool:
    """Return whether a login reason represents active/applied lockout state."""

    return reason in {
        "login_lockout_active",
        "login_lockout_threshold_exceeded",
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc_iso(value)


def _parse_utc_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _coerce_step_up_channel(value: object) -> Literal["email", "sms"]:
    normalized_value = str(value)
    if normalized_value == "email":
        return "email"
    return "sms"


def _coerce_datetime(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def _coerce_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value)


def _coerce_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _login_lockout_persistence_unavailable() -> LoginError:
    return LoginError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _login_step_up_persistence_unavailable() -> LoginError:
    return LoginError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


def _login_step_up_missing_state() -> LoginError:
    return LoginError(
        status_code=503,
        error_code="auth_persistence_missing_state",
        message="Required auth persistence state is missing.",
        reason="auth_persistence_missing_state",
    )


_LOGIN_LOCKOUT_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "auth_login_lockouts": (
        "login_id_normalized",
        "source_ip",
        "failed_attempt_count",
        "last_failed_attempt_at",
        "lockout_expires_at",
    ),
}

_LOGIN_STEP_UP_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "auth_login_step_up_states": (
        "login_id_normalized",
        "source_ip",
        "user_id",
        "challenge_id",
        "challenge_channel",
        "issued_at",
        "challenge_expires_at",
        "consumed_at",
    ),
}


def build_default_login_lockout_store() -> LoginLockoutStoreProtocol:
    """Build the auth login-lockout store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryLoginLockoutStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableLoginLockoutStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentLoginLockoutStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableLoginLockoutStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailableLoginLockoutStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_login_lockout_store = build_default_login_lockout_store()


def build_default_login_step_up_store() -> LoginStepUpStoreProtocol:
    """Build the auth login step-up store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryLoginStepUpStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableLoginStepUpStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentLoginStepUpStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableLoginStepUpStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailableLoginStepUpStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_login_step_up_store = build_default_login_step_up_store()


def get_default_login_lockout_store() -> LoginLockoutStoreProtocol:
    """Return deterministic process-local login lockout store instance."""

    return _default_login_lockout_store


def reset_default_login_lockout_store() -> None:
    """Reset process-local login lockout store for isolated tests."""

    global _default_login_lockout_store
    _default_login_lockout_store = build_default_login_lockout_store()


def get_default_login_step_up_store() -> LoginStepUpStoreProtocol:
    """Return deterministic process-local login step-up store instance."""

    return _default_login_step_up_store


def reset_default_login_step_up_store() -> None:
    """Reset process-local login step-up store for isolated tests."""

    global _default_login_step_up_store
    _default_login_step_up_store = build_default_login_step_up_store()
