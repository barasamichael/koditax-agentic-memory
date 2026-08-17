"""Unit tests for auth CockroachDB transaction execution support."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import psycopg

from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_AMBIGUOUS_TOTAL
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_RETRY_TOTAL
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL
from services.auth.app.metrics import get_default_auth_metrics_emitter
from services.auth.app.metrics import reset_default_auth_metrics_emitter
from services.auth.app.persistence_support import AuthCockroachTransactionAmbiguousCommitError
from services.auth.app.persistence_support import AuthCockroachTransactionRetryExhaustedError
from services.auth.app.persistence_support import AuthCockroachTransactionRetryPolicy
from services.auth.app.persistence_support import AuthCockroachTransactionSqlError
from services.auth.app.persistence_support import AuthCockroachTransactionUnavailableError
from services.auth.app.persistence_support import execute_auth_database_transaction


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_default_auth_metrics_emitter()
    yield
    reset_default_auth_metrics_emitter()


class _FakePsycopgError(psycopg.Error):
    def __init__(self, sqlstate: str, message: str = "simulated transaction failure") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self._connection = connection

    def __enter__(self) -> "_FakeCursor":
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
    def __init__(self, *, name: str) -> None:
        self.name = name
        self.executed_statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


def test_execute_auth_database_transaction_commits_and_closes_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="success")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_calls: list[str] = []

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        callback_calls.append("called")
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "ok"

    result = execute_auth_database_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        transaction_callback=_transaction_callback,
    )

    assert result == "ok"
    assert callback_calls == ["called"]
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert connection.close_count == 1


def test_execute_auth_database_transaction_rolls_back_and_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="failure")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise ValueError("application failure")

    with pytest.raises(ValueError):
        execute_auth_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_callback=_transaction_callback,
        )

    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_database_transaction_retries_only_40001_and_emits_metrics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_connection = _FakeConnection(name="attempt-1")
    second_connection = _FakeConnection(name="attempt-2")
    connections = [first_connection, second_connection]

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connections.pop(0),
    )

    sleep_calls: list[float] = []
    callback_calls: list[str] = []
    reset_default_auth_metrics_emitter()

    def _sleep(delay_seconds: float) -> None:
        sleep_calls.append(delay_seconds)

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        callback_calls.append(cast_str(resolved_connection))
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        if len(callback_calls) == 1:
            raise _FakePsycopgError("40001")
        return "recovered"

    with caplog.at_level(logging.INFO, logger="auth.persistence"):
        result = execute_auth_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_callback=_transaction_callback,
            retry_policy=AuthCockroachTransactionRetryPolicy(
                max_attempts=3,
                base_delay_seconds=0.1,
                backoff_multiplier=2.0,
                max_delay_seconds=1.0,
                jitter_ratio=0.2,
            ),
            retry_jitter=0.5,
            sleep=_sleep,
        )

    assert result == "recovered"
    assert len(callback_calls) == 2
    assert sleep_calls == [pytest.approx(0.12)]
    assert first_connection.commit_count == 0
    assert first_connection.rollback_count == 1
    assert first_connection.close_count == 1
    assert second_connection.commit_count == 1
    assert second_connection.rollback_count == 0
    assert second_connection.close_count == 1

    metrics = get_default_auth_metrics_emitter().snapshot()
    assert _metric_ids(metrics) == {
        AUTH_PERSISTENCE_TRANSACTION_RETRY_TOTAL,
        AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL,
    }
    assert "auth.persistence.transaction" in caplog.text
    assert "retrying" in caplog.text


def test_execute_auth_database_transaction_exhausts_retry_budget(
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

    def _sleep(delay_seconds: float) -> None:
        sleep_calls.append(delay_seconds)

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise _FakePsycopgError("40001")

    with pytest.raises(AuthCockroachTransactionRetryExhaustedError):
        execute_auth_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_callback=_transaction_callback,
            retry_policy=AuthCockroachTransactionRetryPolicy(
                max_attempts=2,
                base_delay_seconds=0.1,
                backoff_multiplier=2.0,
                max_delay_seconds=1.0,
                jitter_ratio=0.2,
            ),
            sleep=_sleep,
        )

    assert sleep_calls == [pytest.approx(0.1)]
    assert first_connection.rollback_count == 1
    assert first_connection.close_count == 1
    assert second_connection.rollback_count == 1
    assert second_connection.close_count == 1


def test_execute_auth_database_transaction_reconciles_40003_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="ambiguous")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    callback_calls = 0
    reconcile_calls = 0

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        nonlocal callback_calls
        callback_calls += 1
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise _FakePsycopgError("40003")

    def _reconcile_callback() -> str:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return "durable-result"

    result = execute_auth_database_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        transaction_callback=_transaction_callback,
        reconcile_callback=_reconcile_callback,
    )

    assert result == "durable-result"
    assert callback_calls == 1
    assert reconcile_calls == 1
    assert connection.rollback_count == 1
    assert connection.commit_count == 0
    assert connection.close_count == 1

    metrics = get_default_auth_metrics_emitter().snapshot()
    assert AUTH_PERSISTENCE_TRANSACTION_AMBIGUOUS_TOTAL in _metric_ids(metrics)


def test_execute_auth_database_transaction_raises_controlled_error_when_40003_cannot_be_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="ambiguous-failure")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise _FakePsycopgError("40003")

    with pytest.raises(AuthCockroachTransactionAmbiguousCommitError):
        execute_auth_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_callback=_transaction_callback,
        )

    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_database_transaction_wraps_non_retryable_sqlstate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(name="sql-error")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        lambda database_url: connection,
    )

    def _transaction_callback(resolved_connection: psycopg.Connection[Any]) -> str:
        with resolved_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        raise _FakePsycopgError("23505")

    with pytest.raises(AuthCockroachTransactionSqlError) as excinfo:
        execute_auth_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_callback=_transaction_callback,
        )

    assert excinfo.value.sqlstate == "23505"
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_execute_auth_database_transaction_wraps_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _connect(_: str) -> psycopg.Connection[Any]:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(
        "services.auth.app.persistence_support.open_auth_database_connection",
        _connect,
    )

    with pytest.raises(AuthCockroachTransactionUnavailableError):
        execute_auth_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_callback=lambda connection: "unused",
        )

    metrics = get_default_auth_metrics_emitter().snapshot()
    assert AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL in _metric_ids(metrics)


def _metric_ids(metrics: tuple[object, ...]) -> set[str]:
    return {getattr(metric, "metric_id") for metric in metrics}


def cast_str(value: object) -> str:
    return str(value)
