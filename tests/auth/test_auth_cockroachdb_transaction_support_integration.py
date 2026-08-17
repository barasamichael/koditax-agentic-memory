"""Focused CockroachDB integration tests for auth transaction execution support."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

import psycopg
import pytest

from services.auth.app.persistence_support import execute_auth_database_transaction
from services.auth.app.persistence_support import load_auth_database_url


def test_execute_auth_database_transaction_commits_a_real_cockroachdb_row() -> None:
    database_url = _resolve_cockroach_auth_database_url()
    email = f"transaction.commit.{uuid4().hex}@example.com"
    phone = f"+2547224{uuid4().int % 1_000_000:06d}"

    user_id = execute_auth_database_transaction(
        database_url=database_url,
        transaction_callback=lambda connection: _insert_user(
            connection,
            email=email,
            phone=phone,
        ),
    )

    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT email_encrypted, phone_number_encrypted FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()

        assert row is not None
        assert str(row[0]) == email
        assert str(row[1]) == phone
    finally:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))


def test_execute_auth_database_transaction_rolls_back_a_real_cockroachdb_failure() -> None:
    database_url = _resolve_cockroach_auth_database_url()
    email = f"transaction.rollback.{uuid4().hex}@example.com"
    phone = f"+2547225{uuid4().int % 1_000_000:06d}"

    def _transaction_callback(connection: psycopg.Connection[object]) -> UUID:
        _insert_user(
            connection,
            email=email,
            phone=phone,
        )
        _raise_value_error("simulated application failure")

    with pytest.raises(ValueError):
        execute_auth_database_transaction(
            database_url=database_url,
            transaction_callback=_transaction_callback,
        )

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE email_encrypted = %s",
                (email,),
            )
            row = cursor.fetchone()
    assert row is None


def _resolve_cockroach_auth_database_url() -> str:
    database_url = load_auth_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for CockroachDB auth transaction tests.")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            row = cursor.fetchone()
    if row is None:
        pytest.skip("DATABASE_URL did not return a CockroachDB database name.")
    if str(row[0]) != "kodi_dev":
        pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")
    if "CockroachDB" not in str(row[1]):
        pytest.skip("DATABASE_URL does not target CockroachDB.")
    return database_url


def _insert_user(
    connection: psycopg.Connection[object],
    *,
    email: str,
    phone: str,
) -> UUID:
    user_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO users (
                id,
                phone_number_encrypted,
                email_encrypted,
                kra_pin_encrypted,
                role,
                subscription_tier,
                password_hash,
                password_history_hashes,
                account_state,
                verification_state,
                deletion_lifecycle_state
            )
            VALUES (%s, %s, %s, %s, %s, 'standard', 'password-hash', %s::jsonb, 'active', 'verified', 'none')
            """,
            (
                user_id,
                phone,
                email,
                "A123456789Z",
                "IndividualTaxpayer",
                json.dumps(["password-hash"]),
            ),
        )
    return user_id


def _raise_value_error(message: str) -> None:
    raise ValueError(message)
