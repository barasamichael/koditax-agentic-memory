"""Persistent and in-memory execution-store primitives for orchestration."""

from __future__ import annotations

import os
import json
from typing import cast
from typing import Protocol
from typing import TypedDict
from pathlib import Path

import psycopg

from shared.determinism.input_hash import canonical_json_dumps

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"


class ActionExecutionStoreError(RuntimeError):
    """Represent deterministic persistence failure in execution store."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class ActionExecutionStoreRecord(TypedDict):
    """Represent one stored execution envelope record."""

    execution_id: str
    idempotency_key: str
    request_fingerprint: str
    envelope: dict[str, object]


class ActionExecutionStore(Protocol):
    """Describe deterministic storage for orchestration execution envelopes."""

    def get(self, idempotency_key: str) -> ActionExecutionStoreRecord | None:
        """Return the stored execution record for one idempotency key."""
        ...

    def put(self, record: ActionExecutionStoreRecord) -> None:
        """Persist one execution record deterministically."""
        ...

    def clear(self) -> None:
        """Reset stored execution records for deterministic test isolation."""
        ...


def load_database_url() -> str | None:
    """Load the DB URL from environment or local `.env` file."""

    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

    env_values = _read_env_values()
    direct_value = env_values.get(DATABASE_URL_ENV_VAR)
    if direct_value:
        return direct_value

    db_user = env_values.get(DB_USER_ENV_VAR)
    db_password = env_values.get(DB_PASSWORD_ENV_VAR)
    db_name = env_values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


class InMemoryActionExecutionStore:
    """Provide deterministic in-memory execution record storage."""

    def __init__(self) -> None:
        self._records: dict[str, ActionExecutionStoreRecord] = {}

    def get(self, idempotency_key: str) -> ActionExecutionStoreRecord | None:
        return self._records.get(idempotency_key)

    def put(self, record: ActionExecutionStoreRecord) -> None:
        existing = self._records.get(record["idempotency_key"])
        if (
            existing is not None
            and existing["request_fingerprint"] != record["request_fingerprint"]
        ):
            raise ActionExecutionStoreError(
                reason_code="execution_persistence_conflict",
                message="Execution record conflicts with existing idempotency fingerprint.",
            )
        self._records[record["idempotency_key"]] = record

    def clear(self) -> None:
        self._records.clear()


class PersistentActionExecutionStore:
    """Persist orchestration execution records in PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get(self, idempotency_key: str) -> ActionExecutionStoreRecord | None:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT execution_id, idempotency_key, request_fingerprint, envelope
                        FROM orchestration_execution_records
                        WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise ActionExecutionStoreError(
                reason_code="execution_persistence_unavailable",
                message="Orchestration execution persistence is unavailable.",
            ) from error

        if row is None:
            return None

        return {
            "execution_id": cast(str, row[0]),
            "idempotency_key": cast(str, row[1]),
            "request_fingerprint": cast(str, row[2]),
            "envelope": _coerce_json_object(row[3]),
        }

    def put(self, record: ActionExecutionStoreRecord) -> None:
        envelope = record["envelope"]
        envelope_json = canonical_json_dumps(envelope)
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO orchestration_execution_records (
                            execution_id,
                            idempotency_key,
                            request_fingerprint,
                            correlation_id,
                            trace_id,
                            action_type,
                            route_id,
                            target_service,
                            target_operation,
                            plan_id,
                            plan_version,
                            plan_status,
                            tenant_id,
                            user_id,
                            execution_status,
                            envelope
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                        )
                        """,
                        (
                            record["execution_id"],
                            record["idempotency_key"],
                            record["request_fingerprint"],
                            _required_nested_string(envelope, "correlation_id"),
                            _required_nested_string(
                                _required_nested_object(envelope, "trace"),
                                "trace_id",
                            ),
                            _required_nested_string(
                                _required_nested_object(envelope, "action_context"),
                                "action_type",
                            ),
                            _optional_nested_string(
                                _required_nested_object(envelope, "action_context"),
                                "route_id",
                            ),
                            _optional_nested_string(
                                _required_nested_object(envelope, "action_context"),
                                "target_service",
                            ),
                            _optional_nested_string(
                                _required_nested_object(envelope, "action_context"),
                                "target_operation",
                            ),
                            _required_nested_string(
                                _required_nested_object(envelope, "plan"),
                                "plan_id",
                            ),
                            _required_nested_string(
                                _required_nested_object(envelope, "plan"),
                                "plan_version",
                            ),
                            _required_nested_string(
                                _required_nested_object(envelope, "plan"),
                                "plan_status",
                            ),
                            _optional_nested_string(
                                _required_nested_object(envelope, "action_context"),
                                "tenant_id",
                            ),
                            _optional_nested_string(
                                _required_nested_object(envelope, "action_context"),
                                "user_id",
                            ),
                            _required_nested_string(envelope, "execution_status"),
                            envelope_json,
                        ),
                    )
                connection.commit()
        except psycopg.errors.UniqueViolation as error:
            existing = self.get(record["idempotency_key"])
            if (
                existing is not None
                and existing["request_fingerprint"] == record["request_fingerprint"]
            ):
                return
            raise ActionExecutionStoreError(
                reason_code="execution_persistence_conflict",
                message="Execution record conflicts with existing idempotency fingerprint.",
            ) from error
        except psycopg.Error as error:
            raise ActionExecutionStoreError(
                reason_code="execution_persistence_unavailable",
                message="Orchestration execution persistence is unavailable.",
            ) from error

    def clear(self) -> None:
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM orchestration_execution_records")
                connection.commit()
        except psycopg.Error as error:
            raise ActionExecutionStoreError(
                reason_code="execution_persistence_unavailable",
                message="Orchestration execution persistence is unavailable.",
            ) from error


def build_default_action_execution_store() -> ActionExecutionStore:
    """Build the default execution store with DB-backed persistence when available."""

    database_url = load_database_url()
    if not database_url:
        return InMemoryActionExecutionStore()
    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.orchestration_execution_records')")
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    return InMemoryActionExecutionStore()
    except psycopg.Error:
        return InMemoryActionExecutionStore()
    return PersistentActionExecutionStore(database_url=database_url)


def _read_env_values() -> dict[str, str]:
    env_file = Path(".env")
    if not env_file.exists():
        return {}
    try:
        raw_lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _coerce_json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        loaded = json.loads(value)
        assert isinstance(loaded, dict)
        return cast(dict[str, object], loaded)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _required_nested_object(source: dict[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, dict):
        raise ActionExecutionStoreError(
            reason_code="execution_persistence_invalid_payload",
            message="Execution envelope is missing required object fields for persistence.",
        )
    return cast(dict[str, object], value)


def _required_nested_string(source: dict[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if isinstance(value, str) and value:
        return value
    raise ActionExecutionStoreError(
        reason_code="execution_persistence_invalid_payload",
        message="Execution envelope is missing required string fields for persistence.",
    )


def _optional_nested_string(source: dict[str, object], field_name: str) -> str | None:
    value = source.get(field_name)
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise ActionExecutionStoreError(
        reason_code="execution_persistence_invalid_payload",
        message="Execution envelope contains invalid optional persistence fields.",
    )
