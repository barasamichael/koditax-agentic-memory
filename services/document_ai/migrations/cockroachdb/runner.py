"""Run the Document AI-only CockroachDB migration lane."""

from __future__ import annotations

import sys
from typing import Any
from typing import cast
from typing import LiteralString
import hashlib
from pathlib import Path
import argparse

import psycopg

from services.document_ai.app.persistence_support import load_document_ai_database_url

DOCUMENT_AI_DATABASE_NAME = "kodi_dev"
DOCUMENT_AI_DATABASE_ENGINE_TOKEN = "CockroachDB"
DOCUMENT_AI_SQL_USER = "hackathon_user"
MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent
MIGRATION_LEDGER_TABLE = "document_ai_cockroachdb_schema_migrations"


class DocumentAIMigrationError(RuntimeError):
    """Represent a sanitized Document AI migration failure."""

    def __init__(self, migration_file: Path, message: str) -> None:
        super().__init__(message)
        self.migration_file = migration_file
        self.message = message


class DocumentAITargetError(DocumentAIMigrationError):
    """Represent a sanitized target-database mismatch."""


def discover_migration_files() -> tuple[Path, ...]:
    """Discover Document AI CockroachDB SQL migrations in lexical order."""

    files = [path for path in MIGRATIONS_DIRECTORY.glob("*.sql") if path.is_file()]
    return tuple(sorted(files, key=lambda item: item.name))


def apply_migrations(database_url: str, migration_files: tuple[Path, ...]) -> None:
    """Apply Document AI CockroachDB migrations with immutable checksums."""

    ordered_files = tuple(sorted(migration_files, key=lambda item: item.name))

    with psycopg.connect(database_url, autocommit=True) as connection:
        _ensure_ledger_table(connection)
        _validate_target_database(connection)

        for migration_file in ordered_files:
            try:
                sql_text = migration_file.read_text(encoding="utf-8")
            except OSError as error:
                raise DocumentAIMigrationError(
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
                    raise DocumentAIMigrationError(
                        migration_file=migration_file,
                        message=(
                            "Migration file checksum differs from the already-applied "
                            "version recorded in document_ai_cockroachdb_schema_migrations."
                        ),
                    )
                print(f"Skipping already applied migration: {migration_file.name}")
                continue

            print(f"Applying migration: {migration_file.name}")
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
                raise DocumentAIMigrationError(
                    migration_file=migration_file,
                    message=str(error),
                ) from error


def main(argv: list[str] | tuple[str, ...] | None = ()) -> int:
    """Run the Document AI CockroachDB migration lane CLI."""

    parser = argparse.ArgumentParser(
        prog="python -m services.document_ai.migrations.cockroachdb.runner"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    del args

    database_url = load_document_ai_database_url()
    if database_url is None or not database_url.strip():
        print("Missing required DATABASE_URL environment variable.", file=sys.stderr)
        return 2

    migration_files = discover_migration_files()
    if not migration_files:
        print("No Document AI CockroachDB migrations found.")
        return 0

    try:
        apply_migrations(database_url=database_url, migration_files=migration_files)
    except DocumentAITargetError as error:
        print(str(error), file=sys.stderr)
        return 1
    except DocumentAIMigrationError as error:
        print(f"Migration failed: {error.migration_file.name} - {error.message}", file=sys.stderr)
        return 1
    except psycopg.Error:
        print("Document AI CockroachDB migration target rejected or unavailable.", file=sys.stderr)
        return 1
    except Exception as error:  # pragma: no cover
        print(f"Unexpected migration failure: {error}", file=sys.stderr)
        return 2

    print("Document AI CockroachDB migrations applied successfully.")
    return 0


def _ensure_ledger_table(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_LEDGER_TABLE} (
                migration_name TEXT PRIMARY KEY,
                checksum_sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def _load_applied_checksum(
    *,
    connection: psycopg.Connection[Any],
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


def _validate_target_database(connection: psycopg.Connection[Any]) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), version(), current_user")
        row = cursor.fetchone()
    if row is None:
        raise DocumentAITargetError(
            migration_file=Path("<connection>"),
            message="Unable to determine Document AI database target.",
        )

    current_database = str(row[0] or "")
    version_text = str(row[1] or "")
    current_user = str(row[2] or "")
    if current_database != DOCUMENT_AI_DATABASE_NAME:
        raise DocumentAITargetError(
            migration_file=Path("<connection>"),
            message="Document AI migration target rejected: expected database kodi_dev.",
        )
    if DOCUMENT_AI_DATABASE_ENGINE_TOKEN not in version_text:
        raise DocumentAITargetError(
            migration_file=Path("<connection>"),
            message="Document AI migration target rejected: expected CockroachDB.",
        )
    if current_user != DOCUMENT_AI_SQL_USER:
        raise DocumentAITargetError(
            migration_file=Path("<connection>"),
            message="Document AI migration target rejected: expected hackathon_user.",
        )
    return current_database


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
