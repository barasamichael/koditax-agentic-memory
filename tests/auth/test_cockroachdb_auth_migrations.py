"""Unit tests for the CockroachDB auth migration runner."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from typing import Callable

import pytest
import psycopg

from services.auth.migrations.cockroachdb import runner


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized_sql = " ".join(sql.lower().split())
        self._connection.executed_statements.append((sql, params))

        if "select current_database(), version()" in normalized_sql:
            self._connection.last_fetchone = (
                self._connection.current_database,
                self._connection.version_text,
            )
            return
        if "select checksum_sha256" in normalized_sql:
            if params is None:
                self._connection.last_fetchone = None
                return
            migration_name = str(params[0])
            checksum = self._connection.ledger.get(migration_name)
            self._connection.last_fetchone = None if checksum is None else (checksum,)
            return
        if "create table if not exists auth_cockroachdb_schema_migrations" in normalized_sql:
            self._connection.ledger_table_created += 1
            self._connection.last_fetchone = None
            return
        if normalized_sql.startswith("insert into auth_cockroachdb_schema_migrations"):
            migration_name, checksum_sha256 = params or ("", "")
            self._connection.pending_ledger[migration_name] = str(checksum_sha256)
            self._connection.last_fetchone = None
            return
        if "fail_migration" in normalized_sql:
            raise psycopg.OperationalError("simulated migration failure")

        self._connection.last_fetchone = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._connection.last_fetchone

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeTransaction:
    def __init__(self, connection: "_FakeConnection") -> None:
        self._connection = connection

    def __enter__(self) -> "_FakeConnection":
        self._connection.transaction_depth += 1
        return self._connection

    def __exit__(
        self, exc_type: object | None, exc: object | None, tb: object | None
    ) -> bool:
        self._connection.transaction_depth -= 1
        if exc_type is None:
            self._connection.ledger.update(self._connection.pending_ledger)
            self._connection.pending_ledger.clear()
        else:
            self._connection.pending_ledger.clear()
        return False


class _FakeConnection:
    def __init__(
        self,
        *,
        current_database: str = "kodi_dev",
        version_text: str = "CockroachDB CCL v26.2.5",
    ) -> None:
        self.current_database = current_database
        self.version_text = version_text
        self.executed_statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.ledger: dict[str, str] = {}
        self.pending_ledger: dict[str, str] = {}
        self.ledger_table_created = 0
        self.transaction_depth = 0
        self.last_fetchone: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(
        self, exc_type: object | None, exc: object | None, tb: object | None
    ) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


def test_discover_migration_files_sorts_lexically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = [
        tmp_path / "0004_auth_lifecycle.sql",
        tmp_path / "0001_auth_core.sql",
        tmp_path / "0003_auth_challenges.sql",
        tmp_path / "0002_auth_runtime.sql",
    ]
    for file_path in files:
        file_path.write_text("select 1;", encoding="utf-8")

    monkeypatch.setattr(runner, "MIGRATIONS_DIRECTORY", tmp_path)

    discovered = runner.discover_migration_files()
    assert [path.name for path in discovered] == [
        "0001_auth_core.sql",
        "0002_auth_runtime.sql",
        "0003_auth_challenges.sql",
        "0004_auth_lifecycle.sql",
    ]


def test_apply_migrations_runs_in_order_and_creates_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_files = _write_migration_files(
        tmp_path,
        {
            "0002_auth_runtime.sql": "SELECT 'runtime';",
            "0001_auth_core.sql": "SELECT 'core';",
        },
    )
    fake_connection = _FakeConnection()

    monkeypatch.setattr(runner, "_validate_target_database", _return_kodi_dev)
    monkeypatch.setattr(runner, "_prevalidate_migration_schema", _noop)
    monkeypatch.setattr(runner, "_validate_migration_schema", _noop)
    monkeypatch.setattr(runner, "_validate_final_schema", _noop)
    monkeypatch.setattr(runner, "_load_applied_checksum", _return_none_checksum)
    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    runner.apply_migrations("postgresql://example.invalid/kodi_dev", migration_files)

    assert fake_connection.ledger_table_created == 1
    assert [sql for sql, _ in fake_connection.executed_statements if "select 'core'" in sql.lower()]  # sanity
    executed_sql = [
        sql
        for sql, _ in fake_connection.executed_statements
        if "select 'core'" in sql.lower() or "select 'runtime'" in sql.lower()
    ]
    assert executed_sql == ["SELECT 'core';", "SELECT 'runtime';"]


def test_apply_migrations_skips_already_applied_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_files = _write_migration_files(
        tmp_path,
        {
            "0001_auth_core.sql": "SELECT 'core';",
            "0002_auth_runtime.sql": "SELECT 'runtime';",
        },
    )
    fake_connection = _FakeConnection()

    monkeypatch.setattr(runner, "_validate_target_database", _return_kodi_dev)
    monkeypatch.setattr(runner, "_prevalidate_migration_schema", _noop)
    monkeypatch.setattr(runner, "_validate_migration_schema", _noop)
    monkeypatch.setattr(runner, "_validate_final_schema", _noop)
    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    def _load_applied_checksum(*, migration_name: str, **kwargs: Any) -> str | None:
        if migration_name == "0001_auth_core.sql":
            return hashlib.sha256("SELECT 'core';".encode("utf-8")).hexdigest()
        return None

    monkeypatch.setattr(runner, "_load_applied_checksum", _load_applied_checksum)

    runner.apply_migrations("postgresql://example.invalid/kodi_dev", migration_files)

    executed_sql = [
        sql
        for sql, _ in fake_connection.executed_statements
        if "select 'core'" in sql.lower() or "select 'runtime'" in sql.lower()
    ]
    assert executed_sql == ["SELECT 'runtime';"]


def test_apply_migrations_rejects_checksum_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_files = _write_migration_files(
        tmp_path,
        {"0001_auth_core.sql": "SELECT 'core';"},
    )
    fake_connection = _FakeConnection()

    monkeypatch.setattr(runner, "_validate_target_database", _return_kodi_dev)
    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))
    monkeypatch.setattr(
        runner,
        "_load_applied_checksum",
        _return_deadbeef_checksum,
    )

    with pytest.raises(runner.AuthSchemaMismatchError):
        runner.apply_migrations("postgresql://example.invalid/kodi_dev", migration_files)

    assert not fake_connection.executed_statements


def test_apply_migrations_rolls_back_failed_migration_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_files = _write_migration_files(
        tmp_path,
        {
            "0001_auth_core.sql": "SELECT 'core';",
            "0002_auth_runtime.sql": "SELECT 'fail_migration runtime';",
            "0003_auth_challenges.sql": "SELECT 'later';",
        },
    )
    fake_connection = _FakeConnection()

    monkeypatch.setattr(runner, "_validate_target_database", _return_kodi_dev)
    monkeypatch.setattr(runner, "_prevalidate_migration_schema", _noop)
    monkeypatch.setattr(runner, "_validate_migration_schema", _noop)
    monkeypatch.setattr(runner, "_validate_final_schema", _noop)
    monkeypatch.setattr(runner, "_load_applied_checksum", _return_none_checksum)
    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    with pytest.raises(runner.AuthMigrationError):
        runner.apply_migrations("postgresql://example.invalid/kodi_dev", migration_files)

    executed_sql = [sql for sql, _ in fake_connection.executed_statements]
    assert any("SELECT 'core';" == sql for sql in executed_sql)
    assert any("SELECT 'fail_migration runtime';" == sql for sql in executed_sql)
    assert not any("SELECT 'later';" == sql for sql in executed_sql)
    assert "0002_auth_runtime.sql" not in fake_connection.ledger
    assert "0003_auth_challenges.sql" not in fake_connection.ledger


def test_validate_target_database_rejects_wrong_database() -> None:
    fake_connection = _FakeConnection(current_database="wrong_db")
    with pytest.raises(runner.AuthTargetError):
        runner._validate_target_database(fake_connection)  # type: ignore[arg-type]


def test_validate_target_database_rejects_non_cockroachdb() -> None:
    fake_connection = _FakeConnection(version_text="PostgreSQL 16.0")
    with pytest.raises(runner.AuthTargetError):
        runner._validate_target_database(fake_connection)  # type: ignore[arg-type]


def test_main_sanitizes_connection_exception_and_hides_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_url = "postgresql://user:secret@example.invalid/kodi_dev"
    monkeypatch.setattr(runner, "load_auth_database_url", _load_database_url_factory(database_url))
    monkeypatch.setattr(runner, "discover_migration_files", _empty_migration_files)

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError(f"could not connect to {database_url}")

    monkeypatch.setattr(runner.psycopg, "connect", _raise)

    exit_code = runner.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert database_url not in captured.out
    assert database_url not in captured.err
    assert "Authentication database connection failed." in captured.err or "could not connect" not in captured.err


def _write_migration_files(tmp_path: Path, files: dict[str, str]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for name, contents in files.items():
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        paths.append(path)
    return tuple(sorted(paths, key=lambda item: item.name))


def _return_kodi_dev(connection: object) -> str:
    return "kodi_dev"


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


def _return_none_checksum(*args: Any, **kwargs: Any) -> str | None:
    return None


def _return_deadbeef_checksum(*args: Any, **kwargs: Any) -> str:
    return "deadbeef"


def _connect_factory(connection: _FakeConnection) -> Callable[..., _FakeConnection]:
    def _connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        return connection

    return _connect


def _load_database_url_factory(database_url: str) -> Callable[[], str]:
    def _load_database_url() -> str:
        return database_url

    return _load_database_url


def _empty_migration_files() -> tuple[Path, ...]:
    return ()
