"""Focused CockroachDB integration tests for auth delegation persistence."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4

import pytest
import psycopg

from services.auth.app.registration import DelegationConflictError
from services.auth.app.registration import PersistentDelegationStore
from services.auth.app.persistence_support import load_auth_database_url


@pytest.fixture(scope="session")
def cockroach_auth_database_url() -> str:
    database_url = load_auth_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for CockroachDB auth tests.")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            row = cursor.fetchone()

    if row is None:
        pytest.skip("DATABASE_URL validation did not return a database row.")

    current_database = str(row[0])
    version_text = str(row[1])
    if current_database != "kodi_dev":
        pytest.skip(
            "DATABASE_URL does not target the expected CockroachDB kodi_dev database."
        )
    if "CockroachDB" not in version_text:
        pytest.skip("DATABASE_URL does not target CockroachDB.")
    return database_url


def test_persistent_delegation_store_grants_revokes_and_reactivates(
    cockroach_auth_database_url: str,
) -> None:
    principal_user_id = _insert_user(
        cockroach_auth_database_url,
        email=_unique_email("delegation.principal"),
    )
    delegate_user_id = _insert_user(
        cockroach_auth_database_url,
        email=_unique_email("delegation.delegate"),
        phone=_unique_phone(),
    )

    store = PersistentDelegationStore(database_url=cockroach_auth_database_url)

    try:
        first = store.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at="2026-08-11T00:00:00Z",
        )
        assert first.is_active is True
        assert first.revoked_at is None

        revoked = store.revoke_delegation(
            delegation_id=first.delegation_id,
            revoked_at="2026-08-11T00:05:00Z",
        )
        assert revoked.is_active is False
        assert revoked.revoked_at == "2026-08-11T00:05:00Z"

        second = store.reactivate_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at="2026-08-11T00:10:00Z",
        )
        assert second.is_active is True
        assert second.revoked_at is None
        assert second.delegation_id != first.delegation_id

        active = store.get_active_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )
        assert active is not None
        assert active.delegation_id == second.delegation_id

        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        count(*) AS total_count,
                        count(*) FILTER (WHERE is_active) AS active_count
                    FROM delegations
                    WHERE principal_user_id = %s
                      AND delegate_user_id = %s
                    """,
                    (principal_user_id, delegate_user_id),
                )
                row = cursor.fetchone()

        assert row is not None
        assert int(row[0]) == 2
        assert int(row[1]) == 1
    finally:
        _cleanup_delegation_rows(
            cockroach_auth_database_url,
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )


def test_persistent_delegation_store_rejects_duplicate_active_pair(
    cockroach_auth_database_url: str,
) -> None:
    principal_user_id = _insert_user(
        cockroach_auth_database_url,
        email=_unique_email("delegation.conflict.principal"),
    )
    delegate_user_id = _insert_user(
        cockroach_auth_database_url,
        email=_unique_email("delegation.conflict.delegate"),
        phone=_unique_phone(),
    )

    store = PersistentDelegationStore(database_url=cockroach_auth_database_url)

    try:
        store.grant_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
            granted_at="2026-08-11T01:00:00Z",
        )

        with pytest.raises(DelegationConflictError) as excinfo:
            store.grant_delegation(
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                granted_at="2026-08-11T01:05:00Z",
            )

        assert excinfo.value.reason == "delegation_active_pair_conflict"
    finally:
        _cleanup_delegation_rows(
            cockroach_auth_database_url,
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )


def _insert_user(
    database_url: str,
    *,
    email: str,
    phone: str | None = None,
) -> UUID:
    user_id = uuid4()
    phone_number = phone if phone is not None else _unique_phone()
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (
                    id,
                    email_encrypted,
                    phone_number_encrypted,
                    kra_pin_encrypted,
                    role,
                    created_at,
                    updated_at,
                    password_hash,
                    password_history_hashes,
                    account_state,
                    verification_state,
                    deletion_lifecycle_state
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    now(),
                    now(),
                    %s,
                    %s::jsonb,
                    'active',
                    'verified',
                    'none'
                )
                """,
                (
                    user_id,
                    email,
                    phone_number,
                    "A123456789Z",
                    "IndividualTaxpayer",
                    "password-hash",
                    '["password-hash"]',
                ),
            )
        connection.commit()
    return user_id


def _cleanup_delegation_rows(
    database_url: str,
    *,
    principal_user_id: UUID,
    delegate_user_id: UUID,
) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM delegations
                WHERE principal_user_id = %s
                  AND delegate_user_id = %s
                """,
                (principal_user_id, delegate_user_id),
            )
            cursor.execute(
                "DELETE FROM users WHERE id IN (%s, %s)",
                (principal_user_id, delegate_user_id),
            )
        connection.commit()


def _unique_email(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex}@example.com"


def _unique_phone() -> str:
    return f"+254722{uuid4().int % 1_000_000:06d}"
