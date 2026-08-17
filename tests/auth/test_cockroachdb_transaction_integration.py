"""Focused CockroachDB integration tests for auth transaction execution."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4

import pytest
import psycopg
from psycopg import sql

from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.persistence_support import execute_auth_transaction
from services.auth.app.persistence_support import AuthCockroachTransactionSqlError

PROBE_TABLE_PREFIX = "auth_tx_probe_"


@pytest.fixture(scope="session")
def cockroach_auth_database_url() -> str:
    database_url = load_auth_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for CockroachDB auth transaction tests.")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            row = cursor.fetchone()

    if row is None:
        pytest.skip("DATABASE_URL validation did not return a database row.")

    current_database = str(row[0])
    version_text = str(row[1])
    if current_database != "kodi_dev":
        pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")
    if "CockroachDB" not in version_text:
        pytest.skip("DATABASE_URL does not target CockroachDB.")
    return database_url


@pytest.fixture
def auth_tx_probe_table_name(cockroach_auth_database_url: str) -> str:
    table_name = f"{PROBE_TABLE_PREFIX}{uuid4().hex}"
    _create_probe_table(cockroach_auth_database_url, table_name)
    try:
        yield table_name
    finally:
        _drop_probe_table(cockroach_auth_database_url, table_name)
        assert not _probe_table_exists(cockroach_auth_database_url, table_name)


def test_execute_auth_transaction_commits_a_real_cockroachdb_probe_row(
    cockroach_auth_database_url: str,
    auth_tx_probe_table_name: str,
) -> None:
    probe_id = uuid4()
    probe_value = f"commit-{uuid4().hex}"

    result = execute_auth_transaction(
        database_url=cockroach_auth_database_url,
        operation_name="auth_probe_commit",
        operation=lambda connection: _insert_probe_row(
            connection,
            table_name=auth_tx_probe_table_name,
            probe_id=probe_id,
            probe_value=probe_value,
        ),
        sleep_fn=lambda _: pytest.fail("sleep must not be called"),
    )

    assert result == probe_id
    with psycopg.connect(cockroach_auth_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT probe_value FROM public.{} WHERE id = %s").format(
                    sql.Identifier(auth_tx_probe_table_name)
                ),
                (probe_id,),
            )
            row = cursor.fetchone()

    assert row is not None
    assert str(row[0]) == probe_value


def test_execute_auth_transaction_rolls_back_a_real_cockroachdb_probe_row(
    cockroach_auth_database_url: str,
    auth_tx_probe_table_name: str,
) -> None:
    probe_id = uuid4()
    probe_value = f"rollback-{uuid4().hex}"

    def _operation(connection: psycopg.Connection[object]) -> UUID:
        _insert_probe_row(
            connection,
            table_name=auth_tx_probe_table_name,
            probe_id=probe_id,
            probe_value=probe_value,
        )
        raise ValueError("simulated application failure")

    with pytest.raises(ValueError):
        execute_auth_transaction(
            database_url=cockroach_auth_database_url,
            operation_name="auth_probe_rollback",
            operation=_operation,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    with psycopg.connect(cockroach_auth_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT id FROM public.{} WHERE id = %s").format(
                    sql.Identifier(auth_tx_probe_table_name)
                ),
                (probe_id,),
            )
            row = cursor.fetchone()

    assert row is None


def test_execute_auth_transaction_rejects_duplicate_constraint_without_retry(
    cockroach_auth_database_url: str,
    auth_tx_probe_table_name: str,
) -> None:
    probe_value = f"duplicate-{uuid4().hex}"
    first_probe_id = uuid4()
    second_probe_id = uuid4()

    with psycopg.connect(cockroach_auth_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO public.{} (id, probe_value) VALUES (%s, %s)").format(
                    sql.Identifier(auth_tx_probe_table_name)
                ),
                (first_probe_id, probe_value),
            )
        connection.commit()

    callback_calls = 0

    def _operation(connection: psycopg.Connection[object]) -> UUID:
        nonlocal callback_calls
        callback_calls += 1
        _insert_probe_row(
            connection,
            table_name=auth_tx_probe_table_name,
            probe_id=second_probe_id,
            probe_value=probe_value,
        )
        return second_probe_id

    with pytest.raises(AuthCockroachTransactionSqlError) as excinfo:
        execute_auth_transaction(
            database_url=cockroach_auth_database_url,
            operation_name="auth_probe_unique_constraint",
            operation=_operation,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    assert callback_calls == 1
    assert excinfo.value.sqlstate is not None
    assert excinfo.value.sqlstate != "40001"

    with psycopg.connect(cockroach_auth_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM public.{} WHERE probe_value = %s").format(
                    sql.Identifier(auth_tx_probe_table_name)
                ),
                (probe_value,),
            )
            row = cursor.fetchone()

    assert row is not None
    assert int(row[0]) == 1


def test_execute_auth_transaction_uses_a_fresh_connection_after_a_failure(
    cockroach_auth_database_url: str,
    auth_tx_probe_table_name: str,
) -> None:
    failed_probe_value = f"failed-{uuid4().hex}"
    recovered_probe_value = f"recovered-{uuid4().hex}"
    failed_probe_id = uuid4()
    recovered_probe_id = uuid4()

    def _failed_operation(connection: psycopg.Connection[object]) -> UUID:
        _insert_probe_row(
            connection,
            table_name=auth_tx_probe_table_name,
            probe_id=failed_probe_id,
            probe_value=failed_probe_value,
        )
        raise ValueError("simulated failure before commit")

    with pytest.raises(ValueError):
        execute_auth_transaction(
            database_url=cockroach_auth_database_url,
            operation_name="auth_probe_reuse_failure",
            operation=_failed_operation,
            sleep_fn=lambda _: pytest.fail("sleep must not be called"),
        )

    recovered_result = execute_auth_transaction(
        database_url=cockroach_auth_database_url,
        operation_name="auth_probe_reuse_recovery",
        operation=lambda connection: _insert_probe_row(
            connection,
            table_name=auth_tx_probe_table_name,
            probe_id=recovered_probe_id,
            probe_value=recovered_probe_value,
        ),
        sleep_fn=lambda _: pytest.fail("sleep must not be called"),
    )

    assert recovered_result == recovered_probe_id

    with psycopg.connect(cockroach_auth_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT probe_value FROM public.{} WHERE id IN (%s, %s) ORDER BY probe_value"
                ).format(sql.Identifier(auth_tx_probe_table_name)),
                (failed_probe_id, recovered_probe_id),
            )
            rows = cursor.fetchall()

    assert len(rows) == 1
    assert str(rows[0][0]) == recovered_probe_value


def test_auth_tx_probe_table_cleanup_removes_table(
    cockroach_auth_database_url: str,
) -> None:
    table_name = f"{PROBE_TABLE_PREFIX}{uuid4().hex}"
    _create_probe_table(cockroach_auth_database_url, table_name)
    assert _probe_table_exists(cockroach_auth_database_url, table_name)
    _drop_probe_table(cockroach_auth_database_url, table_name)
    assert not _probe_table_exists(cockroach_auth_database_url, table_name)


def _create_probe_table(database_url: str, table_name: str) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    CREATE TABLE public.{} (
                        id UUID PRIMARY KEY,
                        probe_value STRING NOT NULL UNIQUE
                    )
                    """
                ).format(sql.Identifier(table_name))
            )
        connection.commit()


def _drop_probe_table(database_url: str, table_name: str) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS public.{}").format(sql.Identifier(table_name))
            )
        connection.commit()


def _probe_table_exists(database_url: str, table_name: str) -> bool:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
            row = cursor.fetchone()
    return bool(row and row[0])


def _insert_probe_row(
    connection: psycopg.Connection[object],
    *,
    table_name: str,
    probe_id: UUID,
    probe_value: str,
) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("INSERT INTO public.{} (id, probe_value) VALUES (%s, %s)").format(
                sql.Identifier(table_name)
            ),
            (probe_id, probe_value),
        )
    return probe_id
