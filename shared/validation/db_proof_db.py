"""Create and drop isolated PostgreSQL proof databases."""

from __future__ import annotations

import os
from uuid import uuid4
from pathlib import Path
import argparse
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Mapping

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"


@dataclass(frozen=True)
class ProofDatabaseInfo:
    """Represent a created proof database and connection URL.

    :param name: Created proof database name.
    :param url: Connection URL for the created database.
    """

    name: str
    url: str


def create_proof_database() -> ProofDatabaseInfo:
    """Create an isolated proof database using .env connection settings.

    :return: Created proof database information.
    :raises RuntimeError: If connection configuration is missing or creation fails.
    """

    base_conninfo = _load_base_conninfo()
    database_name = _build_database_name()
    admin_conninfo = _conninfo_with_database(base_conninfo, "postgres")
    proof_conninfo = _conninfo_with_database(base_conninfo, database_name)

    with psycopg.connect(admin_conninfo, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)),
            )

    return ProofDatabaseInfo(
        name=database_name,
        url=proof_conninfo,
    )


def drop_proof_database(database_name: str) -> None:
    """Drop a proof database after terminating active sessions.

    :param database_name: Database name to drop.
    :return: None.
    :raises RuntimeError: If base connection configuration is missing.
    """

    base_conninfo = _load_base_conninfo()
    admin_conninfo = _conninfo_with_database(base_conninfo, "postgres")

    with psycopg.connect(admin_conninfo, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)),
            )


def main() -> int:
    """Run proof database CLI.

    :return: Process exit code.
    """

    parser = argparse.ArgumentParser(prog="python -m shared.validation.db_proof_db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("create")

    drop_parser = subparsers.add_parser("drop")
    drop_parser.add_argument("--name", required=True)

    args = parser.parse_args()

    try:
        if args.command == "create":
            proof_database = create_proof_database()
            print(f"DB name: {proof_database.name}")
            print(f"Proof DATABASE_URL: {proof_database.url}")
            return 0

        drop_proof_database(args.name)
        print(f"Dropped DB: {args.name}")
        return 0
    except Exception as error:  # pragma: no cover
        print(f"Proof DB command failed: {error}")
        return 1


def _load_base_conninfo() -> str:
    environment_values = _load_environment_values()
    database_url = _get_required_value(DATABASE_URL_ENV_VAR, environment_values)
    connection_parts = conninfo_to_dict(database_url)

    db_user = _get_optional_value(DB_USER_ENV_VAR, environment_values)
    if db_user is not None:
        connection_parts["user"] = db_user

    db_password = _get_optional_value(DB_PASSWORD_ENV_VAR, environment_values)
    if db_password is not None:
        connection_parts["password"] = db_password

    connection_parts.pop("conninfo", None)
    return _build_conninfo_from_parts(connection_parts)


def _conninfo_with_database(base_conninfo: str, database_name: str) -> str:
    connection_parts = conninfo_to_dict(base_conninfo)
    connection_parts["dbname"] = database_name
    connection_parts.pop("conninfo", None)
    return _build_conninfo_from_parts(connection_parts)


def _build_database_name() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    suffix = uuid4().hex[:6]
    return f"kodi_proof_{timestamp}_{suffix}"


def _build_conninfo_from_parts(connection_parts: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key, value in connection_parts.items():
        if value is None:
            continue

        value_text = str(value)
        value_text = value_text.replace("\\", "\\\\")
        value_text = value_text.replace("'", "\\'")
        parts.append(f"{key}='{value_text}'")

    return " ".join(parts)


def _load_environment_values() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = Path(".env")
    if not env_file.exists():
        return values

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip("\"'")

    return values


def _get_optional_value(key: str, environment_values: dict[str, str]) -> str | None:
    process_value = os.getenv(key)
    if process_value is not None and process_value.strip():
        return process_value.strip()

    file_value = environment_values.get(key)
    if file_value is None or not file_value.strip():
        return None
    return file_value.strip()


def _get_required_value(key: str, environment_values: dict[str, str]) -> str:
    value = _get_optional_value(key, environment_values)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required {key}.")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
