from __future__ import annotations

from typing import Any
from typing import cast
import logging
from contextlib import contextmanager
from collections.abc import Iterator

import pytest
import psycopg
from psycopg import pq

from services.document_ai.app import config as document_ai_config
from services.document_ai.app import persistence_support
from services.document_ai.app.persistence_support import DocumentAISchemaColumnRequirement
from services.document_ai.app.persistence_support import DocumentAITransactionAmbiguousResultError


class _FakePool:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs: dict[str, object] = kwargs
        self.open_calls: list[tuple[bool, float]] = []
        self.close_calls: list[float] = []

    def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        self.open_calls.append((wait, timeout))

    def close(self, timeout: float = 5.0) -> None:
        self.close_calls.append(timeout)

    @contextmanager
    def connection(self):
        yield _FakeConnection()


class _FakeConnection:
    def __init__(
        self,
        status: pq.TransactionStatus = pq.TransactionStatus.IDLE,
        *,
        rollback_resets_status: bool = True,
    ) -> None:
        self.closed = False
        self.rollback_calls = 0
        self._rollback_resets_status = rollback_resets_status
        self.info = _FakeInfo(transaction_status=status)

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self._rollback_resets_status:
            self.info.transaction_status = pq.TransactionStatus.IDLE


class _RetryFakePsycopgError(psycopg.Error):
    def __init__(self, sqlstate: str, message: str = "simulated transaction failure") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _RetryFakeCursor:
    def __init__(self, connection: _RetryFakeConnection) -> None:
        self._connection = connection
        self.executed: list[tuple[str, object | None]] = []

    def __enter__(self) -> _RetryFakeCursor:
        return self

    def __exit__(
        self,
        exc_type: object | None,
        exc: object | None,
        tb: object | None,
    ) -> bool:
        del exc_type, exc, tb
        return False

    def execute(self, sql: str, params: object | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return None


class _RetryFakeTransaction:
    def __init__(self, connection: _RetryFakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _RetryFakeConnection:
        return self._connection

    def __exit__(
        self,
        exc_type: object | None,
        exc: object | None,
        tb: object | None,
    ) -> bool:
        if exc_type is not None:
            self._connection.rollback()
        elif self._connection.commit_error is not None:
            raise self._connection.commit_error
        del exc_type, exc, tb
        return False


class _FakeInfo:
    def __init__(self, *, transaction_status: pq.TransactionStatus) -> None:
        self.transaction_status: pq.TransactionStatus = transaction_status


class _RetryFakeConnection:
    def __init__(self, *, commit_error: BaseException | None = None) -> None:
        self.rollback_calls = 0
        self.transaction_calls = 0
        self.cursor_calls = 0
        self.close_calls = 0
        self.commit_error = commit_error

    def transaction(self) -> _RetryFakeTransaction:
        self.transaction_calls += 1
        return _RetryFakeTransaction(self)

    def cursor(self) -> _RetryFakeCursor:
        self.cursor_calls += 1
        return _RetryFakeCursor(self)

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _ValidationFakeCursor:
    def __init__(
        self,
        *,
        version: str = "CockroachDB CCL v26.2.5",
        current_database: str = "kodi_dev",
        current_user: str = "hackathon_user",
        tables: set[str] | None = None,
        columns: list[tuple[object, ...]] | None = None,
        constraints: list[tuple[object, ...]] | None = None,
        indexes: list[tuple[object, ...]] | None = None,
        fail_on_execute: bool = False,
    ) -> None:
        self.version = version
        self.current_database = current_database
        self.current_user = current_user
        self.tables = tables or set()
        self.columns = columns or []
        self.constraints = constraints or []
        self.indexes = indexes or []
        self.fail_on_execute = fail_on_execute
        self.last_sql = ""
        self.last_params: object | None = None
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _ValidationFakeCursor:
        return self

    def __exit__(
        self,
        exc_type: object | None,
        exc: object | None,
        tb: object | None,
    ) -> bool:
        del exc_type, exc, tb
        return False

    def execute(self, sql: str, params: object | None = None) -> None:
        if self.fail_on_execute:
            raise psycopg.OperationalError("simulated persistence failure")
        self.last_sql = " ".join(sql.split())
        self.last_params = params
        normalized_sql = self.last_sql.lower()
        if "select version(), current_database(), current_user" in normalized_sql:
            self._rows = [(self.version, self.current_database, self.current_user)]
        elif "from information_schema.tables" in normalized_sql:
            self._rows = [(table_name,) for table_name in sorted(self.tables)]
        elif "from information_schema.columns" in normalized_sql:
            self._rows = list(self.columns)
        elif "from information_schema.table_constraints" in normalized_sql:
            self._rows = list(self.constraints)
        elif "from information_schema.statistics" in normalized_sql:
            self._rows = list(self.indexes)
        else:
            self._rows = []

    def fetchone(self) -> tuple[object, ...] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = list(self._rows)
        self._rows.clear()
        return rows


class _ValidationFakeConnection:
    def __init__(self, cursor: _ValidationFakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _ValidationFakeConnection:
        return self

    def __exit__(
        self,
        exc_type: object | None,
        exc: object | None,
        tb: object | None,
    ) -> bool:
        del exc_type, exc, tb
        return False

    def cursor(self) -> _ValidationFakeCursor:
        return self._cursor


@contextmanager
def _borrow_retry_connection(
    connection: _RetryFakeConnection,
) -> Iterator[_RetryFakeConnection]:
    yield connection


def test_get_document_ai_connection_pool_adds_application_name_and_reuses_cached_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence_support, "ConnectionPool", _FakePool)

    database_url = "postgresql://user:secret@db.example.invalid/kodi_dev?sslmode=require"
    pool = cast(_FakePool, persistence_support.get_document_ai_connection_pool(database_url))
    reused_pool = cast(_FakePool, persistence_support.get_document_ai_connection_pool(database_url))

    assert pool is reused_pool
    assert pool.open_calls == [(True, 5.0)]
    assert "application_name=document_ai" in str(pool.kwargs["conninfo"])
    assert "sslmode=require" in str(pool.kwargs["conninfo"])

    persistence_support.close_document_ai_connection_pool(connection_pool=cast(Any, pool))
    assert pool.close_calls == [5.0]


def test_document_ai_pool_connection_reset_rolls_back_aborted_connections() -> None:
    connection = _FakeConnection(status=pq.TransactionStatus.INERROR)

    reset_connection = persistence_support._document_ai_pool_connection_reset  # pyright: ignore[reportPrivateUsage]
    reset_connection(cast(Any, connection))

    assert connection.rollback_calls == 1
    assert connection.info.transaction_status == pq.TransactionStatus.IDLE


def test_document_ai_pool_connection_reset_discards_unusable_connections() -> None:
    connection = _FakeConnection(
        status=pq.TransactionStatus.INERROR,
        rollback_resets_status=False,
    )

    with pytest.raises(psycopg.OperationalError):
        reset_connection = persistence_support._document_ai_pool_connection_reset  # pyright: ignore[reportPrivateUsage]
        reset_connection(cast(Any, connection))

    assert connection.rollback_calls == 1


def test_resolve_document_ai_persistence_status_reports_ready_for_cockroachdb_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _ValidationFakeCursor(
        tables={"document_ai_upload_sessions"},
        columns=[
            ("document_ai_chunk_embeddings", "embedding", "VECTOR(1536)", "vector", "NO"),
        ],
        constraints=[],
        indexes=[],
    )
    monkeypatch.setattr(
        persistence_support,
        "_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES",
        ("document_ai_upload_sessions",),
    )
    monkeypatch.setattr(
        persistence_support,
        "_DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS",
        {
            "document_ai_chunk_embeddings": (
                DocumentAISchemaColumnRequirement("embedding", data_type_contains="vector"),
            )
        },
    )
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS", {})
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES", {})
    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        lambda database_url: _ValidationFakeConnection(cursor),
    )

    status = persistence_support.resolve_document_ai_persistence_status(
        database_url="postgresql://example.invalid/kodi_dev",
        required_tables=("document_ai_upload_sessions",),
    )

    assert status == "ready"


def test_document_ai_persistence_indexes_require_native_vector_search_index() -> None:
    chunk_embedding_indexes = persistence_support._DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES[
        "document_ai_chunk_embeddings"
    ]
    assert "idx_document_ai_chunk_embeddings_vector_search" in chunk_embedding_indexes
    assert "idx_document_ai_chunk_embeddings_cosine_active" not in chunk_embedding_indexes


def test_document_ai_persistence_contract_includes_provider_partitions() -> None:
    assert (
        "document_ai_provider_partitions"
        in persistence_support._DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES
    )
    assert "idx_document_ai_provider_partitions_lookup" in (
        persistence_support._DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES[
            "document_ai_provider_partitions"
        ]
    )


@pytest.mark.parametrize(
    ("version", "current_database", "current_user", "expected_status"),
    [
        ("PostgreSQL 16.0", "kodi_dev", "hackathon_user", "schema_mismatch"),
        ("CockroachDB CCL v26.2.5", "wrong_db", "hackathon_user", "schema_mismatch"),
        ("CockroachDB CCL v26.2.5", "kodi_dev", "wrong_user", "schema_mismatch"),
    ],
)
def test_resolve_document_ai_persistence_status_rejects_wrong_engine_database_or_user(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    current_database: str,
    current_user: str,
    expected_status: str,
) -> None:
    cursor = _ValidationFakeCursor(
        version=version,
        current_database=current_database,
        current_user=current_user,
        tables={"document_ai_upload_sessions"},
    )
    monkeypatch.setattr(
        persistence_support,
        "_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES",
        ("document_ai_upload_sessions",),
    )
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS", {})
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS", {})
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES", {})
    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        lambda database_url: _ValidationFakeConnection(cursor),
    )

    status = persistence_support.resolve_document_ai_persistence_status(
        database_url="postgresql://example.invalid/kodi_dev",
        required_tables=("document_ai_upload_sessions",),
    )

    assert status == expected_status


def test_resolve_document_ai_persistence_status_reports_schema_mismatch_for_missing_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _ValidationFakeCursor(
        tables={"document_ai_chunk_embeddings"},
        columns=[
            (
                "document_ai_chunk_embeddings",
                "embedding_dimensions",
                "INTEGER",
                "int4",
                "NO",
            ),
        ],
    )
    monkeypatch.setattr(
        persistence_support,
        "_DOCUMENT_AI_REQUIRED_PERSISTENCE_TABLES",
        ("document_ai_chunk_embeddings",),
    )
    monkeypatch.setattr(
        persistence_support,
        "_DOCUMENT_AI_REQUIRED_PERSISTENCE_COLUMNS",
        {
            "document_ai_chunk_embeddings": (
                DocumentAISchemaColumnRequirement("embedding", data_type_contains="vector"),
            )
        },
    )
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_CONSTRAINTS", {})
    monkeypatch.setattr(persistence_support, "_DOCUMENT_AI_REQUIRED_PERSISTENCE_INDEXES", {})
    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        lambda database_url: _ValidationFakeConnection(cursor),
    )

    status = persistence_support.resolve_document_ai_persistence_status(
        database_url="postgresql://example.invalid/kodi_dev",
        required_tables=("document_ai_chunk_embeddings",),
    )

    assert status == "schema_mismatch"


def test_resolve_document_ai_persistence_status_reports_unavailable_for_sql_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _ValidationFakeCursor(fail_on_execute=True)
    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        lambda database_url: _ValidationFakeConnection(cursor),
    )

    status = persistence_support.resolve_document_ai_persistence_status(
        database_url="postgresql://example.invalid/kodi_dev",
        required_tables=("document_ai_upload_sessions",),
    )

    assert status == "unavailable"


def test_execute_document_ai_database_transaction_succeeds_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetryFakeConnection()

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connection)

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )

    callback_calls = 0

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        return "ok"

    result = persistence_support.execute_document_ai_database_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        transaction_name="document_ai.test.success",
        transaction_callback=_callback,
    )

    assert result == "ok"
    assert callback_calls == 1
    assert connection.rollback_calls == 0
    assert connection.transaction_calls == 1
    assert connection.cursor_calls == 1


def test_execute_document_ai_database_transaction_retries_until_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_connection = _RetryFakeConnection()
    second_connection = _RetryFakeConnection()
    connections = [first_connection, second_connection]

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connections.pop(0))

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR,
        "3",
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR,
        "10",
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR,
        "20",
    )

    callback_calls = 0
    sleep_calls: list[float] = []

    def _sleep(delay_seconds: float) -> None:
        sleep_calls.append(delay_seconds)

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        if callback_calls == 1:
            raise _RetryFakePsycopgError("40001")
        return "recovered"

    with caplog.at_level(logging.INFO, logger="document_ai.persistence"):
        result = persistence_support.execute_document_ai_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_name="document_ai.test.retry",
            transaction_callback=_callback,
            sleep_fn=_sleep,
            jitter_fn=lambda: 0.5,
        )

    assert result == "recovered"
    assert callback_calls == 2
    assert sleep_calls == [0.005]
    assert first_connection.rollback_calls == 1
    assert second_connection.rollback_calls == 0
    assert "document_ai.transaction.retrying" in caplog.text


def test_execute_document_ai_database_transaction_exhausts_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetryFakeConnection()

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connection)

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR,
        "2",
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR,
        "10",
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR,
        "20",
    )

    callback_calls = 0
    sleep_calls: list[float] = []

    def _sleep(delay_seconds: float) -> None:
        sleep_calls.append(delay_seconds)

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        raise _RetryFakePsycopgError("40001")

    with pytest.raises(_RetryFakePsycopgError) as excinfo:
        persistence_support.execute_document_ai_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_name="document_ai.test.exhausted",
            transaction_callback=_callback,
            sleep_fn=_sleep,
            jitter_fn=lambda: 1.0,
        )

    assert excinfo.value.sqlstate == "40001"
    assert callback_calls == 2
    assert sleep_calls == [0.01]
    assert connection.rollback_calls == 2


def test_execute_document_ai_database_transaction_does_not_retry_non_serialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetryFakeConnection()

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connection)

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )

    callback_calls = 0

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        raise _RetryFakePsycopgError("23505")

    with pytest.raises(_RetryFakePsycopgError) as excinfo:
        persistence_support.execute_document_ai_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_name="document_ai.test.non_retryable",
            transaction_callback=_callback,
            sleep_fn=lambda delay_seconds: (_ for _ in ()).throw(
                AssertionError(f"unexpected sleep {delay_seconds}")
            ),
        )

    assert excinfo.value.sqlstate == "23505"
    assert callback_calls == 1
    assert connection.rollback_calls == 1


def test_execute_document_ai_database_transaction_does_not_retry_application_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetryFakeConnection()

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connection)

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )

    callback_calls = 0

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        raise ValueError("application failure")

    with pytest.raises(ValueError, match="application failure"):
        persistence_support.execute_document_ai_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_name="document_ai.test.application_error",
            transaction_callback=_callback,
        )

    assert callback_calls == 1
    assert connection.rollback_calls == 1


def test_execute_document_ai_database_transaction_reconciles_40003_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_connection = _RetryFakeConnection(commit_error=_RetryFakePsycopgError("40003"))
    reconcile_connection = _RetryFakeConnection()
    connections = [failed_connection, reconcile_connection]

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connections.pop(0))

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )

    callback_calls = 0
    reconcile_calls = 0

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        return "original-result"

    def _reconcile(connection: Any) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        with connection.cursor() as cursor:
            cursor.execute("SELECT 2")
        return "durable-result"

    result = persistence_support.execute_document_ai_database_transaction(
        database_url="postgresql://example.invalid/kodi_dev",
        transaction_name="document_ai.test.ambiguous",
        transaction_callback=_callback,
        reconcile_ambiguous_result=_reconcile,
        sleep_fn=lambda _: pytest.fail("sleep must not be called"),
    )

    assert result == "durable-result"
    assert callback_calls == 1
    assert reconcile_calls == 1
    assert failed_connection.rollback_calls == 1
    assert failed_connection.close_calls == 0


def test_execute_document_ai_database_transaction_raises_explicit_error_when_40003_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetryFakeConnection(commit_error=_RetryFakePsycopgError("40003"))

    def _connect_document_ai_database(database_url: str) -> Any:
        del database_url
        return _borrow_retry_connection(connection)

    monkeypatch.setattr(
        persistence_support,
        "connect_document_ai_database",
        _connect_document_ai_database,
    )

    callback_calls = 0

    def _callback(cursor: Any) -> str:
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute("SELECT 1")
        return "original-result"

    with pytest.raises(DocumentAITransactionAmbiguousResultError) as excinfo:
        persistence_support.execute_document_ai_database_transaction(
            database_url="postgresql://example.invalid/kodi_dev",
            transaction_name="document_ai.test.ambiguous_unresolved",
            transaction_callback=_callback,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert excinfo.value.reason_code == "document_ai_persistence_ambiguous_result"
    assert connection.rollback_calls == 1
    assert connection.close_calls == 0


def test_document_ai_database_transaction_retry_config_validates_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR,
        raising=False,
    )
    monkeypatch.delenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR,
        raising=False,
    )
    monkeypatch.delenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR,
        raising=False,
    )

    assert document_ai_config.get_document_ai_database_transaction_max_attempts() == 5
    assert document_ai_config.get_document_ai_database_transaction_backoff_base_ms() == 100
    assert document_ai_config.get_document_ai_database_transaction_backoff_max_ms() == 2000

    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_MAX_ATTEMPTS_ENV_VAR,
        "0",
    )
    with pytest.raises(RuntimeError):
        document_ai_config.get_document_ai_database_transaction_max_attempts()

    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR,
        "-1",
    )
    with pytest.raises(RuntimeError):
        document_ai_config.get_document_ai_database_transaction_backoff_base_ms()

    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_BASE_MS_ENV_VAR,
        "100",
    )
    monkeypatch.setenv(
        document_ai_config.DOCUMENT_AI_DATABASE_TRANSACTION_BACKOFF_MAX_MS_ENV_VAR,
        "50",
    )
    with pytest.raises(RuntimeError):
        document_ai_config.get_document_ai_database_transaction_backoff_max_ms()
