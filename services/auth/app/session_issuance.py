"""Deterministic session issuance with policy controls and bounded expiry."""

from __future__ import annotations

import re
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
from collections.abc import Callable

import psycopg

from services.auth.app.config import get_auth_session_warning_window_seconds
from services.auth.app.config import get_auth_session_max_concurrent_sessions
from services.auth.app.config import get_auth_session_absolute_lifetime_seconds
from services.auth.app.config import get_auth_session_inactivity_timeout_seconds
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import auth_runtime_requires_persistence
from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import validate_auth_database_connection

_TOKEN_NAMESPACE = "kodi_auth_session_issuance"
_REFRESH_TOKEN_PATTERN = re.compile(r"^refresh_[0-9a-f]{64}$")
SessionStatus = Literal["active", "warning", "expired", "invalidated"]
InvalidationReason = Literal["session_concurrency_limit_enforced", "session_revoked"]


@dataclass(frozen=True)
class SessionIssuanceResult:
    """Represent one issued auth session/token bundle."""

    session_id: UUID
    access_token: str
    refresh_token: str
    expires_at: str
    evicted_session_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class SessionPolicyEvaluation:
    """Represent deterministic policy evaluation result for one session."""

    session_id: UUID
    status: SessionStatus
    reason_code: str | None
    issued_at: str
    expires_at: str
    last_activity_at: str
    inactivity_expires_at: str
    absolute_expires_at: str
    warning_window_started_at: str
    is_warning_window: bool
    extension_allowed: bool
    is_invalidated: bool
    invalidated_at: str | None
    invalidated_reason: InvalidationReason | None


@dataclass(frozen=True)
class IssuedSessionRecord:
    """Represent one persisted issued session record."""

    session_id: UUID
    user_id: UUID
    tenant_id: str
    role: str
    issued_at: str
    expires_at: str
    inactivity_expires_at: str
    last_activity_at: str
    is_invalidated: bool
    invalidated_at: str | None
    invalidated_reason: InvalidationReason | None
    device_fingerprint: str | None
    access_token_hash: str
    refresh_token_hash: str


@dataclass(frozen=True)
class RefreshTokenRecord:
    """Represent one persisted refresh-token issuance record."""

    refresh_token_hash: str
    session_id: UUID
    issued_at: str
    is_consumed: bool
    consumed_at: str | None


@dataclass(frozen=True)
class _PolicyConfig:
    inactivity_timeout_seconds: int
    absolute_lifetime_seconds: int
    warning_window_seconds: int
    max_concurrent_sessions: int


class SessionIssuanceError(ValueError):
    """Represent deterministic session issuance failure."""

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


class SessionIssuanceStoreProtocol(Protocol):
    """Define persistence boundary for deterministic session issuance."""

    def issue_session(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        role: str,
        device_fingerprint: str | None,
    ) -> SessionIssuanceResult:
        """Issue deterministic session and token bundle."""

        ...

    def get_session(self, *, session_id: UUID) -> IssuedSessionRecord | None:
        """Return one issued session by identifier when present."""

        ...

    def get_sessions_for_user(self, *, user_id: UUID) -> list[IssuedSessionRecord]:
        """Return deterministic ordered issued sessions for one user."""

        ...

    def evaluate_session(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        """Return deterministic session-policy evaluation for one session."""

        ...

    def touch_session_activity(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        """Update last activity and inactivity expiry where allowed."""

        ...

    def extend_session(self, *, session_id: UUID) -> SessionPolicyEvaluation:
        """Extend session inactivity expiry only inside warning window."""

        ...

    def get_total_session_count(self) -> int:
        """Return total issued-session count."""

        ...

    def refresh_session(self, *, refresh_token: str) -> SessionIssuanceResult:
        """Rotate refresh token and issue deterministic updated token bundle."""

        ...

    def revoke_session(self, *, user_id: UUID, session_id: UUID) -> int:
        """Revoke one owned session deterministically and return revoked count."""

        ...

    def revoke_all_sessions_for_user(self, *, user_id: UUID) -> int:
        """Revoke all active/warning sessions for one user and return revoked count."""

        ...


class InMemorySessionIssuanceStore:
    """Persist deterministic session issuance records in memory."""

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
        inactivity_timeout_seconds: int | None = None,
        absolute_lifetime_seconds: int | None = None,
        warning_window_seconds: int | None = None,
        max_concurrent_sessions: int | None = None,
    ) -> None:
        self._now_provider = now_provider or _utc_now
        self._policy = _resolve_policy_config(
            inactivity_timeout_seconds=inactivity_timeout_seconds,
            absolute_lifetime_seconds=absolute_lifetime_seconds,
            warning_window_seconds=warning_window_seconds,
            max_concurrent_sessions=max_concurrent_sessions,
        )
        self._sessions_by_id: dict[UUID, IssuedSessionRecord] = {}
        self._sessions_by_user: dict[UUID, list[UUID]] = {}
        self._active_refresh_hash_to_session_id: dict[str, UUID] = {}
        self._consumed_refresh_token_hashes: set[str] = set()
        self._lock = Lock()

    def issue_session(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        role: str,
        device_fingerprint: str | None,
    ) -> SessionIssuanceResult:
        """Issue deterministic session and opaque token bundle."""

        try:
            now = self._now_provider()
            absolute_expires_at_value = now + timedelta(
                seconds=self._policy.absolute_lifetime_seconds
            )
            inactivity_expires_at_value = now + timedelta(
                seconds=self._policy.inactivity_timeout_seconds
            )
            expires_at_value = min(absolute_expires_at_value, inactivity_expires_at_value)
            session_id = uuid4()
            access_token = _build_opaque_token(
                token_kind="access",
                user_id=user_id,
                session_id=session_id,
                issued_at=now,
            )
            refresh_token = _build_opaque_token(
                token_kind="refresh",
                user_id=user_id,
                session_id=session_id,
                issued_at=now,
            )

            issued_at = _utc_iso(now)
            inactivity_expires_at = _utc_iso(inactivity_expires_at_value)
            expires_at = _utc_iso(expires_at_value)

            record = IssuedSessionRecord(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                role=role,
                issued_at=issued_at,
                expires_at=expires_at,
                inactivity_expires_at=inactivity_expires_at,
                last_activity_at=issued_at,
                is_invalidated=False,
                invalidated_at=None,
                invalidated_reason=None,
                device_fingerprint=device_fingerprint,
                access_token_hash=sha256(access_token.encode("utf-8")).hexdigest(),
                refresh_token_hash=sha256(refresh_token.encode("utf-8")).hexdigest(),
            )
            with self._lock:
                evicted_session_ids = self._enforce_concurrency_limit_locked(
                    user_id=user_id,
                    invalidated_at=now,
                )
                self._sessions_by_id[session_id] = record
                existing_user_sessions = self._sessions_by_user.get(user_id, [])
                self._sessions_by_user[user_id] = [*existing_user_sessions, session_id]
                self._active_refresh_hash_to_session_id[record.refresh_token_hash] = session_id

            return SessionIssuanceResult(
                session_id=session_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=record.expires_at,
                evicted_session_ids=evicted_session_ids,
            )
        except SessionIssuanceError:
            raise
        except Exception as error:
            raise SessionIssuanceError(
                status_code=500,
                error_code="session_issuance_failed",
                message="Session issuance failed.",
                reason="session_issuance_failed",
            ) from error

    def refresh_session(self, *, refresh_token: str) -> SessionIssuanceResult:
        """Rotate refresh token and issue deterministic updated token bundle."""

        normalized_refresh_token = refresh_token.strip()
        if _REFRESH_TOKEN_PATTERN.fullmatch(normalized_refresh_token) is None:
            raise SessionIssuanceError(
                status_code=401,
                error_code="refresh_token_malformed",
                message="Refresh token is malformed.",
                reason="refresh_token_malformed",
            )

        refresh_token_hash = sha256(normalized_refresh_token.encode("utf-8")).hexdigest()
        with self._lock:
            if refresh_token_hash in self._consumed_refresh_token_hashes:
                raise SessionIssuanceError(
                    status_code=409,
                    error_code="refresh_token_reused",
                    message="Refresh token has already been consumed.",
                    reason="refresh_token_reused",
                )

            session_id = self._active_refresh_hash_to_session_id.get(refresh_token_hash)
            if session_id is None:
                raise SessionIssuanceError(
                    status_code=401,
                    error_code="refresh_token_invalid",
                    message="Refresh token is invalid.",
                    reason="refresh_token_invalid",
                )

            record = self._sessions_by_id.get(session_id)
            if record is None:
                self._active_refresh_hash_to_session_id.pop(refresh_token_hash, None)
                raise SessionIssuanceError(
                    status_code=401,
                    error_code="refresh_token_invalid",
                    message="Refresh token is invalid.",
                    reason="refresh_token_invalid",
                )

            now = self._now_provider()
            evaluation = self._evaluate_record_locked(record=record, now=now)
            if evaluation.status == "invalidated":
                raise SessionIssuanceError(
                    status_code=401,
                    error_code="refresh_token_session_revoked",
                    message="Refresh token belongs to a revoked session.",
                    reason="refresh_token_session_revoked",
                )
            if evaluation.status == "expired":
                raise SessionIssuanceError(
                    status_code=401,
                    error_code="refresh_token_expired",
                    message="Refresh token has expired.",
                    reason="refresh_token_expired",
                )

            if record.refresh_token_hash != refresh_token_hash:
                raise SessionIssuanceError(
                    status_code=401,
                    error_code="refresh_token_invalid",
                    message="Refresh token is invalid.",
                    reason="refresh_token_invalid",
                )

            rotated_record = self._apply_activity_locked(record=record, touched_at=now)
            access_token = _build_opaque_token(
                token_kind="access",
                user_id=record.user_id,
                session_id=record.session_id,
                issued_at=now,
            )
            rotated_refresh_token = _build_opaque_token(
                token_kind="refresh",
                user_id=record.user_id,
                session_id=record.session_id,
                issued_at=now,
            )
            updated_record = IssuedSessionRecord(
                session_id=rotated_record.session_id,
                user_id=rotated_record.user_id,
                tenant_id=rotated_record.tenant_id,
                role=rotated_record.role,
                issued_at=rotated_record.issued_at,
                expires_at=rotated_record.expires_at,
                inactivity_expires_at=rotated_record.inactivity_expires_at,
                last_activity_at=rotated_record.last_activity_at,
                is_invalidated=rotated_record.is_invalidated,
                invalidated_at=rotated_record.invalidated_at,
                invalidated_reason=rotated_record.invalidated_reason,
                device_fingerprint=rotated_record.device_fingerprint,
                access_token_hash=sha256(access_token.encode("utf-8")).hexdigest(),
                refresh_token_hash=sha256(rotated_refresh_token.encode("utf-8")).hexdigest(),
            )
            self._sessions_by_id[session_id] = updated_record
            self._active_refresh_hash_to_session_id.pop(refresh_token_hash, None)
            self._consumed_refresh_token_hashes.add(refresh_token_hash)
            self._active_refresh_hash_to_session_id[updated_record.refresh_token_hash] = session_id
            return SessionIssuanceResult(
                session_id=session_id,
                access_token=access_token,
                refresh_token=rotated_refresh_token,
                expires_at=updated_record.expires_at,
                evicted_session_ids=(),
            )

    def revoke_session(self, *, user_id: UUID, session_id: UUID) -> int:
        """Revoke one owned session deterministically and return revoked count."""

        with self._lock:
            record = self._sessions_by_id.get(session_id)
            if record is None or record.user_id != user_id:
                raise SessionIssuanceError(
                    status_code=404,
                    error_code="logout_session_not_found_or_not_owned",
                    message="Logout target session is not found or not owned by principal.",
                    reason="logout_session_not_found_or_not_owned",
                )
            now = self._now_provider()
            evaluation = self._evaluate_record_locked(record=record, now=now)
            if evaluation.status in {"expired", "invalidated"}:
                return 0
            updated_record = self._invalidate_record_locked(
                record=record,
                invalidated_at=now,
                invalidated_reason="session_revoked",
            )
            self._sessions_by_id[session_id] = updated_record
            return 1

    def revoke_all_sessions_for_user(self, *, user_id: UUID) -> int:
        """Revoke all active/warning sessions for one user and return revoked count."""

        with self._lock:
            session_ids = self._sessions_by_user.get(user_id, [])
            if not session_ids:
                return 0
            now = self._now_provider()
            revoked_session_count = 0
            for session_id in session_ids:
                record = self._sessions_by_id.get(session_id)
                if record is None:
                    continue
                evaluation = self._evaluate_record_locked(record=record, now=now)
                if evaluation.status not in {"active", "warning"}:
                    continue
                updated_record = self._invalidate_record_locked(
                    record=record,
                    invalidated_at=now,
                    invalidated_reason="session_revoked",
                )
                self._sessions_by_id[session_id] = updated_record
                revoked_session_count += 1
            return revoked_session_count

    def get_session(self, *, session_id: UUID) -> IssuedSessionRecord | None:
        """Return one issued session by identifier when present."""

        with self._lock:
            return self._sessions_by_id.get(session_id)

    def get_sessions_for_user(self, *, user_id: UUID) -> list[IssuedSessionRecord]:
        """Return deterministic ordered issued sessions for one user."""

        with self._lock:
            session_ids = self._sessions_by_user.get(user_id, [])
            return [
                session
                for session_id in session_ids
                if (session := self._sessions_by_id.get(session_id)) is not None
            ]

    def evaluate_session(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        """Return deterministic session-policy evaluation for one session."""

        with self._lock:
            record = self._sessions_by_id.get(session_id)
            if record is None:
                return None
            return self._evaluate_record_locked(record=record, now=self._now_provider())

    def touch_session_activity(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        """Update last activity and inactivity expiry where allowed."""

        with self._lock:
            record = self._sessions_by_id.get(session_id)
            if record is None:
                return None

            now = self._now_provider()
            evaluation = self._evaluate_record_locked(record=record, now=now)
            if evaluation.status in {"expired", "invalidated"}:
                return evaluation

            updated = self._apply_activity_locked(record=record, touched_at=now)
            self._sessions_by_id[session_id] = updated
            return self._evaluate_record_locked(record=updated, now=now)

    def extend_session(self, *, session_id: UUID) -> SessionPolicyEvaluation:
        """Extend session inactivity expiry only inside warning window."""

        with self._lock:
            record = self._sessions_by_id.get(session_id)
            if record is None:
                raise SessionIssuanceError(
                    status_code=404,
                    error_code="session_extension_not_allowed",
                    message="Session extension is not allowed for current session state.",
                    reason="session_extension_not_allowed",
                )

            now = self._now_provider()
            evaluation = self._evaluate_record_locked(record=record, now=now)
            if evaluation.status != "warning" or not evaluation.extension_allowed:
                raise SessionIssuanceError(
                    status_code=409,
                    error_code="session_extension_not_allowed",
                    message="Session extension is not allowed for current session state.",
                    reason="session_extension_not_allowed",
                    details={
                        "current_status": evaluation.status,
                        "reason_code": evaluation.reason_code,
                    },
                )

            updated = self._apply_activity_locked(record=record, touched_at=now)
            self._sessions_by_id[session_id] = updated
            return self._evaluate_record_locked(record=updated, now=now)

    def get_total_session_count(self) -> int:
        """Return total issued-session count for deterministic tests."""

        with self._lock:
            return len(self._sessions_by_id)

    def _apply_activity_locked(
        self,
        *,
        record: IssuedSessionRecord,
        touched_at: datetime,
    ) -> IssuedSessionRecord:
        absolute_expires_at = _parse_utc(record.issued_at) + timedelta(
            seconds=self._policy.absolute_lifetime_seconds
        )
        inactivity_expires_at = touched_at + timedelta(
            seconds=self._policy.inactivity_timeout_seconds
        )
        effective_expires_at = min(absolute_expires_at, inactivity_expires_at)
        return IssuedSessionRecord(
            session_id=record.session_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            role=record.role,
            issued_at=record.issued_at,
            expires_at=_utc_iso(effective_expires_at),
            inactivity_expires_at=_utc_iso(inactivity_expires_at),
            last_activity_at=_utc_iso(touched_at),
            is_invalidated=record.is_invalidated,
            invalidated_at=record.invalidated_at,
            invalidated_reason=record.invalidated_reason,
            device_fingerprint=record.device_fingerprint,
            access_token_hash=record.access_token_hash,
            refresh_token_hash=record.refresh_token_hash,
        )

    def _evaluate_record_locked(
        self,
        *,
        record: IssuedSessionRecord,
        now: datetime,
    ) -> SessionPolicyEvaluation:
        issued_at = _parse_utc(record.issued_at)
        inactivity_expires_at = _parse_utc(record.inactivity_expires_at)
        absolute_expires_at = issued_at + timedelta(seconds=self._policy.absolute_lifetime_seconds)
        effective_expires_at = min(inactivity_expires_at, absolute_expires_at)
        warning_window_started_at = max(
            issued_at,
            effective_expires_at - timedelta(seconds=self._policy.warning_window_seconds),
        )
        status: SessionStatus
        reason_code: str | None

        if record.is_invalidated:
            status = "invalidated"
            reason_code = record.invalidated_reason or "session_revoked"
        elif now >= absolute_expires_at:
            status = "expired"
            reason_code = "session_absolute_expiry"
        elif now >= inactivity_expires_at:
            status = "expired"
            reason_code = "session_inactivity_timeout"
        elif now >= warning_window_started_at:
            status = "warning"
            reason_code = None
        else:
            status = "active"
            reason_code = None

        extension_allowed = (
            status == "warning"
            and now < absolute_expires_at
            and inactivity_expires_at < absolute_expires_at
        )
        return SessionPolicyEvaluation(
            session_id=record.session_id,
            status=status,
            reason_code=reason_code,
            issued_at=record.issued_at,
            expires_at=_utc_iso(effective_expires_at),
            last_activity_at=record.last_activity_at,
            inactivity_expires_at=_utc_iso(inactivity_expires_at),
            absolute_expires_at=_utc_iso(absolute_expires_at),
            warning_window_started_at=_utc_iso(warning_window_started_at),
            is_warning_window=status == "warning",
            extension_allowed=extension_allowed,
            is_invalidated=record.is_invalidated,
            invalidated_at=record.invalidated_at,
            invalidated_reason=record.invalidated_reason,
        )

    def _enforce_concurrency_limit_locked(
        self,
        *,
        user_id: UUID,
        invalidated_at: datetime,
    ) -> tuple[UUID, ...]:
        session_ids = self._sessions_by_user.get(user_id, [])
        if not session_ids:
            return ()

        active_ids: list[UUID] = []
        for existing_session_id in session_ids:
            record = self._sessions_by_id.get(existing_session_id)
            if record is None:
                continue
            evaluation = self._evaluate_record_locked(record=record, now=invalidated_at)
            if evaluation.status in {"active", "warning"}:
                active_ids.append(existing_session_id)

        excess_count = (len(active_ids) + 1) - self._policy.max_concurrent_sessions
        if excess_count <= 0:
            return ()

        evicted_ids: list[UUID] = []
        for session_id_to_evict in active_ids[:excess_count]:
            record = self._sessions_by_id[session_id_to_evict]
            updated = self._invalidate_record_locked(
                record=record,
                invalidated_at=invalidated_at,
                invalidated_reason="session_concurrency_limit_enforced",
            )
            self._sessions_by_id[session_id_to_evict] = updated
            evicted_ids.append(session_id_to_evict)
        return tuple(evicted_ids)

    def _invalidate_record_locked(
        self,
        *,
        record: IssuedSessionRecord,
        invalidated_at: datetime,
        invalidated_reason: InvalidationReason,
    ) -> IssuedSessionRecord:
        return IssuedSessionRecord(
            session_id=record.session_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            role=record.role,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            inactivity_expires_at=record.inactivity_expires_at,
            last_activity_at=record.last_activity_at,
            is_invalidated=True,
            invalidated_at=_utc_iso(invalidated_at),
            invalidated_reason=invalidated_reason,
            device_fingerprint=record.device_fingerprint,
            access_token_hash=record.access_token_hash,
            refresh_token_hash=record.refresh_token_hash,
        )


class UnavailableSessionIssuanceStore:
    """Fail closed when production session persistence is unavailable."""

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

    def issue_session(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        role: str,
        device_fingerprint: str | None,
    ) -> SessionIssuanceResult:
        del user_id, tenant_id, role, device_fingerprint
        raise self._error()

    def get_session(self, *, session_id: UUID) -> IssuedSessionRecord | None:
        del session_id
        raise self._error()

    def get_sessions_for_user(self, *, user_id: UUID) -> list[IssuedSessionRecord]:
        del user_id
        raise self._error()

    def evaluate_session(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        del session_id
        raise self._error()

    def touch_session_activity(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        del session_id
        raise self._error()

    def extend_session(self, *, session_id: UUID) -> SessionPolicyEvaluation:
        del session_id
        raise self._error()

    def get_total_session_count(self) -> int:
        raise self._error()

    def refresh_session(self, *, refresh_token: str) -> SessionIssuanceResult:
        del refresh_token
        raise self._error()

    def revoke_session(self, *, user_id: UUID, session_id: UUID) -> int:
        del user_id, session_id
        raise self._error()

    def revoke_all_sessions_for_user(self, *, user_id: UUID) -> int:
        del user_id
        raise self._error()

    def _error(self) -> SessionIssuanceError:
        return SessionIssuanceError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )


class PersistentSessionIssuanceStore:
    """Persist auth session issuance and refresh rotation state in PostgreSQL."""

    def __init__(
        self,
        *,
        database_url: str,
        now_provider: Callable[[], datetime] | None = None,
        inactivity_timeout_seconds: int | None = None,
        absolute_lifetime_seconds: int | None = None,
        warning_window_seconds: int | None = None,
        max_concurrent_sessions: int | None = None,
    ) -> None:
        self._database_url = database_url
        self._now_provider = now_provider or _utc_now
        self._policy = _resolve_policy_config(
            inactivity_timeout_seconds=inactivity_timeout_seconds,
            absolute_lifetime_seconds=absolute_lifetime_seconds,
            warning_window_seconds=warning_window_seconds,
            max_concurrent_sessions=max_concurrent_sessions,
        )

    def issue_session(
        self,
        *,
        user_id: UUID,
        tenant_id: str,
        role: str,
        device_fingerprint: str | None,
    ) -> SessionIssuanceResult:
        now = self._now_provider()
        absolute_expires_at_value = now + timedelta(seconds=self._policy.absolute_lifetime_seconds)
        inactivity_expires_at_value = now + timedelta(
            seconds=self._policy.inactivity_timeout_seconds
        )
        expires_at_value = min(absolute_expires_at_value, inactivity_expires_at_value)
        session_id = uuid4()
        access_token = _build_opaque_token(
            token_kind="access",
            user_id=user_id,
            session_id=session_id,
            issued_at=now,
        )
        refresh_token = _build_opaque_token(
            token_kind="refresh",
            user_id=user_id,
            session_id=session_id,
            issued_at=now,
        )
        record = IssuedSessionRecord(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            issued_at=_utc_iso(now),
            expires_at=_utc_iso(expires_at_value),
            inactivity_expires_at=_utc_iso(inactivity_expires_at_value),
            last_activity_at=_utc_iso(now),
            is_invalidated=False,
            invalidated_at=None,
            invalidated_reason=None,
            device_fingerprint=device_fingerprint,
            access_token_hash=sha256(access_token.encode("utf-8")).hexdigest(),
            refresh_token_hash=sha256(refresh_token.encode("utf-8")).hexdigest(),
        )

        def _transaction(connection: psycopg.Connection[object]) -> SessionIssuanceResult:
            with connection.cursor() as cursor:
                evicted_session_ids = self._enforce_concurrency_limit_locked(
                    cursor=cursor,
                    user_id=user_id,
                    invalidated_at=now,
                )
                cursor.execute(
                    """
                    INSERT INTO sessions (
                        id,
                        user_id,
                        idempotency_key,
                        issued_at,
                        expires_at,
                        inactivity_expires_at,
                        last_activity_at,
                        tenant_id,
                        role,
                        device_fingerprint_hash,
                        is_invalidated,
                        invalidated_at,
                        invalidated_reason,
                        access_token_hash,
                        refresh_token_hash,
                        created_at
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
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        LEAST(%s, now())
                    )
                    """,
                    (
                        record.session_id,
                        record.user_id,
                        f"session:{record.session_id}",
                        _parse_utc(record.issued_at),
                        _parse_utc(record.expires_at),
                        _parse_utc(record.inactivity_expires_at),
                        _parse_utc(record.last_activity_at),
                        record.tenant_id,
                        record.role,
                        record.device_fingerprint,
                        record.is_invalidated,
                        None,
                        None,
                        record.access_token_hash,
                        record.refresh_token_hash,
                        _parse_utc(record.issued_at),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO auth_session_refresh_tokens (
                        refresh_token_hash,
                        session_id,
                        issued_at,
                        is_consumed,
                        consumed_at
                    )
                    VALUES (%s, %s, %s, FALSE, NULL)
                    """,
                    (
                        record.refresh_token_hash,
                        record.session_id,
                        _parse_utc(record.issued_at),
                    ),
                )
            return SessionIssuanceResult(
                session_id=record.session_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=record.expires_at,
                evicted_session_ids=evicted_session_ids,
            )

        def _reconcile() -> SessionIssuanceResult | None:
            persisted_record = self.get_session(session_id=record.session_id)
            if persisted_record is None:
                return None
            if not self._session_record_matches_issued_record(
                persisted_record=persisted_record,
                expected_record=record,
            ):
                return None
            evicted_session_ids = tuple(
                session.session_id
                for session in self.get_sessions_for_user(user_id=user_id)
                if session.invalidated_reason == "session_concurrency_limit_enforced"
                and session.invalidated_at == record.issued_at
            )
            return SessionIssuanceResult(
                session_id=record.session_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=record.expires_at,
                evicted_session_ids=evicted_session_ids,
            )

        return execute_auth_database_transaction(
            database_url=self._database_url,
            transaction_callback=_transaction,
            reconcile_callback=_reconcile,
        )

    def refresh_session(self, *, refresh_token: str) -> SessionIssuanceResult:
        normalized_refresh_token = refresh_token.strip()
        if _REFRESH_TOKEN_PATTERN.fullmatch(normalized_refresh_token) is None:
            raise SessionIssuanceError(
                status_code=401,
                error_code="refresh_token_malformed",
                message="Refresh token is malformed.",
                reason="refresh_token_malformed",
            )

        refresh_token_hash = sha256(normalized_refresh_token.encode("utf-8")).hexdigest()
        now = self._now_provider()
        access_token_nonce = uuid4()
        rotated_refresh_token_nonce = uuid4()
        record: IssuedSessionRecord | None = None
        updated_record: IssuedSessionRecord | None = None
        access_token: str | None = None
        rotated_refresh_token: str | None = None

        def _transaction(connection: psycopg.Connection[object]) -> SessionIssuanceResult:
            nonlocal record
            nonlocal access_token
            nonlocal rotated_refresh_token
            nonlocal updated_record
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE auth_session_refresh_tokens
                    SET is_consumed = TRUE,
                        consumed_at = %s
                    WHERE refresh_token_hash = %s
                      AND is_consumed = FALSE
                    RETURNING session_id
                    """,
                    (now, refresh_token_hash),
                )
                claimed = cursor.fetchone()
                if claimed is None:
                    refresh_record = self._get_refresh_token_record_locked(
                        cursor=cursor,
                        refresh_token_hash=refresh_token_hash,
                    )
                    if refresh_record is None:
                        raise SessionIssuanceError(
                            status_code=401,
                            error_code="refresh_token_invalid",
                            message="Refresh token is invalid.",
                            reason="refresh_token_invalid",
                        )
                    raise SessionIssuanceError(
                        status_code=409,
                        error_code="refresh_token_reused",
                        message="Refresh token has already been consumed.",
                        reason="refresh_token_reused",
                    )

                current_record = self._get_session_record_locked(
                    cursor=cursor,
                    session_id=UUID(str(claimed[0])),
                )
                if current_record is None:
                    raise SessionIssuanceError(
                        status_code=401,
                        error_code="refresh_token_invalid",
                        message="Refresh token is invalid.",
                        reason="refresh_token_invalid",
                    )
                transaction_evaluation = self._evaluate_record(
                    record=current_record,
                    now=now,
                )
                if transaction_evaluation.status == "invalidated":
                    raise SessionIssuanceError(
                        status_code=401,
                        error_code="refresh_token_session_revoked",
                        message="Refresh token belongs to a revoked session.",
                        reason="refresh_token_session_revoked",
                    )
                if transaction_evaluation.status == "expired":
                    raise SessionIssuanceError(
                        status_code=401,
                        error_code="refresh_token_expired",
                        message="Refresh token has expired.",
                        reason="refresh_token_expired",
                    )
                if current_record.refresh_token_hash != refresh_token_hash:
                    raise SessionIssuanceError(
                        status_code=401,
                        error_code="refresh_token_invalid",
                        message="Refresh token is invalid.",
                        reason="refresh_token_invalid",
                    )
                record = current_record

                access_token = _build_opaque_token(
                    token_kind="access",
                    user_id=current_record.user_id,
                    session_id=current_record.session_id,
                    issued_at=now,
                    nonce=access_token_nonce,
                )
                rotated_refresh_token = _build_opaque_token(
                    token_kind="refresh",
                    user_id=current_record.user_id,
                    session_id=current_record.session_id,
                    issued_at=now,
                    nonce=rotated_refresh_token_nonce,
                )
                rotated_record = self._apply_activity(record=current_record, touched_at=now)
                updated_record = IssuedSessionRecord(
                    session_id=rotated_record.session_id,
                    user_id=rotated_record.user_id,
                    tenant_id=rotated_record.tenant_id,
                    role=rotated_record.role,
                    issued_at=rotated_record.issued_at,
                    expires_at=rotated_record.expires_at,
                    inactivity_expires_at=rotated_record.inactivity_expires_at,
                    last_activity_at=rotated_record.last_activity_at,
                    is_invalidated=rotated_record.is_invalidated,
                    invalidated_at=rotated_record.invalidated_at,
                    invalidated_reason=rotated_record.invalidated_reason,
                    device_fingerprint=rotated_record.device_fingerprint,
                    access_token_hash=sha256(access_token.encode("utf-8")).hexdigest(),
                    refresh_token_hash=sha256(rotated_refresh_token.encode("utf-8")).hexdigest(),
                )

                self._write_session_record_locked(cursor=cursor, record=updated_record)
                cursor.execute(
                    """
                    INSERT INTO auth_session_refresh_tokens (
                        refresh_token_hash,
                        session_id,
                        issued_at,
                        is_consumed,
                        consumed_at
                    )
                    VALUES (%s, %s, %s, FALSE, NULL)
                    """,
                    (
                        updated_record.refresh_token_hash,
                        updated_record.session_id,
                        now,
                    ),
                )
            return SessionIssuanceResult(
                session_id=updated_record.session_id,
                access_token=access_token,
                refresh_token=rotated_refresh_token,
                expires_at=updated_record.expires_at,
                evicted_session_ids=(),
            )

        def _reconcile() -> SessionIssuanceResult | None:
            assert record is not None
            assert updated_record is not None
            assert access_token is not None
            assert rotated_refresh_token is not None
            session_record = self.get_session(session_id=record.session_id)
            if session_record is None:
                return None
            if not self._session_record_matches_expected_record(
                persisted_record=session_record,
                expected_record=updated_record,
            ):
                return None
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    old_refresh_record = self._get_refresh_token_record_locked(
                        cursor=cursor,
                        refresh_token_hash=refresh_token_hash,
                    )
                    new_refresh_record = self._get_refresh_token_record_locked(
                        cursor=cursor,
                        refresh_token_hash=updated_record.refresh_token_hash,
                    )
            if old_refresh_record is None or new_refresh_record is None:
                return None
            if (
                not old_refresh_record.is_consumed
                or old_refresh_record.consumed_at != _utc_iso(now)
                or old_refresh_record.session_id != updated_record.session_id
            ):
                return None
            if (
                new_refresh_record.is_consumed
                or new_refresh_record.consumed_at is not None
                or new_refresh_record.session_id != updated_record.session_id
            ):
                return None
            return SessionIssuanceResult(
                session_id=updated_record.session_id,
                access_token=access_token,
                refresh_token=rotated_refresh_token,
                expires_at=updated_record.expires_at,
                evicted_session_ids=(),
            )

        try:
            return execute_auth_database_transaction(
                database_url=self._database_url,
                transaction_callback=_transaction,
                reconcile_callback=_reconcile,
            )
        except SessionIssuanceError:
            raise
        except psycopg.Error as error:
            raise _session_persistence_unavailable() from error

    def revoke_session(self, *, user_id: UUID, session_id: UUID) -> int:
        now = self._now_provider()

        def _transaction(connection: psycopg.Connection[object]) -> int:
            with connection.cursor() as cursor:
                record = self._get_session_record_locked(cursor=cursor, session_id=session_id)
                if record is None or record.user_id != user_id:
                    raise SessionIssuanceError(
                        status_code=404,
                        error_code="logout_session_not_found_or_not_owned",
                        message="Logout target session is not found or not owned by principal.",
                        reason="logout_session_not_found_or_not_owned",
                    )
                evaluation = self._evaluate_record(record=record, now=now)
                if evaluation.status in {"expired", "invalidated"}:
                    return 0
                updated = self._invalidate_record(
                    record=record,
                    invalidated_at=now,
                    invalidated_reason="session_revoked",
                )
                self._write_session_record_locked(cursor=cursor, record=updated)
            return 1

        def _reconcile() -> int | None:
            record = self.get_session(session_id=session_id)
            if record is None or record.user_id != user_id:
                return None
            if (
                not record.is_invalidated
                or record.invalidated_at != _utc_iso(now)
                or record.invalidated_reason != "session_revoked"
            ):
                return None
            return 1

        return execute_auth_database_transaction(
            database_url=self._database_url,
            transaction_callback=_transaction,
            reconcile_callback=_reconcile,
        )

    def revoke_all_sessions_for_user(self, *, user_id: UUID) -> int:
        now = self._now_provider()

        def _transaction(connection: psycopg.Connection[object]) -> int:
            revoked_session_count = 0
            with connection.cursor() as cursor:
                records = self._get_session_records_for_user_locked(
                    cursor=cursor, user_id=user_id
                )
                for record in records:
                    evaluation = self._evaluate_record(record=record, now=now)
                    if evaluation.status not in {"active", "warning"}:
                        continue
                    updated = self._invalidate_record(
                        record=record,
                        invalidated_at=now,
                        invalidated_reason="session_revoked",
                    )
                    self._write_session_record_locked(cursor=cursor, record=updated)
                    revoked_session_count += 1
            return revoked_session_count

        def _reconcile() -> int | None:
            records = self.get_sessions_for_user(user_id=user_id)
            return sum(
                1
                for record in records
                if record.is_invalidated
                and record.invalidated_at == _utc_iso(now)
                and record.invalidated_reason == "session_revoked"
            )

        return execute_auth_database_transaction(
            database_url=self._database_url,
            transaction_callback=_transaction,
            reconcile_callback=_reconcile,
        )

    def get_session(self, *, session_id: UUID) -> IssuedSessionRecord | None:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    return self._get_session_record_locked(cursor=cursor, session_id=session_id)
        except psycopg.Error as error:
            raise _session_persistence_unavailable() from error

    def get_sessions_for_user(self, *, user_id: UUID) -> list[IssuedSessionRecord]:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    return list(
                        self._get_session_records_for_user_locked(cursor=cursor, user_id=user_id)
                    )
        except psycopg.Error as error:
            raise _session_persistence_unavailable() from error

    def evaluate_session(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        record = self.get_session(session_id=session_id)
        if record is None:
            return None
        return self._evaluate_record(record=record, now=self._now_provider())

    def touch_session_activity(self, *, session_id: UUID) -> SessionPolicyEvaluation | None:
        now = self._now_provider()

        def _transaction(connection: psycopg.Connection[object]) -> SessionPolicyEvaluation | None:
            with connection.cursor() as cursor:
                record = self._get_session_record_locked(cursor=cursor, session_id=session_id)
                if record is None:
                    return None
                evaluation = self._evaluate_record(record=record, now=now)
                if evaluation.status in {"expired", "invalidated"}:
                    return evaluation
                updated = self._apply_activity(record=record, touched_at=now)
                self._write_session_record_locked(cursor=cursor, record=updated)
            return self._evaluate_record(record=updated, now=now)

        def _reconcile() -> SessionPolicyEvaluation | None:
            record = self.get_session(session_id=session_id)
            if record is None:
                return None
            if not self._session_record_matches_activity_update(
                persisted_record=record,
                touched_at=now,
            ):
                return None
            return self._evaluate_record(record=record, now=now)

        return execute_auth_database_transaction(
            database_url=self._database_url,
            transaction_callback=_transaction,
            reconcile_callback=_reconcile,
        )

    def extend_session(self, *, session_id: UUID) -> SessionPolicyEvaluation:
        now = self._now_provider()

        def _transaction(connection: psycopg.Connection[object]) -> SessionPolicyEvaluation:
            with connection.cursor() as cursor:
                record = self._get_session_record_locked(cursor=cursor, session_id=session_id)
                if record is None:
                    raise SessionIssuanceError(
                        status_code=404,
                        error_code="session_extension_not_allowed",
                        message="Session extension is not allowed for current session state.",
                        reason="session_extension_not_allowed",
                    )
                evaluation = self._evaluate_record(record=record, now=now)
                if evaluation.status != "warning" or not evaluation.extension_allowed:
                    raise SessionIssuanceError(
                        status_code=409,
                        error_code="session_extension_not_allowed",
                        message="Session extension is not allowed for current session state.",
                        reason="session_extension_not_allowed",
                        details={
                            "current_status": evaluation.status,
                            "reason_code": evaluation.reason_code,
                        },
                    )
                updated = self._apply_activity(record=record, touched_at=now)
                self._write_session_record_locked(cursor=cursor, record=updated)
            return self._evaluate_record(record=updated, now=now)

        def _reconcile() -> SessionPolicyEvaluation | None:
            record = self.get_session(session_id=session_id)
            if record is None:
                return None
            if not self._session_record_matches_activity_update(
                persisted_record=record,
                touched_at=now,
            ):
                return None
            return self._evaluate_record(record=record, now=now)

        return execute_auth_database_transaction(
            database_url=self._database_url,
            transaction_callback=_transaction,
            reconcile_callback=_reconcile,
        )

    def get_total_session_count(self) -> int:
        try:
            with connect_auth_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM sessions")
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise _session_persistence_unavailable() from error
        assert row is not None
        return int(row[0])

    def _get_session_record_locked(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        session_id: UUID,
    ) -> IssuedSessionRecord | None:
        cursor.execute(
            """
            SELECT
                id,
                user_id,
                tenant_id,
                role,
                issued_at,
                expires_at,
                inactivity_expires_at,
                last_activity_at,
                is_invalidated,
                invalidated_at,
                invalidated_reason,
                device_fingerprint_hash,
                access_token_hash,
                refresh_token_hash
            FROM sessions
            WHERE id = %s
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_session_record(row=row)

    def _get_session_records_for_user_locked(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
    ) -> tuple[IssuedSessionRecord, ...]:
        cursor.execute(
            """
            SELECT
                id,
                user_id,
                tenant_id,
                role,
                issued_at,
                expires_at,
                inactivity_expires_at,
                last_activity_at,
                is_invalidated,
                invalidated_at,
                invalidated_reason,
                device_fingerprint_hash,
                access_token_hash,
                refresh_token_hash
            FROM sessions
            WHERE user_id = %s
            ORDER BY issued_at ASC, id ASC
            """,
            (user_id,),
        )
        return tuple(_row_to_session_record(row=row) for row in cursor.fetchall())

    def _get_refresh_token_record_locked(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        refresh_token_hash: str,
    ) -> RefreshTokenRecord | None:
        cursor.execute(
            """
            SELECT
                refresh_token_hash,
                session_id,
                issued_at,
                is_consumed,
                consumed_at
            FROM auth_session_refresh_tokens
            WHERE refresh_token_hash = %s
            """,
            (refresh_token_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return RefreshTokenRecord(
            refresh_token_hash=str(row[0]),
            session_id=UUID(str(row[1])),
            issued_at=_utc_iso(row[2]) if isinstance(row[2], datetime) else str(row[2]),
            is_consumed=bool(row[3]),
            consumed_at=_utc_iso(row[4]) if isinstance(row[4], datetime) else None,
        )

    def _write_session_record_locked(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        record: IssuedSessionRecord,
    ) -> None:
        cursor.execute(
            """
            UPDATE sessions
            SET tenant_id = %s,
                role = %s,
                expires_at = %s,
                inactivity_expires_at = %s,
                last_activity_at = %s,
                is_invalidated = %s,
                invalidated_at = %s,
                invalidated_reason = %s,
                device_fingerprint_hash = %s,
                access_token_hash = %s,
                refresh_token_hash = %s
            WHERE id = %s
            """,
            (
                record.tenant_id,
                record.role,
                _parse_utc(record.expires_at),
                _parse_utc(record.inactivity_expires_at),
                _parse_utc(record.last_activity_at),
                record.is_invalidated,
                _parse_optional_utc(record.invalidated_at),
                record.invalidated_reason,
                record.device_fingerprint,
                record.access_token_hash,
                record.refresh_token_hash,
                record.session_id,
            ),
        )

    def _evaluate_record(
        self,
        *,
        record: IssuedSessionRecord,
        now: datetime,
    ) -> SessionPolicyEvaluation:
        issued_at = _parse_utc(record.issued_at)
        inactivity_expires_at = _parse_utc(record.inactivity_expires_at)
        absolute_expires_at = issued_at + timedelta(seconds=self._policy.absolute_lifetime_seconds)
        effective_expires_at = min(inactivity_expires_at, absolute_expires_at)
        warning_window_started_at = max(
            issued_at,
            effective_expires_at - timedelta(seconds=self._policy.warning_window_seconds),
        )

        if record.is_invalidated:
            status: SessionStatus = "invalidated"
            reason_code = record.invalidated_reason or "session_revoked"
        elif now >= absolute_expires_at:
            status = "expired"
            reason_code = "session_absolute_expiry"
        elif now >= inactivity_expires_at:
            status = "expired"
            reason_code = "session_inactivity_timeout"
        elif now >= warning_window_started_at:
            status = "warning"
            reason_code = None
        else:
            status = "active"
            reason_code = None

        extension_allowed = (
            status == "warning"
            and now < absolute_expires_at
            and inactivity_expires_at < absolute_expires_at
        )
        return SessionPolicyEvaluation(
            session_id=record.session_id,
            status=status,
            reason_code=reason_code,
            issued_at=record.issued_at,
            expires_at=_utc_iso(effective_expires_at),
            last_activity_at=record.last_activity_at,
            inactivity_expires_at=_utc_iso(inactivity_expires_at),
            absolute_expires_at=_utc_iso(absolute_expires_at),
            warning_window_started_at=_utc_iso(warning_window_started_at),
            is_warning_window=status == "warning",
            extension_allowed=extension_allowed,
            is_invalidated=record.is_invalidated,
            invalidated_at=record.invalidated_at,
            invalidated_reason=record.invalidated_reason,
        )

    def _apply_activity(
        self,
        *,
        record: IssuedSessionRecord,
        touched_at: datetime,
    ) -> IssuedSessionRecord:
        absolute_expires_at = _parse_utc(record.issued_at) + timedelta(
            seconds=self._policy.absolute_lifetime_seconds
        )
        inactivity_expires_at = touched_at + timedelta(
            seconds=self._policy.inactivity_timeout_seconds
        )
        effective_expires_at = min(absolute_expires_at, inactivity_expires_at)
        return IssuedSessionRecord(
            session_id=record.session_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            role=record.role,
            issued_at=record.issued_at,
            expires_at=_utc_iso(effective_expires_at),
            inactivity_expires_at=_utc_iso(inactivity_expires_at),
            last_activity_at=_utc_iso(touched_at),
            is_invalidated=record.is_invalidated,
            invalidated_at=record.invalidated_at,
            invalidated_reason=record.invalidated_reason,
            device_fingerprint=record.device_fingerprint,
            access_token_hash=record.access_token_hash,
            refresh_token_hash=record.refresh_token_hash,
        )

    def _invalidate_record(
        self,
        *,
        record: IssuedSessionRecord,
        invalidated_at: datetime,
        invalidated_reason: InvalidationReason,
    ) -> IssuedSessionRecord:
        return IssuedSessionRecord(
            session_id=record.session_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            role=record.role,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            inactivity_expires_at=record.inactivity_expires_at,
            last_activity_at=record.last_activity_at,
            is_invalidated=True,
            invalidated_at=_utc_iso(invalidated_at),
            invalidated_reason=invalidated_reason,
            device_fingerprint=record.device_fingerprint,
            access_token_hash=record.access_token_hash,
            refresh_token_hash=record.refresh_token_hash,
        )

    def _enforce_concurrency_limit_locked(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
        invalidated_at: datetime,
    ) -> tuple[UUID, ...]:
        records = self._get_session_records_for_user_locked(cursor=cursor, user_id=user_id)
        active_ids: list[UUID] = []
        for record in records:
            evaluation = self._evaluate_record(record=record, now=invalidated_at)
            if evaluation.status in {"active", "warning"}:
                active_ids.append(record.session_id)
        excess_count = (len(active_ids) + 1) - self._policy.max_concurrent_sessions
        if excess_count <= 0:
            return ()
        evicted_ids: list[UUID] = []
        for session_id_to_evict in active_ids[:excess_count]:
            record = next(item for item in records if item.session_id == session_id_to_evict)
            updated = self._invalidate_record(
                record=record,
                invalidated_at=invalidated_at,
                invalidated_reason="session_concurrency_limit_enforced",
            )
            self._write_session_record_locked(cursor=cursor, record=updated)
            evicted_ids.append(session_id_to_evict)
        return tuple(evicted_ids)

    def _session_record_matches_issued_record(
        self,
        *,
        persisted_record: IssuedSessionRecord,
        expected_record: IssuedSessionRecord,
    ) -> bool:
        return self._session_record_matches_expected_record(
            persisted_record=persisted_record,
            expected_record=expected_record,
        )

    def _session_record_matches_activity_update(
        self,
        *,
        persisted_record: IssuedSessionRecord,
        touched_at: datetime,
    ) -> bool:
        absolute_expires_at = _parse_utc(persisted_record.issued_at) + timedelta(
            seconds=self._policy.absolute_lifetime_seconds
        )
        inactivity_expires_at = touched_at + timedelta(
            seconds=self._policy.inactivity_timeout_seconds
        )
        expected_expires_at = min(absolute_expires_at, inactivity_expires_at)
        return (
            persisted_record.last_activity_at == _utc_iso(touched_at)
            and persisted_record.expires_at == _utc_iso(expected_expires_at)
            and persisted_record.inactivity_expires_at == _utc_iso(inactivity_expires_at)
            and not persisted_record.is_invalidated
        )

    def _session_record_matches_expected_record(
        self,
        *,
        persisted_record: IssuedSessionRecord,
        expected_record: IssuedSessionRecord,
    ) -> bool:
        return (
            persisted_record.session_id == expected_record.session_id
            and persisted_record.user_id == expected_record.user_id
            and persisted_record.tenant_id == expected_record.tenant_id
            and persisted_record.role == expected_record.role
            and persisted_record.issued_at == expected_record.issued_at
            and persisted_record.expires_at == expected_record.expires_at
            and persisted_record.inactivity_expires_at == expected_record.inactivity_expires_at
            and persisted_record.last_activity_at == expected_record.last_activity_at
            and persisted_record.is_invalidated == expected_record.is_invalidated
            and persisted_record.invalidated_at == expected_record.invalidated_at
            and persisted_record.invalidated_reason == expected_record.invalidated_reason
            and persisted_record.device_fingerprint == expected_record.device_fingerprint
            and persisted_record.access_token_hash == expected_record.access_token_hash
            and persisted_record.refresh_token_hash == expected_record.refresh_token_hash
        )


def _resolve_policy_config(
    *,
    inactivity_timeout_seconds: int | None,
    absolute_lifetime_seconds: int | None,
    warning_window_seconds: int | None,
    max_concurrent_sessions: int | None,
) -> _PolicyConfig:
    resolved_inactivity_timeout = (
        inactivity_timeout_seconds
        if inactivity_timeout_seconds is not None and inactivity_timeout_seconds > 0
        else get_auth_session_inactivity_timeout_seconds()
    )
    resolved_absolute_lifetime = (
        absolute_lifetime_seconds
        if absolute_lifetime_seconds is not None and absolute_lifetime_seconds > 0
        else get_auth_session_absolute_lifetime_seconds()
    )
    resolved_warning_window = (
        warning_window_seconds
        if warning_window_seconds is not None and warning_window_seconds > 0
        else get_auth_session_warning_window_seconds()
    )
    resolved_warning_window = min(resolved_warning_window, resolved_inactivity_timeout)
    resolved_max_concurrent = (
        max_concurrent_sessions
        if max_concurrent_sessions is not None and max_concurrent_sessions > 0
        else get_auth_session_max_concurrent_sessions()
    )
    return _PolicyConfig(
        inactivity_timeout_seconds=resolved_inactivity_timeout,
        absolute_lifetime_seconds=resolved_absolute_lifetime,
        warning_window_seconds=resolved_warning_window,
        max_concurrent_sessions=resolved_max_concurrent,
    )


def _build_opaque_token(
    *,
    token_kind: str,
    user_id: UUID,
    session_id: UUID,
    issued_at: datetime,
    nonce: UUID | None = None,
) -> str:
    token_seed = (
        f"{_TOKEN_NAMESPACE}:{token_kind}:{user_id}:{session_id}:{issued_at.isoformat()}:{nonce or uuid4()}"
    )
    digest = sha256(token_seed.encode("utf-8")).hexdigest()
    return f"{token_kind}_{digest}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _parse_optional_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_utc(value)


def _row_to_session_record(*, row: tuple[object, ...]) -> IssuedSessionRecord:
    invalidated_reason = row[10]
    issued_at = row[4]
    expires_at = row[5]
    inactivity_expires_at = row[6]
    last_activity_at = row[7]
    assert isinstance(issued_at, datetime)
    assert isinstance(expires_at, datetime)
    assert isinstance(inactivity_expires_at, datetime)
    assert isinstance(last_activity_at, datetime)
    return IssuedSessionRecord(
        session_id=UUID(str(row[0])),
        user_id=UUID(str(row[1])),
        tenant_id=str(row[2]),
        role=str(row[3]),
        issued_at=_utc_iso(issued_at),
        expires_at=_utc_iso(expires_at),
        inactivity_expires_at=_utc_iso(inactivity_expires_at),
        last_activity_at=_utc_iso(last_activity_at),
        is_invalidated=bool(row[8]),
        invalidated_at=_utc_iso(row[9]) if isinstance(row[9], datetime) else None,
        invalidated_reason=(
            cast(InvalidationReason, str(invalidated_reason))
            if isinstance(invalidated_reason, str) and invalidated_reason
            else None
        ),
        device_fingerprint=str(row[11]) if isinstance(row[11], str) else None,
        access_token_hash=str(row[12] or ""),
        refresh_token_hash=str(row[13] or ""),
    )


def _session_persistence_unavailable() -> SessionIssuanceError:
    return SessionIssuanceError(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_SESSION_PERSISTENCE_SCHEMA: dict[str, tuple[str, ...]] = {
    "sessions": (
        "id",
        "user_id",
        "tenant_id",
        "role",
        "issued_at",
        "expires_at",
        "inactivity_expires_at",
        "last_activity_at",
        "is_invalidated",
        "invalidated_at",
        "invalidated_reason",
        "device_fingerprint_hash",
        "access_token_hash",
        "refresh_token_hash",
    ),
    "auth_session_refresh_tokens": (
        "refresh_token_hash",
        "session_id",
        "issued_at",
        "is_consumed",
        "consumed_at",
    ),
}


def build_default_session_issuance_store() -> SessionIssuanceStoreProtocol:
    """Build the auth session store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemorySessionIssuanceStore()

    database_url = load_auth_database_url()
    if not database_url:
        return UnavailableSessionIssuanceStore(
            status_code=503,
            error_code="auth_persistence_unavailable",
            message="Auth persistence is unavailable.",
            reason="auth_persistence_unavailable",
        )

    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return PersistentSessionIssuanceStore(database_url=database_url)
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return UnavailableSessionIssuanceStore(
            status_code=500,
            error_code="auth_persistence_schema_mismatch",
            message="Auth persistence schema is not aligned with runtime requirements.",
            reason="auth_persistence_schema_mismatch",
        )
    return UnavailableSessionIssuanceStore(
        status_code=503,
        error_code="auth_persistence_unavailable",
        message="Auth persistence is unavailable.",
        reason="auth_persistence_unavailable",
    )


_default_session_issuance_store: SessionIssuanceStoreProtocol = (
    build_default_session_issuance_store()
)


def get_default_session_issuance_store() -> SessionIssuanceStoreProtocol:
    """Return deterministic process-local session issuance store."""

    return _default_session_issuance_store


def reset_default_session_issuance_store() -> None:
    """Reset process-local session issuance store for isolated tests."""

    global _default_session_issuance_store
    _default_session_issuance_store = build_default_session_issuance_store()
