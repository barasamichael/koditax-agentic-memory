"""Run SQL migrations against PostgreSQL."""

from __future__ import annotations

import os
import sys
from typing import cast
from typing import LiteralString
import hashlib
from pathlib import Path
import argparse

import psycopg

DATABASE_URL_ENV_VAR = "DATABASE_URL"
MIGRATIONS_DIRECTORY = Path("database/migrations")
MIGRATION_LEDGER_TABLE = "schema_migrations"
MIGRATION_LEDGER_SQL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_LEDGER_TABLE} (
    migration_name TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationExecutionError(Exception):
    """Represent one migration execution failure.

    :param migration_file: SQL migration file that failed.
    :param message: Human-readable execution error message.
    """

    def __init__(self, migration_file: Path, message: str) -> None:
        super().__init__(message)
        self.migration_file = migration_file
        self.message = message


def discover_migration_files(repo_root: Path) -> tuple[Path, ...]:
    """Discover SQL migration files in filename order.

    :param repo_root: Repository root path.
    :return: Ordered SQL migration file paths.
    """

    migrations_path = repo_root / MIGRATIONS_DIRECTORY
    if not migrations_path.exists():
        return ()

    files = [file_path for file_path in migrations_path.glob("*.sql") if file_path.is_file()]
    return tuple(sorted(files, key=lambda item: item.name))


def select_migration_files(
    migration_files: tuple[Path, ...],
    *,
    start_from: str | None = None,
    end_at: str | None = None,
) -> tuple[Path, ...]:
    """Select a bounded inclusive migration window by filename prefix.

    :param migration_files: Ordered migration file paths.
    :param start_from: Inclusive starting filename prefix, e.g. ``0017``.
    :param end_at: Inclusive ending filename prefix, e.g. ``0027``.
    :return: Filtered ordered migration files.
    :raises ValueError: If the requested bounds are invalid or not found.
    """

    if start_from is None and end_at is None:
        return migration_files

    ordered_names = tuple(migration_file.name for migration_file in migration_files)
    start_index = 0
    end_index = len(migration_files) - 1

    if start_from is not None:
        start_index = _resolve_migration_bound_index(
            ordered_names,
            bound_value=start_from,
            label="start_from",
        )
    if end_at is not None:
        end_index = _resolve_migration_bound_index(
            ordered_names,
            bound_value=end_at,
            label="end_at",
        )
    if start_index > end_index:
        raise ValueError("Migration range is invalid: start_from resolves after end_at.")

    return migration_files[start_index : end_index + 1]


def apply_migrations(database_url: str, migration_files: tuple[Path, ...], repo_root: Path) -> None:
    """Apply SQL migration files in order, one transaction per file.

    :param database_url: Target PostgreSQL connection string.
    :param migration_files: Ordered SQL migration files.
    :param repo_root: Repository root path for readable logging.
    :return: None.
    :raises MigrationExecutionError: If any migration fails to read or execute.
    """

    with psycopg.connect(database_url, autocommit=True) as connection:
        _ensure_migration_ledger(connection)
        for migration_file in migration_files:
            relative_path = _relative_path(repo_root, migration_file)

            try:
                sql_text = migration_file.read_text(encoding="utf-8")
            except OSError as error:
                raise MigrationExecutionError(
                    migration_file=migration_file,
                    message=f"Unable to read migration file: {error}.",
                ) from error

            checksum_sha256 = _migration_checksum(sql_text)
            applied_checksum = _load_applied_checksum(
                connection=connection,
                migration_name=migration_file.name,
            )
            if applied_checksum is not None:
                if applied_checksum != checksum_sha256:
                    raise MigrationExecutionError(
                        migration_file=migration_file,
                        message=(
                            "Migration file checksum differs from the already-applied "
                            "version recorded in schema_migrations."
                        ),
                    )
                print(f"Skipping already applied migration: {relative_path}")
                continue

            print(f"Applying migration: {relative_path}")
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(cast(LiteralString, sql_text))
                        cursor.execute(
                            f"""
                            INSERT INTO {MIGRATION_LEDGER_TABLE} (
                                migration_name,
                                checksum_sha256
                            )
                            VALUES (%s, %s)
                            """,
                            (migration_file.name, checksum_sha256),
                        )
            except psycopg.Error as error:
                connection.rollback()
                raise MigrationExecutionError(
                    migration_file=migration_file,
                    message=str(error),
                ) from error


def main() -> int:
    """Execute migration runner CLI.

    :return: Process exit code.
    """

    parser = argparse.ArgumentParser(prog="python -m shared.validation.db_migrate")
    parser.add_argument(
        "--start-from",
        dest="start_from",
        help="Inclusive migration filename prefix to start from, e.g. 0017 or 0017_knowledge.",
    )
    parser.add_argument(
        "--end-at",
        dest="end_at",
        help="Inclusive migration filename prefix to stop at, e.g. 0027 or 0027_document_ai.",
    )
    args = parser.parse_args()

    database_url = _load_database_url()
    if database_url is None or not database_url.strip():
        print(
            f"Missing required {DATABASE_URL_ENV_VAR} environment variable.",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(__file__).resolve().parents[2]
    all_migration_files = discover_migration_files(repo_root)
    try:
        migration_files = select_migration_files(
            all_migration_files,
            start_from=args.start_from,
            end_at=args.end_at,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not migration_files:
        print("No SQL migrations found.")
        return 0

    try:
        apply_migrations(
            database_url=database_url,
            migration_files=migration_files,
            repo_root=repo_root,
        )
    except MigrationExecutionError as error:
        relative_path = _relative_path(repo_root, error.migration_file)
        print(f"Migration failed: {relative_path} - {error.message}", file=sys.stderr)
        return 1
    except Exception as error:  # pragma: no cover
        print(f"Unexpected migration failure: {error}", file=sys.stderr)
        return 2

    print("Migrations applied successfully.")
    return 0


def _ensure_migration_ledger(connection: psycopg.Connection[object]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(cast(LiteralString, MIGRATION_LEDGER_SQL))


def _load_applied_checksum(
    *,
    connection: psycopg.Connection[object],
    migration_name: str,
) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT checksum_sha256
            FROM {MIGRATION_LEDGER_TABLE}
            WHERE migration_name = %s
            """,
            (migration_name,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


def _migration_checksum(sql_text: str) -> str:
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def _resolve_migration_bound_index(
    migration_names: tuple[str, ...],
    *,
    bound_value: str,
    label: str,
) -> int:
    normalized = bound_value.strip()
    if not normalized:
        raise ValueError(f"Migration range {label} cannot be blank.")

    matches = [
        index
        for index, migration_name in enumerate(migration_names)
        if migration_name.startswith(normalized)
    ]
    if not matches:
        raise ValueError(
            f"Migration range {label} `{normalized}` did not match any migration file."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Migration range {label} `{normalized}` matched multiple migration files."
        )
    return matches[0]


def _relative_path(repo_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _load_database_url() -> str | None:
    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

    env_file = Path(".env")
    if not env_file.exists():
        return None

    try:
        env_lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in env_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(f"{DATABASE_URL_ENV_VAR}="):
            continue

        parsed_value = line.split("=", maxsplit=1)[1].strip().strip("\"'")
        return parsed_value or None

    return None


if __name__ == "__main__":
    raise SystemExit(main())
