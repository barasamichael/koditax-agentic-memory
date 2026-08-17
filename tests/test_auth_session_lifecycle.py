"""Focused deterministic tests for auth session lifecycle semantics."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from contextlib import contextmanager
from collections.abc import Callable
from collections.abc import Iterator

import pytest

from services.auth.app.session_issuance import RefreshTokenRecord
from services.auth.app.session_issuance import IssuedSessionRecord
from services.auth.app.session_issuance import SessionIssuanceError
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.session_issuance import PersistentSessionIssuanceStore


class _FrozenClock:
    """Provide deterministic time controls for session lifecycle checks."""

    def __init__(self) -> None:
        self._current = datetime(2026, 4, 11, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def test_idle_timeout_expires_session_deterministically() -> None:
    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=60,
        absolute_lifetime_seconds=600,
        warning_window_seconds=10,
        max_concurrent_sessions=3,
    )
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )

    clock.advance(seconds=61)
    first_eval = store.evaluate_session(session_id=issued.session_id)
    second_eval = store.evaluate_session(session_id=issued.session_id)
    assert first_eval is not None
    assert second_eval is not None
    assert first_eval.status == "expired"
    assert first_eval.reason_code == "session_inactivity_timeout"
    assert second_eval.status == "expired"
    assert second_eval.reason_code == "session_inactivity_timeout"


def test_absolute_timeout_dominates_extension_attempts() -> None:
    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=300,
        absolute_lifetime_seconds=120,
        warning_window_seconds=30,
        max_concurrent_sessions=3,
    )
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )

    clock.advance(seconds=95)
    warning_eval = store.evaluate_session(session_id=issued.session_id)
    assert warning_eval is not None
    assert warning_eval.status == "warning"
    assert warning_eval.extension_allowed is False

    with pytest.raises(SessionIssuanceError) as extension_error:
        store.extend_session(session_id=issued.session_id)
    assert extension_error.value.error_code == "session_extension_not_allowed"
    assert extension_error.value.reason == "session_extension_not_allowed"

    clock.advance(seconds=30)
    expired_eval = store.evaluate_session(session_id=issued.session_id)
    assert expired_eval is not None
    assert expired_eval.status == "expired"
    assert expired_eval.reason_code == "session_absolute_expiry"


def test_refresh_path_rejects_expired_session_deterministically() -> None:
    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=60,
        absolute_lifetime_seconds=300,
        warning_window_seconds=10,
        max_concurrent_sessions=3,
    )
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-a",
    )

    clock.advance(seconds=61)
    with pytest.raises(SessionIssuanceError) as first_error:
        store.refresh_session(refresh_token=issued.refresh_token)
    with pytest.raises(SessionIssuanceError) as second_error:
        store.refresh_session(refresh_token=issued.refresh_token)
    assert first_error.value.error_code == "refresh_token_expired"
    assert first_error.value.reason == "refresh_token_expired"
    assert second_error.value.error_code == "refresh_token_expired"
    assert second_error.value.reason == "refresh_token_expired"


def test_persistent_refresh_tokens_are_generated_once_across_replayed_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FrozenClock()
    store = PersistentSessionIssuanceStore(
        database_url="postgresql://example.invalid/kodi_dev",
        now_provider=clock.now,
        inactivity_timeout_seconds=300,
        absolute_lifetime_seconds=600,
        warning_window_seconds=60,
        max_concurrent_sessions=3,
    )
    user_id = uuid4()
    session_id = uuid4()
    issued_at = clock.now().isoformat().replace("+00:00", "Z")
    refresh_token = "refresh_" + "a" * 64
    refresh_token_hash = sha256(refresh_token.encode("utf-8")).hexdigest()
    record = IssuedSessionRecord(
        session_id=session_id,
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        issued_at=issued_at,
        expires_at=(clock.now() + timedelta(seconds=300)).isoformat().replace(
            "+00:00", "Z"
        ),
        inactivity_expires_at=(clock.now() + timedelta(seconds=300)).isoformat().replace(
            "+00:00", "Z"
        ),
        last_activity_at=issued_at,
        is_invalidated=False,
        invalidated_at=None,
        invalidated_reason=None,
        device_fingerprint="device-a",
        access_token_hash="access-token-hash",
        refresh_token_hash=refresh_token_hash,
    )
    refresh_record = RefreshTokenRecord(
        refresh_token_hash=refresh_token_hash,
        session_id=session_id,
        issued_at=issued_at,
        is_consumed=False,
        consumed_at=None,
    )
    generated_tokens: list[str] = []

    class _FakeCursor:
        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(
            self,
            exc_type: object | None,
            exc: object | None,
            tb: object | None,
        ) -> bool:
            return False

        def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
            del sql, params

        def fetchone(self) -> tuple[object, ...] | None:
            return None

    class _FakeConnection:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    @contextmanager
    def _fake_connection(_database_url: str) -> Iterator[_FakeConnection]:
        yield _FakeConnection()

    def _fake_build_opaque_token(
        *,
        token_kind: str,
        user_id: object,
        session_id: object,
        issued_at: object,
    ) -> str:
        del user_id, session_id, issued_at
        token = f"{token_kind}_{len(generated_tokens) + 1}"
        generated_tokens.append(token)
        return token

    def _fake_get_refresh_token_record_locked(
        self: PersistentSessionIssuanceStore,
        *,
        cursor: object,
        refresh_token_hash: str,
    ) -> RefreshTokenRecord | None:
        del self, cursor
        if refresh_token_hash == refresh_record.refresh_token_hash:
            return refresh_record
        if len(generated_tokens) > 1 and refresh_token_hash == sha256(
            generated_tokens[1].encode("utf-8")
        ).hexdigest():
            return RefreshTokenRecord(
                refresh_token_hash=refresh_token_hash,
                session_id=session_id,
                issued_at=issued_at,
                is_consumed=False,
                consumed_at=None,
            )
        return None

    def _fake_get_session_record_locked(
        self: PersistentSessionIssuanceStore,
        *,
        cursor: object,
        session_id: UUID,
    ) -> IssuedSessionRecord | None:
        del self, cursor
        if session_id != record.session_id:
            return None
        return record

    def _fake_get_session(
        self: PersistentSessionIssuanceStore,
        *,
        session_id: UUID,
    ) -> IssuedSessionRecord | None:
        del self
        if session_id != record.session_id or len(generated_tokens) < 2:
            return None
        access_token = generated_tokens[0]
        refresh_token_value = generated_tokens[1]
        rotated_record = store._apply_activity(record=record, touched_at=clock.now())
        return IssuedSessionRecord(
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
            refresh_token_hash=sha256(refresh_token_value.encode("utf-8")).hexdigest(),
        )

    def _fake_execute_auth_database_transaction(
        *,
        database_url: str,
        transaction_callback: Callable[[object], object],
        reconcile_callback: Callable[[], object | None],
    ) -> object:
        del database_url, reconcile_callback
        fake_connection = _FakeConnection()
        first_result = transaction_callback(fake_connection)
        second_result = transaction_callback(fake_connection)
        del first_result
        return second_result

    monkeypatch.setattr(
        "services.auth.app.session_issuance.connect_auth_database",
        _fake_connection,
    )
    monkeypatch.setattr(
        "services.auth.app.session_issuance.execute_auth_database_transaction",
        _fake_execute_auth_database_transaction,
    )
    monkeypatch.setattr(
        "services.auth.app.session_issuance.PersistentSessionIssuanceStore._get_refresh_token_record_locked",
        _fake_get_refresh_token_record_locked,
    )
    monkeypatch.setattr(
        "services.auth.app.session_issuance.PersistentSessionIssuanceStore._get_session_record_locked",
        _fake_get_session_record_locked,
    )
    monkeypatch.setattr(
        "services.auth.app.session_issuance.PersistentSessionIssuanceStore.get_session",
        _fake_get_session,
    )
    monkeypatch.setattr(
        "services.auth.app.session_issuance._build_opaque_token",
        _fake_build_opaque_token,
    )

    result = store.refresh_session(refresh_token=refresh_token)

    assert result.session_id == session_id
    assert result.access_token == "access_1"
    assert result.refresh_token == "refresh_2"
    assert generated_tokens == ["access_1", "refresh_2"]
