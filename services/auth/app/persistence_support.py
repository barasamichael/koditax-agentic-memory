"""Shared CockroachDB persistence helpers for auth runtime slices."""

from __future__ import annotations

import os
import time
import random
from typing import Any
from typing import Literal
from typing import TypeVar
import logging
from pathlib import Path
from datetime import datetime
from contextlib import suppress
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable
from collections.abc import Iterator

import psycopg

from services.auth.app.config import get_auth_secret_runtime_mode
from services.auth.app.metrics import AuthMetricsEmitter
from services.auth.app.metrics import get_default_auth_metrics_emitter
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_RETRY_TOTAL
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_PERSISTENCE_TRANSACTION_AMBIGUOUS_TOTAL
from shared.determinism.input_hash import canonical_json_dumps

DATABASE_URL_ENV_VAR = "DATABASE_URL"
AUTH_DATABASE_NAME = "kodi_dev"
AUTH_DATABASE_ENGINE_REQUIREMENT = "CockroachDB"
AUTH_DATABASE_CONNECT_TIMEOUT_SECONDS = 15
AUTH_TRANSACTION_MAX_ATTEMPTS_DEFAULT = 3
AUTH_TRANSACTION_MAX_ATTEMPTS_CAP = 5
AUTH_TRANSACTION_RETRY_BASE_DELAY_SECONDS = 0.025
AUTH_TRANSACTION_RETRY_MAX_DELAY_SECONDS = 0.1
AUTH_TRANSACTION_RETRY_JITTER_MAX_SECONDS = 0.01
LOGGER = logging.getLogger("auth.persistence")
T = TypeVar("T")

AuthPersistenceStatus = Literal["ready", "unavailable", "schema_mismatch"]
AuthDatabaseValidationReason = Literal[
    "ready",
    "database_unreachable",
    "wrong_database",
    "wrong_database_engine",
    "database_validation_failed",
]


@dataclass(frozen=True)
class AuthPersistenceConfig:
    """Represent auth persistence runtime configuration."""

    database_url: str | None
    database_engine_requirement: str
    persistence_required: bool
    connect_timeout_seconds: int


@dataclass(frozen=True)
class AuthDatabaseValidationResult:
    """Represent sanitized validation data for auth persistence readiness."""

    ready: bool
    reason: AuthDatabaseValidationReason
    current_database: str | None = None
    engine: str | None = None
    current_user: str | None = None
    current_timestamp: datetime | None = None


@dataclass(frozen=True)
class AuthCockroachTransactionRetryPolicy:
    """Represent bounded CockroachDB transaction retry behavior."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.1
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 1.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("auth_cockroach_retry_max_attempts_must_be_positive")
        if self.base_delay_seconds <= 0:
            raise ValueError("auth_cockroach_retry_base_delay_must_be_positive")
        if self.backoff_multiplier < 1:
            raise ValueError("auth_cockroach_retry_backoff_multiplier_must_be_at_least_one")
        if self.max_delay_seconds <= 0:
            raise ValueError("auth_cockroach_retry_max_delay_must_be_positive")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("auth_cockroach_retry_jitter_ratio_must_be_between_zero_and_one")


@dataclass(frozen=True)
class AuthCockroachTransactionExecutionResult:
    """Represent deterministic transaction execution metadata."""

    attempt_count: int
    retry_delays_seconds: tuple[float, ...]
    reconciled: bool


class AuthCockroachTransactionError(RuntimeError):
    """Represent deterministic auth CockroachDB transaction failures."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        sqlstate: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.sqlstate = sqlstate
        self.details = details or {}


class AuthCockroachTransactionUnavailableError(AuthCockroachTransactionError):
    """Represent unavailable auth persistence during CockroachDB execution."""


class AuthCockroachTransactionRetryExhaustedError(AuthCockroachTransactionError):
    """Represent exhausted retry attempts for serialization failures."""


class AuthCockroachTransactionAmbiguousCommitError(AuthCockroachTransactionError):
    """Represent durable state that could not be reconciled safely."""


class AuthCockroachTransactionSqlError(AuthCockroachTransactionError):
    """Represent non-retryable CockroachDB SQL failures."""


def get_auth_persistence_config() -> AuthPersistenceConfig:
    """Return the typed auth persistence configuration boundary."""

    return AuthPersistenceConfig(
        database_url=load_auth_database_url(),
        database_engine_requirement=AUTH_DATABASE_ENGINE_REQUIREMENT,
        persistence_required=auth_runtime_requires_persistence(),
        connect_timeout_seconds=AUTH_DATABASE_CONNECT_TIMEOUT_SECONDS,
    )


def auth_runtime_requires_persistence() -> bool:
    """Return whether auth should fail closed to persistent storage."""

    return get_auth_secret_runtime_mode() in {"hackathon", "production"}


def load_auth_database_url() -> str | None:
    """Load the CockroachDB connection URL from env or local `.env`."""

    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None:
        normalized_value = env_value.strip()
        return normalized_value or None

    env_values = _read_env_values()
    direct_value = env_values.get(DATABASE_URL_ENV_VAR)
    if direct_value is None:
        return None
    normalized_value = direct_value.strip()
    return normalized_value or None


def open_auth_database_connection(database_url: str) -> psycopg.Connection[Any]:
    """Open one Psycopg 3 connection with bounded timeout."""

    return psycopg.connect(
        database_url,
        connect_timeout=AUTH_DATABASE_CONNECT_TIMEOUT_SECONDS,
    )


def connect_auth_database(database_url: str) -> psycopg.Connection[Any]:
    """Backward-compatible auth database connection factory."""

    return open_auth_database_connection(database_url)


def execute_auth_transaction(
    *,
    database_url: str,
    operation_name: str,
    operation: Callable[[psycopg.Connection[Any]], T],
    reconcile_ambiguous_result: Callable[[psycopg.Connection[Any]], T | None] | None = None,
    max_attempts: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[], float] = random.random,
) -> T:
    """Execute one auth persistence transaction callback.

    The callback may only perform SQL, deterministic calculation, serialization,
    comparison, and database-result construction. It must not commit, roll back,
    close the connection, or call external providers.
    """

    resolved_operation_name = operation_name.strip()
    if not resolved_operation_name:
        raise ValueError("auth_transaction_operation_name_must_be_non_empty")

    resolved_max_attempts = _resolve_auth_transaction_max_attempts(max_attempts)

    for attempt_number in range(1, resolved_max_attempts + 1):
        connection: psycopg.Connection[Any] | None = None
        try:
            connection = open_auth_database_connection(database_url)
        except psycopg.Error as error:
            sqlstate = _extract_sqlstate(error)
            _emit_auth_transaction_event(
                event_status="failed",
                operation_name=resolved_operation_name,
                attempt_number=attempt_number,
                maximum_attempts=resolved_max_attempts,
                sqlstate=sqlstate,
            )
            raise AuthCockroachTransactionUnavailableError(
                reason_code="auth_transaction_unavailable",
                message="Auth CockroachDB transaction could not open a connection.",
                sqlstate=sqlstate,
                details={
                    "operation_name": resolved_operation_name,
                    "attempt_number": attempt_number,
                    "maximum_attempts": resolved_max_attempts,
                },
            ) from error

        try:
            try:
                result = operation(connection)
            except Exception as error:  # noqa: BLE001
                sqlstate = _extract_sqlstate(error)
                sqlstate_class = _classify_auth_transaction_sqlstate(sqlstate)
                if sqlstate_class == "retryable_serialization_failure":
                    with suppress(Exception):
                        connection.rollback()
                    if attempt_number >= resolved_max_attempts:
                        _emit_auth_transaction_event(
                            event_status="failed",
                            operation_name=resolved_operation_name,
                            attempt_number=attempt_number,
                            maximum_attempts=resolved_max_attempts,
                            sqlstate=sqlstate,
                        )
                        raise AuthCockroachTransactionRetryExhaustedError(
                            reason_code="auth_transaction_retry_exhausted",
                            message="Auth CockroachDB transaction retries were exhausted.",
                            sqlstate=sqlstate,
                            details={
                                "operation_name": resolved_operation_name,
                                "attempt_number": attempt_number,
                                "maximum_attempts": resolved_max_attempts,
                            },
                        ) from error

                    retry_delay_seconds = _compute_auth_transaction_retry_delay_seconds(
                        attempt_number=attempt_number,
                        jitter_fn=jitter_fn,
                    )
                    _emit_auth_transaction_event(
                        event_status="retrying",
                        operation_name=resolved_operation_name,
                        attempt_number=attempt_number,
                        maximum_attempts=resolved_max_attempts,
                        sqlstate=sqlstate,
                    )
                    sleep_fn(retry_delay_seconds)
                    continue

                if sqlstate_class == "ambiguous_transaction_result":
                    with suppress(Exception):
                        connection.rollback()
                    _emit_auth_transaction_reconciliation_event(
                        event_name="auth transaction ambiguous result",
                        operation_name=resolved_operation_name,
                        sqlstate=sqlstate,
                    )
                    _close_auth_connection(connection)
                    connection = None
                    return _reconcile_auth_transaction_ambiguous_result(
                        database_url=database_url,
                        operation_name=resolved_operation_name,
                        reconcile_ambiguous_result=reconcile_ambiguous_result,
                        sqlstate=sqlstate,
                    )

                with suppress(Exception):
                    connection.rollback()

                if isinstance(error, psycopg.OperationalError):
                    _emit_auth_transaction_event(
                        event_status="failed",
                        operation_name=resolved_operation_name,
                        attempt_number=attempt_number,
                        maximum_attempts=resolved_max_attempts,
                        sqlstate=sqlstate,
                    )
                    raise AuthCockroachTransactionUnavailableError(
                        reason_code="auth_transaction_unavailable",
                        message="Auth CockroachDB transaction became unavailable.",
                        sqlstate=sqlstate,
                        details={
                            "operation_name": resolved_operation_name,
                            "attempt_number": attempt_number,
                            "maximum_attempts": resolved_max_attempts,
                        },
                    ) from error

                if isinstance(error, psycopg.Error):
                    _emit_auth_transaction_event(
                        event_status="failed",
                        operation_name=resolved_operation_name,
                        attempt_number=attempt_number,
                        maximum_attempts=resolved_max_attempts,
                        sqlstate=sqlstate,
                    )
                    raise AuthCockroachTransactionSqlError(
                        reason_code="auth_transaction_sql_error",
                        message="Auth CockroachDB transaction failed.",
                        sqlstate=sqlstate,
                        details={
                            "operation_name": resolved_operation_name,
                            "attempt_number": attempt_number,
                            "maximum_attempts": resolved_max_attempts,
                        },
                    ) from error

                _emit_auth_transaction_event(
                    event_status="failed",
                    operation_name=resolved_operation_name,
                    attempt_number=attempt_number,
                    maximum_attempts=resolved_max_attempts,
                    sqlstate=None,
                )
                raise

            try:
                connection.commit()
            except Exception as error:  # noqa: BLE001
                sqlstate = _extract_sqlstate(error)
                sqlstate_class = _classify_auth_transaction_sqlstate(sqlstate)
                if sqlstate_class == "retryable_serialization_failure":
                    with suppress(Exception):
                        connection.rollback()
                    if attempt_number >= resolved_max_attempts:
                        _emit_auth_transaction_event(
                            event_status="failed",
                            operation_name=resolved_operation_name,
                            attempt_number=attempt_number,
                            maximum_attempts=resolved_max_attempts,
                            sqlstate=sqlstate,
                        )
                        raise AuthCockroachTransactionRetryExhaustedError(
                            reason_code="auth_transaction_retry_exhausted",
                            message="Auth CockroachDB transaction retries were exhausted.",
                            sqlstate=sqlstate,
                            details={
                                "operation_name": resolved_operation_name,
                                "attempt_number": attempt_number,
                                "maximum_attempts": resolved_max_attempts,
                            },
                        ) from error

                    retry_delay_seconds = _compute_auth_transaction_retry_delay_seconds(
                        attempt_number=attempt_number,
                        jitter_fn=jitter_fn,
                    )
                    _emit_auth_transaction_event(
                        event_status="retrying",
                        operation_name=resolved_operation_name,
                        attempt_number=attempt_number,
                        maximum_attempts=resolved_max_attempts,
                        sqlstate=sqlstate,
                    )
                    sleep_fn(retry_delay_seconds)
                    continue

                if sqlstate_class == "ambiguous_transaction_result":
                    with suppress(Exception):
                        connection.rollback()
                    _emit_auth_transaction_reconciliation_event(
                        event_name="auth transaction ambiguous result",
                        operation_name=resolved_operation_name,
                        sqlstate=sqlstate,
                    )
                    _close_auth_connection(connection)
                    connection = None
                    return _reconcile_auth_transaction_ambiguous_result(
                        database_url=database_url,
                        operation_name=resolved_operation_name,
                        reconcile_ambiguous_result=reconcile_ambiguous_result,
                        sqlstate=sqlstate,
                    )

                with suppress(Exception):
                    connection.rollback()

                if isinstance(error, psycopg.OperationalError):
                    _emit_auth_transaction_event(
                        event_status="failed",
                        operation_name=resolved_operation_name,
                        attempt_number=attempt_number,
                        maximum_attempts=resolved_max_attempts,
                        sqlstate=sqlstate,
                    )
                    raise AuthCockroachTransactionUnavailableError(
                        reason_code="auth_transaction_unavailable",
                        message="Auth CockroachDB transaction became unavailable.",
                        sqlstate=sqlstate,
                        details={
                            "operation_name": resolved_operation_name,
                            "attempt_number": attempt_number,
                            "maximum_attempts": resolved_max_attempts,
                        },
                    ) from error

                if isinstance(error, psycopg.Error):
                    _emit_auth_transaction_event(
                        event_status="failed",
                        operation_name=resolved_operation_name,
                        attempt_number=attempt_number,
                        maximum_attempts=resolved_max_attempts,
                        sqlstate=sqlstate,
                    )
                    raise AuthCockroachTransactionSqlError(
                        reason_code="auth_transaction_sql_error",
                        message="Auth CockroachDB transaction failed.",
                        sqlstate=sqlstate,
                        details={
                            "operation_name": resolved_operation_name,
                            "attempt_number": attempt_number,
                            "maximum_attempts": resolved_max_attempts,
                        },
                    ) from error

                _emit_auth_transaction_event(
                    event_status="failed",
                    operation_name=resolved_operation_name,
                    attempt_number=attempt_number,
                    maximum_attempts=resolved_max_attempts,
                    sqlstate=None,
                )
                raise
            else:
                _emit_auth_transaction_event(
                    event_status="succeeded",
                    operation_name=resolved_operation_name,
                    attempt_number=attempt_number,
                    maximum_attempts=resolved_max_attempts,
                    sqlstate=None,
                )
                return result
        finally:
            _close_auth_connection(connection)

    raise AuthCockroachTransactionRetryExhaustedError(
        reason_code="auth_transaction_retry_exhausted",
        message="Auth CockroachDB transaction retries were exhausted.",
        details={
            "operation_name": resolved_operation_name,
            "maximum_attempts": resolved_max_attempts,
        },
    )


def execute_auth_database_transaction(
    *,
    database_url: str,
    transaction_callback: Callable[[psycopg.Connection[Any]], T],
    reconcile_callback: Callable[[], T] | None = None,
    retry_policy: AuthCockroachTransactionRetryPolicy | None = None,
    retry_jitter: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    metrics_emitter: AuthMetricsEmitter | None = None,
) -> T:
    """Execute one CockroachDB transaction with bounded retry and reconciliation."""

    resolved_retry_policy = (
        retry_policy if retry_policy is not None else AuthCockroachTransactionRetryPolicy()
    )
    resolved_metrics_emitter = (
        metrics_emitter if metrics_emitter is not None else get_default_auth_metrics_emitter()
    )
    retry_delays: list[float] = []

    for attempt_count in range(1, resolved_retry_policy.max_attempts + 1):
        connection: psycopg.Connection[Any] | None = None
        try:
            connection = open_auth_database_connection(database_url)
        except psycopg.OperationalError as error:
            _emit_transaction_event(
                event_status="failed",
                reason_code="auth_persistence_unavailable",
                attempt_count=attempt_count,
                sqlstate=_extract_sqlstate(error),
                retry_delays_seconds=tuple(retry_delays),
                reconciled=False,
            )
            resolved_metrics_emitter.increment_counter_non_blocking(
                AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL,
                dimensions={"reason_code": "auth_persistence_unavailable"},
            )
            raise AuthCockroachTransactionUnavailableError(
                reason_code="auth_persistence_unavailable",
                message="Auth CockroachDB persistence is unavailable.",
                sqlstate=_extract_sqlstate(error),
            ) from error

        try:
            result = transaction_callback(connection)
            connection.commit()
        except psycopg.Error as error:
            sqlstate = _extract_sqlstate(error)
            if sqlstate == "40001":
                _cleanup_connection(connection=connection, should_rollback=True)
                if attempt_count >= resolved_retry_policy.max_attempts:
                    _emit_transaction_event(
                        event_status="failed",
                        reason_code="auth_persistence_retry_exhausted",
                        attempt_count=attempt_count,
                        sqlstate=sqlstate,
                        retry_delays_seconds=tuple(retry_delays),
                        reconciled=False,
                    )
                    resolved_metrics_emitter.increment_counter_non_blocking(
                        AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL,
                        dimensions={"reason_code": "auth_persistence_retry_exhausted"},
                    )
                    raise AuthCockroachTransactionRetryExhaustedError(
                        reason_code="auth_persistence_retry_exhausted",
                        message="Auth CockroachDB transaction retries were exhausted.",
                        sqlstate=sqlstate,
                        details={
                            "attempt_count": attempt_count,
                            "retry_delays_seconds": tuple(retry_delays),
                        },
                    ) from error

                retry_delay_seconds = _compute_retry_delay_seconds(
                    attempt_count=attempt_count,
                    retry_policy=resolved_retry_policy,
                    retry_jitter=retry_jitter,
                )
                retry_delays.append(retry_delay_seconds)
                _emit_transaction_event(
                    event_status="retrying",
                    reason_code="serialization_failure",
                    attempt_count=attempt_count,
                    sqlstate=sqlstate,
                    retry_delays_seconds=tuple(retry_delays),
                    reconciled=False,
                    retry_delay_seconds=retry_delay_seconds,
                )
                resolved_metrics_emitter.increment_counter_non_blocking(
                    AUTH_PERSISTENCE_TRANSACTION_RETRY_TOTAL,
                    dimensions={"reason_code": "40001"},
                )
                sleep(retry_delay_seconds)
                continue
            if sqlstate == "40003":
                _cleanup_connection(connection=connection, should_rollback=True)
                _emit_transaction_event(
                    event_status="reconciling",
                    reason_code="ambiguous_commit",
                    attempt_count=attempt_count,
                    sqlstate=sqlstate,
                    retry_delays_seconds=tuple(retry_delays),
                    reconciled=False,
                )
                if reconcile_callback is None:
                    resolved_metrics_emitter.increment_counter_non_blocking(
                        AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL,
                        dimensions={"reason_code": "ambiguous_commit_unreconciled"},
                    )
                    raise AuthCockroachTransactionAmbiguousCommitError(
                        reason_code="auth_persistence_ambiguous_commit",
                        message="Auth CockroachDB transaction outcome is ambiguous.",
                        sqlstate=sqlstate,
                        details={"attempt_count": attempt_count},
                    ) from error
                try:
                    reconciled_result = reconcile_callback()
                except Exception as reconcile_error:  # noqa: BLE001
                    resolved_metrics_emitter.increment_counter_non_blocking(
                        AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL,
                        dimensions={"reason_code": "ambiguous_commit_unreconciled"},
                    )
                    _emit_transaction_event(
                        event_status="failed",
                        reason_code="ambiguous_commit_unreconciled",
                        attempt_count=attempt_count,
                        sqlstate=sqlstate,
                        retry_delays_seconds=tuple(retry_delays),
                        reconciled=False,
                    )
                    raise AuthCockroachTransactionAmbiguousCommitError(
                        reason_code="auth_persistence_ambiguous_commit",
                        message="Auth CockroachDB transaction reconciliation failed.",
                        sqlstate=sqlstate,
                        details={"attempt_count": attempt_count},
                    ) from reconcile_error
                resolved_metrics_emitter.increment_counter_non_blocking(
                    AUTH_PERSISTENCE_TRANSACTION_AMBIGUOUS_TOTAL,
                    dimensions={"reason_code": "reconciled"},
                )
                resolved_metrics_emitter.increment_counter_non_blocking(
                    AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL,
                    dimensions={"reason_code": "ambiguous_commit_reconciled"},
                )
                _emit_transaction_event(
                    event_status="succeeded",
                    reason_code="ambiguous_commit_reconciled",
                    attempt_count=attempt_count,
                    sqlstate=sqlstate,
                    retry_delays_seconds=tuple(retry_delays),
                    reconciled=True,
                    )
                return reconciled_result

            if isinstance(error, psycopg.OperationalError):
                _cleanup_connection(connection=connection, should_rollback=True)
                _emit_transaction_event(
                    event_status="failed",
                    reason_code="auth_persistence_unavailable",
                    attempt_count=attempt_count,
                    sqlstate=sqlstate,
                    retry_delays_seconds=tuple(retry_delays),
                    reconciled=False,
                )
                resolved_metrics_emitter.increment_counter_non_blocking(
                    AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL,
                    dimensions={"reason_code": "auth_persistence_unavailable"},
                )
                raise AuthCockroachTransactionUnavailableError(
                    reason_code="auth_persistence_unavailable",
                    message="Auth CockroachDB persistence is unavailable.",
                    sqlstate=sqlstate,
                ) from error

            _cleanup_connection(connection=connection, should_rollback=True)
            resolved_metrics_emitter.increment_counter_non_blocking(
                AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL,
                dimensions={"reason_code": "sql_error"},
            )
            _emit_transaction_event(
                event_status="failed",
                reason_code="sql_error",
                attempt_count=attempt_count,
                sqlstate=sqlstate,
                retry_delays_seconds=tuple(retry_delays),
                reconciled=False,
            )
            raise AuthCockroachTransactionSqlError(
                reason_code="auth_persistence_sql_error",
                message="Auth CockroachDB transaction failed.",
                sqlstate=sqlstate,
                details={"attempt_count": attempt_count},
            ) from error
        except Exception:
            _cleanup_connection(connection=connection, should_rollback=True)
            raise
        else:
            resolved_metrics_emitter.increment_counter_non_blocking(
                AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL,
                dimensions={"reason_code": "success"},
            )
            _emit_transaction_event(
                event_status="succeeded",
                reason_code="transaction_committed",
                attempt_count=attempt_count,
                sqlstate=None,
                retry_delays_seconds=tuple(retry_delays),
                reconciled=False,
            )
            return result
        finally:
            _close_auth_connection(connection)

    raise AuthCockroachTransactionRetryExhaustedError(
        reason_code="auth_persistence_retry_exhausted",
        message="Auth CockroachDB transaction retries were exhausted.",
        details={"retry_delays_seconds": tuple(retry_delays)},
    )


@contextmanager
def auth_database_transaction(
    database_url: str,
) -> Iterator[psycopg.Connection[Any]]:
    """Provide one reusable auth database transaction boundary."""

    connection: psycopg.Connection[Any] | None = None
    try:
        connection = open_auth_database_connection(database_url)
        yield connection
        connection.commit()
    except Exception:
        if connection is not None:
            with suppress(Exception):
                connection.rollback()
        raise
    finally:
        if connection is not None:
            with suppress(Exception):
                connection.close()


def validate_auth_database_connection(
    database_url: str,
) -> AuthDatabaseValidationResult:
    """Validate the configured auth database without exposing secrets."""

    connection: psycopg.Connection[Any] | None = None
    try:
        connection = open_auth_database_connection(database_url)
        with connection.cursor() as cursor:
            cursor.execute("SELECT version()")
            version_row = cursor.fetchone()
            cursor.execute("SELECT current_database()")
            database_row = cursor.fetchone()
            cursor.execute("SELECT current_user")
            user_row = cursor.fetchone()
            cursor.execute("SELECT now()")
            now_row = cursor.fetchone()

        version_value = _safe_first_text(version_row)
        current_database = _safe_first_text(database_row)
        current_user = _safe_first_text(user_row)
        current_timestamp = _safe_first_datetime(now_row)

        if current_database != AUTH_DATABASE_NAME:
            return AuthDatabaseValidationResult(
                ready=False,
                reason="wrong_database",
                current_database=current_database,
                engine=version_value,
                current_user=current_user,
                current_timestamp=current_timestamp,
            )
        if AUTH_DATABASE_ENGINE_REQUIREMENT not in version_value:
            return AuthDatabaseValidationResult(
                ready=False,
                reason="wrong_database_engine",
                current_database=current_database,
                engine=version_value,
                current_user=current_user,
                current_timestamp=current_timestamp,
            )
        if not current_user.strip() or current_timestamp is None:
            return AuthDatabaseValidationResult(
                ready=False,
                reason="database_validation_failed",
                current_database=current_database,
                engine=version_value,
                current_user=current_user,
                current_timestamp=current_timestamp,
            )
        return AuthDatabaseValidationResult(
            ready=True,
            reason="ready",
            current_database=current_database,
            engine=version_value,
            current_user=current_user,
            current_timestamp=current_timestamp,
        )
    except psycopg.Error:
        return AuthDatabaseValidationResult(
            ready=False,
            reason="database_unreachable",
        )
    except Exception:
        return AuthDatabaseValidationResult(
            ready=False,
            reason="database_validation_failed",
        )
    finally:
        if connection is not None:
            with suppress(Exception):
                connection.rollback()
            with suppress(Exception):
                connection.close()


def resolve_auth_persistence_status(
    *,
    database_url: str,
    required_schema: dict[str, tuple[str, ...]],
) -> AuthPersistenceStatus:
    """Return DB readiness for the requested auth persistence schema."""

    del required_schema
    validation = validate_auth_database_connection(database_url)
    if validation.ready:
        return "ready"
    if validation.reason in {"wrong_database", "wrong_database_engine"}:
        return "schema_mismatch"
    return "unavailable"


def _safe_first_text(row: tuple[object, ...] | None) -> str:
    if row is None or not row:
        return ""
    first_value = row[0]
    if first_value is None:
        return ""
    return str(first_value)


def _safe_first_datetime(row: tuple[object, ...] | None) -> datetime | None:
    if row is None or not row:
        return None
    first_value = row[0]
    if isinstance(first_value, datetime):
        return first_value
    return None


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


def _extract_sqlstate(error: BaseException) -> str | None:
    try:
        sqlstate = _normalize_sqlstate(getattr(error, "sqlstate", None))
    except Exception:
        sqlstate = None
    if sqlstate is not None:
        return sqlstate

    try:
        diag = getattr(error, "diag", None)
        diag_sqlstate = _normalize_sqlstate(getattr(diag, "sqlstate", None))
    except Exception:
        diag_sqlstate = None
    if diag_sqlstate is not None:
        return diag_sqlstate
    return None


def _classify_auth_transaction_sqlstate(
    sqlstate: str | None,
) -> Literal[
    "retryable_serialization_failure",
    "ambiguous_transaction_result",
    "non_retryable",
]:
    if sqlstate == "40001":
        return "retryable_serialization_failure"
    if sqlstate == "40003":
        return "ambiguous_transaction_result"
    return "non_retryable"


def _normalize_sqlstate(sqlstate: object) -> str | None:
    if not isinstance(sqlstate, str):
        return None
    normalized_sqlstate = sqlstate.strip()
    if len(normalized_sqlstate) != 5 or not normalized_sqlstate.isalnum():
        return None
    return normalized_sqlstate


def _resolve_auth_transaction_max_attempts(max_attempts: int | None) -> int:
    if max_attempts is None:
        return AUTH_TRANSACTION_MAX_ATTEMPTS_DEFAULT
    if max_attempts < 1:
        raise ValueError("auth_transaction_max_attempts_must_be_positive")
    if max_attempts > AUTH_TRANSACTION_MAX_ATTEMPTS_CAP:
        raise ValueError("auth_transaction_max_attempts_exceeds_safe_cap")
    return max_attempts


def _compute_auth_transaction_retry_delay_seconds(
    *,
    attempt_number: int,
    jitter_fn: Callable[[], float],
) -> float:
    exponential_delay = AUTH_TRANSACTION_RETRY_BASE_DELAY_SECONDS * (
        2 ** max(0, attempt_number - 1)
    )
    bounded_delay = min(AUTH_TRANSACTION_RETRY_MAX_DELAY_SECONDS, exponential_delay)
    try:
        jitter_sample = float(jitter_fn())
    except Exception:
        jitter_sample = 0.5
    if jitter_sample != jitter_sample:  # NaN guard
        jitter_sample = 0.5
    bounded_jitter_sample = min(1.0, max(0.0, jitter_sample))
    jitter_span = min(bounded_delay * 0.2, AUTH_TRANSACTION_RETRY_JITTER_MAX_SECONDS)
    jitter_offset = (bounded_jitter_sample - 0.5) * 2.0 * jitter_span
    bounded_retry_delay = min(
        AUTH_TRANSACTION_RETRY_MAX_DELAY_SECONDS,
        max(0.0, bounded_delay + jitter_offset),
    )
    return round(bounded_retry_delay, 6)


def _emit_auth_transaction_event(
    *,
    event_status: str,
    operation_name: str,
    attempt_number: int,
    maximum_attempts: int,
    sqlstate: str | None,
) -> None:
    event: dict[str, object] = {
        "event_type": "auth.persistence.transaction",
        "event_status": event_status,
        "operation_name": operation_name,
        "attempt_number": attempt_number,
        "maximum_attempts": maximum_attempts,
        "sqlstate": sqlstate,
    }
    try:
        LOGGER.info(canonical_json_dumps(event))
    except Exception:
        _emit_auth_persistence_warning(
            event=event,
            failure_stage="transaction_event_emit_failed",
        )
        return


def _emit_auth_transaction_reconciliation_event(
    *,
    event_name: str,
    operation_name: str,
    sqlstate: str | None,
) -> None:
    event: dict[str, object] = {
        "event_type": "auth.persistence.transaction",
        "event_name": event_name,
        "operation_name": operation_name,
        "sqlstate": sqlstate,
    }
    try:
        LOGGER.info(canonical_json_dumps(event))
    except Exception:
        _emit_auth_persistence_warning(
            event=event,
            failure_stage="reconciliation_event_emit_failed",
        )
        return


def _reconcile_auth_transaction_ambiguous_result(
    *,
    database_url: str,
    operation_name: str,
    reconcile_ambiguous_result: Callable[[psycopg.Connection[Any]], T | None] | None,
    sqlstate: str | None,
) -> T:
    if reconcile_ambiguous_result is None:
        _emit_auth_transaction_reconciliation_event(
            event_name="auth transaction reconciliation failed",
            operation_name=operation_name,
            sqlstate=sqlstate,
        )
        raise AuthCockroachTransactionAmbiguousCommitError(
            reason_code="auth_persistence_ambiguous_result",
            message="Auth CockroachDB transaction outcome is ambiguous.",
            sqlstate=sqlstate,
            details={"operation_name": operation_name},
        )

    reconcile_connection: psycopg.Connection[Any] | None = None
    try:
        reconcile_connection = open_auth_database_connection(database_url)
    except psycopg.Error as error:
        _emit_auth_transaction_reconciliation_event(
            event_name="auth transaction reconciliation failed",
            operation_name=operation_name,
            sqlstate=sqlstate,
        )
        raise AuthCockroachTransactionAmbiguousCommitError(
            reason_code="auth_persistence_ambiguous_result",
            message="Auth CockroachDB transaction reconciliation failed.",
            sqlstate=sqlstate,
            details={"operation_name": operation_name},
        ) from error

    _emit_auth_transaction_reconciliation_event(
        event_name="auth transaction reconciliation started",
        operation_name=operation_name,
        sqlstate=sqlstate,
    )
    try:
        reconciled_result = reconcile_ambiguous_result(reconcile_connection)
    except Exception as error:  # noqa: BLE001
        _emit_auth_transaction_reconciliation_event(
            event_name="auth transaction reconciliation failed",
            operation_name=operation_name,
            sqlstate=sqlstate,
        )
        raise AuthCockroachTransactionAmbiguousCommitError(
            reason_code="auth_persistence_ambiguous_result",
            message="Auth CockroachDB transaction reconciliation failed.",
            sqlstate=sqlstate,
            details={"operation_name": operation_name},
        ) from error
    finally:
        _close_auth_connection(reconcile_connection)

    if reconciled_result is None:
        _emit_auth_transaction_reconciliation_event(
            event_name="auth transaction reconciliation not found",
            operation_name=operation_name,
            sqlstate=sqlstate,
        )
        raise AuthCockroachTransactionAmbiguousCommitError(
            reason_code="auth_persistence_ambiguous_result",
            message="Auth CockroachDB transaction outcome could not be reconciled.",
            sqlstate=sqlstate,
            details={"operation_name": operation_name},
        )

    _emit_auth_transaction_reconciliation_event(
        event_name="auth transaction reconciliation succeeded",
        operation_name=operation_name,
        sqlstate=sqlstate,
    )
    return reconciled_result


def _compute_retry_delay_seconds(
    *,
    attempt_count: int,
    retry_policy: AuthCockroachTransactionRetryPolicy,
    retry_jitter: float,
) -> float:
    exponent = max(0, attempt_count - 1)
    base_delay = retry_policy.base_delay_seconds * (retry_policy.backoff_multiplier**exponent)
    bounded_delay = min(retry_policy.max_delay_seconds, max(0.0, base_delay))
    bounded_jitter = max(-retry_policy.jitter_ratio, min(retry_policy.jitter_ratio, retry_jitter))
    jittered_delay = bounded_delay * (1.0 + bounded_jitter)
    return round(min(retry_policy.max_delay_seconds, max(0.0, jittered_delay)), 6)


def _cleanup_connection(
    *,
    connection: psycopg.Connection[Any] | None,
    should_rollback: bool,
) -> None:
    if connection is None:
        return
    if should_rollback:
        with suppress(Exception):
            connection.rollback()


def _close_auth_connection(connection: object | None) -> None:
    if connection is None:
        return
    close = getattr(connection, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def _emit_transaction_event(
    *,
    event_status: str,
    reason_code: str,
    attempt_count: int,
    sqlstate: str | None,
    retry_delays_seconds: tuple[float, ...],
    reconciled: bool,
    retry_delay_seconds: float | None = None,
) -> None:
    event: dict[str, object] = {
        "event_type": "auth.persistence.transaction",
        "event_status": event_status,
        "reason_code": reason_code,
        "attempt_count": attempt_count,
        "sqlstate": sqlstate,
        "retry_delays_seconds": retry_delays_seconds,
        "reconciled": reconciled,
    }
    if retry_delay_seconds is not None:
        event["retry_delay_seconds"] = retry_delay_seconds
    try:
        LOGGER.info(canonical_json_dumps(event))
    except Exception:
        _emit_auth_persistence_warning(
            event=event,
            failure_stage="transaction_event_emit_failed",
        )
        return


def _emit_auth_persistence_warning(
    *,
    event: dict[str, object],
    failure_stage: str,
) -> None:
    warning_event = dict(event)
    warning_event["event_status"] = "warning"
    warning_event["failure_stage"] = failure_stage
    try:
        LOGGER.warning(canonical_json_dumps(warning_event))
    except Exception:
        return
