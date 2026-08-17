from __future__ import annotations

from pathlib import Path

import pytest

from shared.validation.db_migrate import apply_migrations
from shared.validation.db_migrate import _migration_checksum
from shared.validation.db_migrate import select_migration_files
from shared.validation.db_migrate import MigrationExecutionError


class FakePsycopgError(Exception):
    """Stand in for psycopg.Error in focused migration-runner tests."""


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> FakeTransaction:
        self._connection.transaction_entries += 1
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._fetchone_result: tuple[object, ...] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        normalized_query = " ".join(query.split())
        self._connection.executed.append((normalized_query, params))
        if "CREATE TABLE IF NOT EXISTS schema_migrations" in normalized_query:
            return
        if normalized_query.startswith("SELECT checksum_sha256 FROM schema_migrations"):
            migration_name = str(params[0]) if params is not None else ""
            checksum = self._connection.applied_checksums.get(migration_name)
            self._fetchone_result = None if checksum is None else (checksum,)
            return
        if normalized_query.startswith("INSERT INTO schema_migrations"):
            assert params is not None
            migration_name = str(params[0])
            checksum = str(params[1])
            self._connection.applied_checksums[migration_name] = checksum
            return
        if self._connection.fail_on_sql and normalized_query == self._connection.fail_on_sql:
            raise FakePsycopgError("simulated migration failure")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._fetchone_result


class FakeConnection:
    def __init__(
        self,
        *,
        applied_checksums: dict[str, str] | None = None,
        fail_on_sql: str | None = None,
    ) -> None:
        self.applied_checksums = dict(applied_checksums or {})
        self.fail_on_sql = fail_on_sql
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.rollback_called = 0
        self.transaction_entries = 0

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def rollback(self) -> None:
        self.rollback_called += 1


def test_apply_migrations_records_new_migration_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration_file = tmp_path / "0001_example.sql"
    sql_text = "CREATE TABLE example_table (id INTEGER PRIMARY KEY);"
    migration_file.write_text(sql_text, encoding="utf-8")
    fake_connection = FakeConnection()

    monkeypatch.setattr(
        "shared.validation.db_migrate.psycopg.connect",
        lambda *_args, **_kwargs: fake_connection,
    )

    apply_migrations(
        database_url="postgresql://example",
        migration_files=(migration_file,),
        repo_root=tmp_path,
    )

    assert fake_connection.transaction_entries == 1
    assert fake_connection.applied_checksums == {
        "0001_example.sql": _migration_checksum(sql_text),
    }


def test_apply_migrations_skips_already_applied_migration_with_same_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration_file = tmp_path / "0001_example.sql"
    sql_text = "CREATE TABLE example_table (id INTEGER PRIMARY KEY);"
    migration_file.write_text(sql_text, encoding="utf-8")
    fake_connection = FakeConnection(
        applied_checksums={"0001_example.sql": _migration_checksum(sql_text)},
    )

    monkeypatch.setattr(
        "shared.validation.db_migrate.psycopg.connect",
        lambda *_args, **_kwargs: fake_connection,
    )

    apply_migrations(
        database_url="postgresql://example",
        migration_files=(migration_file,),
        repo_root=tmp_path,
    )

    output = capsys.readouterr().out
    assert "Skipping already applied migration: 0001_example.sql" in output
    assert fake_connection.transaction_entries == 0


def test_apply_migrations_rejects_checksum_drift_for_existing_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration_file = tmp_path / "0001_example.sql"
    migration_file.write_text(
        "CREATE TABLE example_table (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    fake_connection = FakeConnection(
        applied_checksums={"0001_example.sql": "different-checksum"},
    )

    monkeypatch.setattr(
        "shared.validation.db_migrate.psycopg.connect",
        lambda *_args, **_kwargs: fake_connection,
    )

    with pytest.raises(MigrationExecutionError) as error_info:
        apply_migrations(
            database_url="postgresql://example",
            migration_files=(migration_file,),
            repo_root=tmp_path,
        )

    assert error_info.value.migration_file == migration_file
    assert "checksum differs" in error_info.value.message


def test_apply_migrations_rolls_back_and_wraps_database_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration_file = tmp_path / "0001_example.sql"
    sql_text = "CREATE TABLE example_table (id INTEGER PRIMARY KEY);"
    migration_file.write_text(sql_text, encoding="utf-8")
    fake_connection = FakeConnection(fail_on_sql=sql_text)

    monkeypatch.setattr(
        "shared.validation.db_migrate.psycopg.connect",
        lambda *_args, **_kwargs: fake_connection,
    )
    monkeypatch.setattr(
        "shared.validation.db_migrate.psycopg.Error",
        FakePsycopgError,
    )

    with pytest.raises(MigrationExecutionError) as error_info:
        apply_migrations(
            database_url="postgresql://example",
            migration_files=(migration_file,),
            repo_root=tmp_path,
        )

    assert error_info.value.migration_file == migration_file
    assert error_info.value.message == "simulated migration failure"
    assert fake_connection.rollback_called == 1


def test_select_migration_files_returns_inclusive_requested_window(tmp_path: Path) -> None:
    migration_files = (
        tmp_path / "0016_alpha.sql",
        tmp_path / "0017_beta.sql",
        tmp_path / "0018_gamma.sql",
        tmp_path / "0027_delta.sql",
        tmp_path / "0028_epsilon.sql",
    )

    selected = select_migration_files(
        migration_files,
        start_from="0017",
        end_at="0027",
    )

    assert tuple(path.name for path in selected) == (
        "0017_beta.sql",
        "0018_gamma.sql",
        "0027_delta.sql",
    )


def test_select_migration_files_accepts_exact_filename_prefix_match(tmp_path: Path) -> None:
    migration_files = (
        tmp_path / "0017_knowledge_persistent_catalog_baseline.sql",
        tmp_path / "0018_knowledge_hybrid_retrieval_embeddings.sql",
        tmp_path / "0027_document_ai_extraction_execution_state.sql",
    )

    selected = select_migration_files(
        migration_files,
        start_from="0017_knowledge",
        end_at="0027_document_ai",
    )

    assert tuple(path.name for path in selected) == (
        "0017_knowledge_persistent_catalog_baseline.sql",
        "0018_knowledge_hybrid_retrieval_embeddings.sql",
        "0027_document_ai_extraction_execution_state.sql",
    )


def test_select_migration_files_rejects_missing_start_bound(tmp_path: Path) -> None:
    migration_files = (
        tmp_path / "0017_beta.sql",
        tmp_path / "0027_delta.sql",
    )

    with pytest.raises(ValueError) as error_info:
        select_migration_files(
            migration_files,
            start_from="0016",
            end_at="0027",
        )

    assert "start_from `0016` did not match any migration file" in str(error_info.value)


def test_select_migration_files_rejects_reversed_bounds(tmp_path: Path) -> None:
    migration_files = (
        tmp_path / "0017_beta.sql",
        tmp_path / "0027_delta.sql",
    )

    with pytest.raises(ValueError) as error_info:
        select_migration_files(
            migration_files,
            start_from="0027",
            end_at="0017",
        )

    assert "start_from resolves after end_at" in str(error_info.value)
