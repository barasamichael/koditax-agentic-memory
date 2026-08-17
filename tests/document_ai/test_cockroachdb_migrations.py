"""Unit tests for the Document AI CockroachDB migration runner."""

from __future__ import annotations

from typing import Any
import hashlib
from pathlib import Path
from collections.abc import Callable

import pytest
import psycopg

from services.document_ai.migrations.cockroachdb import runner


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(
        self, exc_type: object | None, exc: object | None, tb: object | None
    ) -> bool:
        del exc_type, exc, tb
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized_sql = " ".join(sql.lower().split())
        self._connection.executed_statements.append((sql, params))

        if "select current_database(), version(), current_user" in normalized_sql:
            self._connection.last_fetchone = (
                self._connection.current_database,
                self._connection.version_text,
                self._connection.current_user,
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
        if "create table if not exists document_ai_cockroachdb_schema_migrations" in normalized_sql:
            self._connection.ledger_table_created += 1
            self._connection.last_fetchone = None
            return
        if normalized_sql.startswith("insert into document_ai_cockroachdb_schema_migrations"):
            migration_name, checksum_sha256 = params or ("", "")
            self._connection.pending_ledger[migration_name] = str(checksum_sha256)
            self._connection.last_fetchone = None
            return
        if "fail_migration" in normalized_sql:
            raise psycopg.OperationalError("simulated migration failure")

        self._connection.last_fetchone = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._connection.last_fetchone


class _FakeTransaction:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeConnection:
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
        current_user: str = "hackathon_user",
        ledger: dict[str, str] | None = None,
    ) -> None:
        self.current_database = current_database
        self.version_text = version_text
        self.current_user = current_user
        self.executed_statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.ledger: dict[str, str] = dict(ledger or {})
        self.pending_ledger: dict[str, str] = {}
        self.ledger_table_created = 0
        self.transaction_depth = 0
        self.last_fetchone: tuple[Any, ...] | None = None
        self.rollback_calls = 0

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self, exc_type: object | None, exc: object | None, tb: object | None
    ) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_discover_migration_files_sorts_lexically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        tmp_path / "0002_document_ai_runtime.sql",
        tmp_path / "0001_document_ai_bootstrap.sql",
        tmp_path / "0003_document_ai_followup.sql",
    ]
    for file_path in files:
        file_path.write_text("select 1;", encoding="utf-8")

    monkeypatch.setattr(runner, "MIGRATIONS_DIRECTORY", tmp_path)

    discovered = runner.discover_migration_files()
    assert [path.name for path in discovered] == [
        "0001_document_ai_bootstrap.sql",
        "0002_document_ai_runtime.sql",
        "0003_document_ai_followup.sql",
    ]


def test_apply_migrations_runs_in_order_and_creates_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_files = _write_migration_files(
        tmp_path,
        {
            "0002_document_ai_runtime.sql": "SELECT 'runtime';",
            "0001_document_ai_bootstrap.sql": "SELECT 'bootstrap';",
        },
    )
    fake_connection = _FakeConnection()

    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    runner.apply_migrations("postgresql://example.invalid/kodi_dev", migration_files)

    assert fake_connection.ledger_table_created == 1
    executed_sql = [
        sql
        for sql, _ in fake_connection.executed_statements
        if "select 'bootstrap'" in sql.lower() or "select 'runtime'" in sql.lower()
    ]
    assert executed_sql == ["SELECT 'bootstrap';", "SELECT 'runtime';"]


def test_apply_migrations_skips_already_applied_migration_with_same_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    migration_file = tmp_path / "0001_document_ai_bootstrap.sql"
    sql_text = "SELECT 'bootstrap';"
    migration_file.write_text(sql_text, encoding="utf-8")
    fake_connection = _FakeConnection(
        ledger={"0001_document_ai_bootstrap.sql": hashlib.sha256(sql_text.encode()).hexdigest()}
    )

    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    runner.apply_migrations(
        "postgresql://example.invalid/kodi_dev",
        (migration_file,),
    )

    output = capsys.readouterr().out
    assert "Skipping already applied migration: 0001_document_ai_bootstrap.sql" in output
    assert fake_connection.transaction_depth == 0


def test_apply_migrations_rejects_checksum_drift_for_existing_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_file = tmp_path / "0001_document_ai_bootstrap.sql"
    migration_file.write_text("SELECT 'bootstrap';", encoding="utf-8")
    fake_connection = _FakeConnection(
        ledger={"0001_document_ai_bootstrap.sql": "different-checksum"}
    )

    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    with pytest.raises(runner.DocumentAIMigrationError) as error_info:
        runner.apply_migrations(
            "postgresql://example.invalid/kodi_dev",
            (migration_file,),
        )

    assert error_info.value.migration_file == migration_file
    assert "checksum differs" in error_info.value.message


def test_apply_migrations_rolls_back_failed_migration_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_files = _write_migration_files(
        tmp_path,
        {
            "0001_document_ai_bootstrap.sql": "SELECT 'bootstrap';",
            "0002_document_ai_runtime.sql": "SELECT 'fail_migration runtime';",
            "0003_document_ai_followup.sql": "SELECT 'later';",
        },
    )
    fake_connection = _FakeConnection()

    monkeypatch.setattr(runner.psycopg, "connect", _connect_factory(fake_connection))

    with pytest.raises(runner.DocumentAIMigrationError) as error_info:
        runner.apply_migrations("postgresql://example.invalid/kodi_dev", migration_files)

    assert error_info.value.migration_file.name == "0002_document_ai_runtime.sql"
    executed_sql = [sql for sql, _ in fake_connection.executed_statements]
    assert any("SELECT 'bootstrap';" == sql for sql in executed_sql)
    assert any("SELECT 'fail_migration runtime';" == sql for sql in executed_sql)
    assert not any("SELECT 'later';" == sql for sql in executed_sql)
    assert "0002_document_ai_runtime.sql" not in fake_connection.ledger
    assert "0003_document_ai_followup.sql" not in fake_connection.ledger


def test_validate_target_database_rejects_wrong_database() -> None:
    fake_connection = _FakeConnection(current_database="wrong_db")
    with pytest.raises(runner.DocumentAITargetError):
        runner._validate_target_database(fake_connection)  # type: ignore[arg-type]


def test_validate_target_database_rejects_non_cockroachdb() -> None:
    fake_connection = _FakeConnection(version_text="PostgreSQL 16.0")
    with pytest.raises(runner.DocumentAITargetError):
        runner._validate_target_database(fake_connection)  # type: ignore[arg-type]


def test_validate_target_database_rejects_wrong_user() -> None:
    fake_connection = _FakeConnection(current_user="other_user")
    with pytest.raises(runner.DocumentAITargetError):
        runner._validate_target_database(fake_connection)  # type: ignore[arg-type]


def _write_migration_files(tmp_path: Path, files: dict[str, str]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for name, contents in files.items():
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        paths.append(path)
    return tuple(sorted(paths, key=lambda item: item.name))


def _connect_factory(connection: _FakeConnection) -> Callable[..., _FakeConnection]:
    def _connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        return connection

    return _connect
