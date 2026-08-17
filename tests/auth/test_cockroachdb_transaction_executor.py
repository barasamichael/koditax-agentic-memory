"""Focused unit tests for the auth CockroachDB transaction executor."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import logging

import pytest
import psycopg

from services.auth.app.persistence_support import _extract_sqlstate
from services.auth.app.persistence_support import execute_auth_transaction
from services.auth.app.persistence_support import AuthCockroachTransactionSqlError
from services.auth.app.persistence_support import _classify_auth_transaction_sqlstate
from services.auth.app.persistence_support import AuthCockroachTransactionRetryExhaustedError
from services.auth.app.persistence_support import AuthCockroachTransactionAmbiguousCommitError


class _FakePsycopgError(psycopg.Error):
    def __init__(self, sqlstate: str, message: str = "simulated transaction failure") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeDiagnostic:
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(
        self,
        exc_type: object | None,
        exc: object | None,
        tb: object | None,
    ) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._connection.executed_statements.append((sql, params))


class _FakeConnection:
    def __init__(
        self,
        *,
        name: str,
        commit_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.name = name
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.executed_statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_count += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture(autouse=True)
def _silence_auth_persistence_logs() -> None:
    yield


def test_execute_auth_transaction_commits_returns_result_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="success")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    payload = {"email": "alice@example.com", "attempt": 1}
    callback_connections: list[str] = []

    def _operation(resolved_connection: psycopg.Connection[Any]) -> dict[str, Any]:
        callback_connections.append(cast_name(resolved_connection))
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        assert payload == {"email": "alice@example.com", "attempt": 1}
        return payload

    def _raise_on_info(message: object, *args: object, **kwargs: object) -> None:
        del message, args, kwargs
        raise RuntimeError("structured persistence logger unavailable")

    warnings: list[str] = []

    def _capture_warning(message: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        warnings.append(str(message))

    monkeypatch.setattr(
        "services.auth.app.persistence_support.LOGGER.info",
        _raise_on_info,
    )
    monkeypatch.setattr(
        "services.auth.app.persistence_support.LOGGER.warning",
        _capture_warning,
    )

    result = execute_auth_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        operation_name="auth_write",
        operation=_operation,
        jitter_fn=lambda: 0.5,
        sleep_fn=lambda _: pytest.fail("sleep must not be called on success"),
    )

    assert result is payload
    assert callback_connections == ["success"]
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert connection.close_count == 1
    assert warnings
    assert "transaction_event_emit_failed" in warnings[0]


def test_execute_auth_transaction_rolls_back_on_callback_failure_and_preserves_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="callback-failure")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_error = ValueError("validation failure")

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise callback_error

    with pytest.raises(ValueError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
        )

    assert excinfo.value is callback_error
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_transaction_preserves_callback_failure_when_rollback_and_close_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        name="cleanup-failure",
        rollback_error=RuntimeError("rollback failure"),
        close_error=RuntimeError("close failure"),
    )

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_error = ValueError("validation failure")

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise callback_error

    with pytest.raises(ValueError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
        )

    assert excinfo.value is callback_error
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_transaction_wraps_non_retryable_commit_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        name="commit-failure",
        commit_error=_FakePsycopgError("23505"),
    )

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_calls = 0

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "unused"

    with pytest.raises(AuthCockroachTransactionSqlError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
        )

    assert callback_calls == 1
    assert excinfo.value.sqlstate == "23505"
    assert connection.commit_count == 1
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_transaction_retries_only_40001_callback_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = _FakeConnection(name="attempt-1")
    second_connection = _FakeConnection(name="attempt-2")
    connections = [first_connection, second_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    sleep_calls: list[float] = []
    callback_connections: list[str] = []
    payload = {"stable": ["value"]}
    payload_snapshot = deepcopy(payload)

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        callback_connections.append(cast_name(resolved_connection))
        assert payload == payload_snapshot
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        if len(callback_connections) == 1:
            raise _FakePsycopgError("40001")
        return "recovered"

    result = execute_auth_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        operation_name="auth_write",
        operation=_operation,
        sleep_fn=sleep_calls.append,
        jitter_fn=lambda: 0.5,
    )

    assert result == "recovered"
    assert callback_connections == ["attempt-1", "attempt-2"]
    assert sleep_calls == [pytest.approx(0.025)]
    assert first_connection.commit_count == 0
    assert first_connection.rollback_count == 1
    assert first_connection.close_count == 1
    assert second_connection.commit_count == 1
    assert second_connection.rollback_count == 0
    assert second_connection.close_count == 1
    assert payload == payload_snapshot


def test_execute_auth_transaction_retries_40001_commit_failures_with_new_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = _FakeConnection(name="attempt-1", commit_error=_FakePsycopgError("40001"))
    second_connection = _FakeConnection(name="attempt-2")
    connections = [first_connection, second_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    callback_connections: list[str] = []
    sleep_calls: list[float] = []

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        callback_connections.append(cast_name(resolved_connection))
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "committed"

    result = execute_auth_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        operation_name="auth_write",
        operation=_operation,
        sleep_fn=sleep_calls.append,
        jitter_fn=lambda: 0.5,
    )

    assert result == "committed"
    assert callback_connections == ["attempt-1", "attempt-2"]
    assert sleep_calls == [pytest.approx(0.025)]
    assert first_connection.commit_count == 1
    assert first_connection.rollback_count == 1
    assert first_connection.close_count == 1
    assert second_connection.commit_count == 1
    assert second_connection.rollback_count == 0
    assert second_connection.close_count == 1


def test_execute_auth_transaction_exhausts_three_attempts_without_sleeping_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = _FakeConnection(name="attempt-1")
    second_connection = _FakeConnection(name="attempt-2")
    third_connection = _FakeConnection(name="attempt-3")
    connections = [first_connection, second_connection, third_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    sleep_calls: list[float] = []
    callback_connections: list[str] = []

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        callback_connections.append(cast_name(resolved_connection))
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise _FakePsycopgError("40001")

    with pytest.raises(AuthCockroachTransactionRetryExhaustedError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            sleep_fn=sleep_calls.append,
            jitter_fn=lambda: 0.5,
        )

    assert callback_connections == ["attempt-1", "attempt-2", "attempt-3"]
    assert sleep_calls == [pytest.approx(0.025), pytest.approx(0.05)]
    assert first_connection.rollback_count == 1
    assert second_connection.rollback_count == 1
    assert third_connection.rollback_count == 1
    assert first_connection.close_count == 1
    assert second_connection.close_count == 1
    assert third_connection.close_count == 1
    assert excinfo.value.reason_code == "auth_transaction_retry_exhausted"


def test_execute_auth_transaction_does_not_retry_text_that_mentions_40001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="text-only")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_calls = 0

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise ValueError("serialization-like text 40001 but no sqlstate")

    with pytest.raises(ValueError):
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_transaction_reconciles_40003_without_replaying_operation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed_connection = _FakeConnection(name="failed-40003")
    reconcile_connection = _FakeConnection(name="reconcile-40003")
    connections = [failed_connection, reconcile_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    failed_connection.commit_error = _FakePsycopgError("40003")

    callback_calls = 0
    reconcile_calls = 0
    captured_reconcile_connection_names: list[str] = []

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "original-result"

    def _reconcile(resolved_connection: psycopg.Connection[Any]) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        assert failed_connection.close_count == 1
        captured_reconcile_connection_names.append(cast_name(resolved_connection))
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 2")
        return "durable-result"

    with caplog.at_level(logging.INFO, logger="auth.persistence"):
        result = execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            reconcile_ambiguous_result=_reconcile,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert result == "durable-result"
    assert callback_calls == 1
    assert reconcile_calls == 1
    assert captured_reconcile_connection_names == ["reconcile-40003"]
    assert failed_connection.rollback_count == 1
    assert failed_connection.close_count == 1
    assert reconcile_connection.commit_count == 0
    assert reconcile_connection.rollback_count == 0
    assert reconcile_connection.close_count == 1
    assert "auth transaction ambiguous result" in caplog.text
    assert "auth transaction reconciliation started" in caplog.text
    assert "auth transaction reconciliation succeeded" in caplog.text
    assert "auth transaction reconciliation failed" not in caplog.text


def test_execute_auth_transaction_raises_controlled_error_when_40003_has_no_reconciliation_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="40003-no-reconcile")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_calls = 0

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "original-result"

    connection.commit_error = _FakePsycopgError("40003")

    with pytest.raises(AuthCockroachTransactionAmbiguousCommitError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert excinfo.value.reason_code == "auth_persistence_ambiguous_result"
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_transaction_raises_controlled_error_when_reconciliation_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_connection = _FakeConnection(name="failed-none")
    reconcile_connection = _FakeConnection(name="reconcile-none")
    connections = [failed_connection, reconcile_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    callback_calls = 0
    reconcile_calls = 0

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "original-result"

    def _reconcile(resolved_connection: psycopg.Connection[Any]) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 2")
        return None

    failed_connection.commit_error = _FakePsycopgError("40003")

    with pytest.raises(AuthCockroachTransactionAmbiguousCommitError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            reconcile_ambiguous_result=_reconcile,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert reconcile_calls == 1
    assert excinfo.value.reason_code == "auth_persistence_ambiguous_result"
    assert failed_connection.rollback_count == 1
    assert failed_connection.close_count == 1
    assert reconcile_connection.close_count == 1


def test_execute_auth_transaction_raises_controlled_error_when_reconciliation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_connection = _FakeConnection(name="failed-error")
    reconcile_connection = _FakeConnection(name="reconcile-error")
    connections = [failed_connection, reconcile_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    callback_calls = 0
    reconcile_calls = 0

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "original-result"

    def _reconcile(resolved_connection: psycopg.Connection[Any]) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 2")
        raise RuntimeError("reconciliation exploded")

    failed_connection.commit_error = _FakePsycopgError("40003")

    with pytest.raises(AuthCockroachTransactionAmbiguousCommitError) as excinfo:
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            reconcile_ambiguous_result=_reconcile,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert reconcile_calls == 1
    assert excinfo.value.reason_code == "auth_persistence_ambiguous_result"
    assert failed_connection.rollback_count == 1
    assert failed_connection.close_count == 1
    assert reconcile_connection.close_count == 1


def test_classify_auth_transaction_sqlstate_recognizes_40001_and_40003() -> None:
    assert _classify_auth_transaction_sqlstate("40001") == "retryable_serialization_failure"
    assert _classify_auth_transaction_sqlstate("40003") == "ambiguous_transaction_result"
    assert _classify_auth_transaction_sqlstate("23505") == "non_retryable"


def test_execute_auth_transaction_does_not_retry_40003_text_without_real_sqlstate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="40003-text-only")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_calls = 0

    def _operation(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise ValueError("commit maybe 40003 but no real sqlstate")

    with pytest.raises(ValueError):
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=_operation,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_extract_sqlstate_recognizes_supported_psycopg_shapes() -> None:
    assert _extract_sqlstate(_FakePsycopgError("40001")) == "40001"
    assert _extract_sqlstate(_FakePsycopgError("40003")) == "40003"
    assert _extract_sqlstate(_FakePsycopgError("23505")) == "23505"
    assert _extract_sqlstate(_FakePsycopgError("23503")) == "23503"
    assert _extract_sqlstate(_ErrorWithDiagnostic("40001")) == "40001"
    assert _extract_sqlstate(_MissingSqlstateError()) is None


def test_extract_sqlstate_does_not_use_exception_text() -> None:
    error = RuntimeError("40001 in exception text only")
    assert _extract_sqlstate(error) is None
    assert _extract_sqlstate(RuntimeError("40003 in exception text only")) is None


def test_extract_sqlstate_never_raises_secondary_exception() -> None:
    assert _extract_sqlstate(_ExplodingSqlstateError()) is None


def test_execute_auth_transaction_rejects_invalid_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="unused")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    with pytest.raises(ValueError):
        execute_auth_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            operation_name="auth_write",
            operation=lambda resolved_connection: "unused",
            max_attempts=0,
        )


def test_execute_auth_transaction_does_not_leak_sensitive_values_in_logs_or_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = _FakeConnection(name="secret-safe", commit_error=_FakePsycopgError("23505"))

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    database_url = "postgresql://db-url-sentinel"
    database_password = "db-password-sentinel"
    access_token = "access-token-sentinel"
    refresh_token = "refresh-token-sentinel"
    otp = "otp-sentinel"
    email = "email-sentinel@example.com"
    phone = "+254700000000"

    with caplog.at_level(logging.INFO, logger="auth.persistence"):
        with pytest.raises(AuthCockroachTransactionSqlError) as excinfo:
            execute_auth_transaction(
                database_url=database_url,
                operation_name="auth_write",
                operation=lambda resolved_connection: "unused",
            )

    captured = "\n".join([caplog.text, str(excinfo.value)])
    for secret in (
        database_url,
        database_password,
        access_token,
        refresh_token,
        otp,
        email,
        phone,
    ):
        assert secret not in captured


class _ErrorWithDiagnostic(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("diagnostic fallback")
        self._diag = _FakeDiagnostic(sqlstate)

    @property
    def sqlstate(self) -> str:
        raise RuntimeError("sqlstate property exploded")

    @property
    def diag(self) -> _FakeDiagnostic:
        return self._diag


class _MissingSqlstateError(Exception):
    def __init__(self) -> None:
        super().__init__("missing sqlstate")

    @property
    def sqlstate(self) -> object:
        return None


class _ExplodingSqlstateError(Exception):
    def __init__(self) -> None:
        super().__init__("exploding sqlstate")

    @property
    def sqlstate(self) -> object:
        raise RuntimeError("primary field exploded")

    @property
    def diag(self) -> object:
        raise RuntimeError("diagnostic field exploded")


def cast_name(connection: object) -> str:
    return str(getattr(connection, "name", connection))
