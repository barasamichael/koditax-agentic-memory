"""Real-database schema tests for the CockroachDB auth migration lane."""

from __future__ import annotations

import json
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any
from typing import cast
from uuid import UUID
from uuid import uuid4

import pytest
import psycopg
from psycopg import sql

from services.auth.app.persistence_support import load_auth_database_url
from services.auth.migrations.cockroachdb import runner


@pytest.fixture(scope="session")
def cockroach_auth_database() -> Iterator[tuple[str, set[str], set[str]]]:
    database_url = load_auth_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for CockroachDB auth schema tests.")

    with psycopg.connect(database_url) as connection:
        try:
            runner._validate_target_database(connection)  # type: ignore[arg-type]
        except runner.AuthTargetError:
            pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")
        before_tables = _load_tables(connection)

    assert runner.main() == 0
    assert runner.main() == 0

    with psycopg.connect(database_url) as connection:
        after_tables = _load_tables(connection)
        runner._validate_final_schema(connection=connection)  # type: ignore[arg-type]

    yield database_url, before_tables, after_tables


def test_cockroachdb_auth_schema_isolated_table_changes(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    _, before_tables, after_tables = cockroach_auth_database
    created_tables = after_tables - before_tables
    allowed_tables = {
        "users",
        "sessions",
        "delegations",
        "auth_cockroachdb_schema_migrations",
        "auth_session_refresh_tokens",
        "auth_login_lockouts",
        "auth_idempotency_preclaims",
        "auth_otp_challenges",
        "auth_password_reset_challenges",
        "auth_login_step_up_states",
        "auth_phone_change_requests",
        "auth_phone_change_audit_events",
        "auth_account_deletion_requests",
        "auth_account_deletion_audit_events",
        "auth_account_deletion_notifications",
        "auth_account_deletion_incidents",
        "auth_account_deletion_reauth_proofs",
        "auth_account_deletion_otp_proofs",
    }

    assert created_tables <= allowed_tables


def test_phone_change_superseded_state_is_supported(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        request_id = uuid4()
        user_id = _insert_user(connection, email=_unique_email("schema.phone.superseded"))
        _insert_phone_change_request(
            connection,
            request_id=request_id,
            user_id=user_id,
            state="superseded",
        )
        connection.rollback()


def test_duplicate_phone_change_pending_request_is_rejected(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        user_id = _insert_user(connection, email=_unique_email("schema.phone.pending"))
        _insert_phone_change_request(
            connection,
            request_id=uuid4(),
            user_id=user_id,
            state="pending_confirmation",
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with _savepoint(connection):
                _insert_phone_change_request(
                    connection,
                    request_id=uuid4(),
                    user_id=user_id,
                    state="pending_confirmation",
                    new_phone_number_normalized=_unique_phone_number(),
                )
        connection.rollback()


def test_duplicate_email_and_phone_are_rejected(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        duplicate_email = _unique_email("schema.duplicate")
        duplicate_phone = _unique_phone_number()
        _insert_user(
            connection,
            email=duplicate_email,
            phone=duplicate_phone,
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with _savepoint(connection):
                _insert_user(
                    connection,
                    email=duplicate_email,
                    phone=_unique_phone_number(),
                )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with _savepoint(connection):
                _insert_user(
                    connection,
                    email=_unique_email("schema.unique"),
                    phone=duplicate_phone,
                )
        connection.rollback()


def test_unsupported_role_is_rejected(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            with _savepoint(connection):
                _insert_user(connection, email=_unique_email("schema.role.invalid"), role="Guest")
        connection.rollback()


def test_delegation_constraints_reject_same_user_and_duplicate_active_pair(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        principal_user_id = _insert_user(connection, email=_unique_email("schema.delegate.principal"))
        delegate_user_id = _insert_user(
            connection,
            email=_unique_email("schema.delegate.delegate"),
            phone=_unique_phone_number(),
        )
        connection.commit()

        try:
            with pytest.raises(psycopg.errors.CheckViolation):
                with _savepoint(connection):
                    _insert_delegation(connection, principal_user_id, principal_user_id)

            _insert_delegation(connection, principal_user_id, delegate_user_id)

            with pytest.raises(psycopg.errors.UniqueViolation):
                with _savepoint(connection):
                    _insert_delegation(connection, principal_user_id, delegate_user_id)
        finally:
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM delegations
                    WHERE principal_user_id IN (%s, %s)
                       OR delegate_user_id IN (%s, %s)
                    """,
                    (principal_user_id, delegate_user_id, principal_user_id, delegate_user_id),
                )
                cursor.execute(
                    "DELETE FROM users WHERE id IN (%s, %s)",
                    (principal_user_id, delegate_user_id),
                )
            connection.commit()


def test_session_refresh_token_one_active_per_session(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        user_id = _insert_user(connection, email=_unique_email("schema.session.user"))
        session_id = uuid4()
        _insert_session(connection, session_id=session_id, user_id=user_id)
        _insert_refresh_token(connection, session_id=session_id, refresh_token_hash=_unique_token("refresh"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            with _savepoint(connection):
                _insert_refresh_token(
                    connection,
                    session_id=session_id,
                    refresh_token_hash=_unique_token("refresh"),
                )
        connection.rollback()


def test_invalid_lifecycle_foreign_keys_are_rejected(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with _savepoint(connection):
                _insert_account_deletion_request(
                    connection,
                    request_id=uuid4(),
                    user_id=uuid4(),
                    state="requested",
                )
        connection.rollback()


def test_login_lockout_and_password_reset_constraints_reject_invalid_values(
    cockroach_auth_database: tuple[str, set[str], set[str]]
) -> None:
    database_url, _, _ = cockroach_auth_database
    with psycopg.connect(database_url) as connection:
        user_id = _insert_user(connection, email=_unique_email("schema.constraints"))

        with pytest.raises(psycopg.errors.CheckViolation):
            with _savepoint(connection):
                _insert_login_lockout(connection, failed_attempt_count=-1)
        with pytest.raises(psycopg.errors.CheckViolation):
            with _savepoint(connection):
                _insert_otp_challenge(
                    connection,
                    issued_at="2026-08-06T12:00:00Z",
                    expires_at="2026-08-06T11:59:59Z",
                )
        with pytest.raises(psycopg.errors.CheckViolation):
            with _savepoint(connection):
                _insert_password_reset_challenge(
                    connection,
                    user_id=user_id,
                    idempotency_key=_unique_token("password-reset"),
                    issued_at="2026-08-06T12:00:00Z",
                    expires_at="2026-08-06T11:59:59Z",
                )
        connection.rollback()


def _load_tables(connection: psycopg.Connection[object]) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
            ORDER BY table_name
            """,
            ("public",),
        )
        rows = cast(list[tuple[Any, ...]], cursor.fetchall())
    return {str(row[0]) for row in rows}


def _insert_user(
    connection: psycopg.Connection[object],
    *,
    email: str,
    phone: str | None = None,
    role: str = "IndividualTaxpayer",
) -> UUID:
    user_id = uuid4()
    resolved_phone = phone
    if resolved_phone is None:
        resolved_phone = _unique_phone_number()
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
                resolved_phone,
                email,
                "A123456789Z",
                role,
                json.dumps(["password-hash"]),
            ),
        )
    return user_id


def _insert_delegation(
    connection: psycopg.Connection[object],
    principal_user_id: UUID,
    delegate_user_id: UUID,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO delegations (
                principal_user_id,
                delegate_user_id,
                granted_at,
                revoked_at,
                is_active
            )
            VALUES (%s, %s, now(), NULL, TRUE)
            """,
            (principal_user_id, delegate_user_id),
        )


def _insert_session(
    connection: psycopg.Connection[object],
    *,
    session_id: UUID,
    user_id: UUID,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sessions (
                id,
                user_id,
                idempotency_key,
                issued_at,
                expires_at,
                inactivity_expires_at,
                last_activity_at,
                tenant_id,
                role,
                is_invalidated,
                created_at
            )
            VALUES (
                %s,
                %s,
                %s,
                now(),
                now() + interval '1 hour',
                now() + interval '30 minutes',
                now(),
                'default_tenant',
                'IndividualTaxpayer',
                FALSE,
                now()
            )
            """,
            (session_id, user_id, f"session:{session_id}"),
        )


def _insert_refresh_token(
    connection: psycopg.Connection[object],
    *,
    session_id: UUID,
    refresh_token_hash: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO auth_session_refresh_tokens (
                refresh_token_hash,
                session_id,
                issued_at,
                is_consumed,
                consumed_at
            )
            VALUES (%s, %s, now(), FALSE, NULL)
            """,
            (refresh_token_hash, session_id),
        )


def _insert_phone_change_request(
    connection: psycopg.Connection[object],
    *,
    request_id: UUID,
    user_id: UUID,
    state: str,
    new_phone_number_normalized: str | None = None,
) -> None:
    resolved_current_phone_number = _unique_phone_number()
    resolved_new_phone_number = (
        new_phone_number_normalized if new_phone_number_normalized is not None else _unique_phone_number()
    )
    step_up_challenge_id = _insert_otp_challenge(
        connection,
        issued_at="2026-08-07T00:00:00Z",
        expires_at="2026-08-07T00:30:00Z",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO auth_phone_change_requests (
                request_id,
                user_id,
                tenant_id,
                requested_at,
                current_phone_number_normalized,
                new_phone_number_normalized,
                phone_change_state,
                step_up_challenge_id,
                step_up_expires_at,
                request_idempotency_key,
                request_fingerprint,
                confirmed_at,
                confirm_idempotency_key,
                confirm_request_fingerprint
            )
            VALUES (
                %s,
                %s,
                'default_tenant',
                now(),
                %s,
                %s,
                %s,
                %s,
                now() + interval '30 minutes',
                %s,
                'fingerprint',
                NULL,
                NULL,
                NULL
            )
            """,
            (
                request_id,
                user_id,
                resolved_current_phone_number,
                resolved_new_phone_number,
                state,
                step_up_challenge_id,
                f"phone-request:{request_id}",
            ),
        )


def _insert_account_deletion_request(
    connection: psycopg.Connection[object],
    *,
    request_id: UUID,
    user_id: UUID,
    state: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO auth_account_deletion_requests (
                request_id,
                user_id,
                tenant_id,
                request_reason,
                requested_at,
                deletion_state,
                blocker_reasons,
                request_idempotency_key,
                request_fingerprint,
                confirmed_at,
                cooldown_expires_at,
                executed_at,
                execution_outcome,
                revoked_session_count,
                confirm_idempotency_key,
                confirm_request_fingerprint,
                cancel_idempotency_key,
                cancel_request_fingerprint,
                execute_idempotency_key,
                execute_request_fingerprint
            )
            VALUES (
                %s,
                %s,
                'default_tenant',
                'cleanup',
                now(),
                %s,
                '[]'::jsonb,
                %s,
                'fingerprint',
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL
            )
            """,
            (request_id, user_id, state, f"account-deletion:{request_id}"),
        )


def _insert_login_lockout(
    connection: psycopg.Connection[object],
    *,
    failed_attempt_count: int,
    login_id_normalized: str | None = None,
    source_ip: str | None = None,
) -> None:
    resolved_login_id_normalized = (
        login_id_normalized if login_id_normalized is not None else _unique_email("schema.login")
    )
    resolved_source_ip = source_ip if source_ip is not None else _unique_ip_address()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO auth_login_lockouts (
                login_id_normalized,
                source_ip,
                failed_attempt_count,
                last_failed_attempt_at,
                lockout_expires_at,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s, now(), now() + interval '5 minutes', now(), now())
            """,
            (resolved_login_id_normalized, resolved_source_ip, failed_attempt_count),
        )


def _insert_otp_challenge(
    connection: psycopg.Connection[object],
    *,
    issued_at: str,
    expires_at: str,
    subject_normalized: str | None = None,
    idempotency_key: str | None = None,
) -> UUID:
    challenge_id = uuid4()
    resolved_subject_normalized = (
        subject_normalized if subject_normalized is not None else _unique_phone_number()
    )
    resolved_idempotency_key = (
        idempotency_key if idempotency_key is not None else _unique_token("otp")
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO auth_otp_challenges (
                challenge_id,
                channel,
                purpose,
                subject_normalized,
                otp_code,
                issued_at,
                expires_at,
                consumed_at,
                failed_attempt_count,
                max_attempts,
                cooldown_seconds,
                cooldown_expires_at,
                idempotency_key,
                request_fingerprint,
                created_at
            )
            VALUES (
                %s,
                'sms',
                'login_step_up',
                %s,
                '123456',
                %s::timestamptz,
                %s::timestamptz,
                NULL,
                0,
                3,
                60,
                NULL,
                %s,
                %s,
                now()
            )
            """,
            (
                challenge_id,
                resolved_subject_normalized,
                issued_at,
                expires_at,
                resolved_idempotency_key,
                f"otp-fingerprint:{challenge_id}",
            ),
        )
    return challenge_id


def _insert_password_reset_challenge(
    connection: psycopg.Connection[object],
    *,
    user_id: UUID,
    idempotency_key: str,
    issued_at: str,
    expires_at: str,
    subject_normalized: str | None = None,
) -> None:
    resolved_subject_normalized = (
        subject_normalized if subject_normalized is not None else _unique_email("schema.password")
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO auth_password_reset_challenges (
                challenge_id,
                purpose,
                channel,
                subject_normalized,
                user_id,
                reset_code,
                issued_at,
                expires_at,
                consumed_at,
                failed_attempt_count,
                max_attempts,
                idempotency_key,
                request_fingerprint,
                created_at
            )
            VALUES (
                %s,
                'password_reset',
                'email',
                %s,
                %s,
                '999999',
                %s::timestamptz,
                %s::timestamptz,
                NULL,
                0,
                3,
                %s,
                %s,
                now()
            )
            """,
            (
                uuid4(),
                resolved_subject_normalized,
                user_id,
                issued_at,
                expires_at,
                idempotency_key,
                f"password-reset-fingerprint:{user_id}:{idempotency_key}",
            ),
        )


def _unique_email(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex}@example.com"


def _unique_phone_number() -> str:
    return f"+2547{uuid4().int % 1_000_000_000_000:012d}"


def _unique_token(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _unique_ip_address() -> str:
    octet_three = uuid4().int % 254 + 1
    octet_four = uuid4().int % 254 + 1
    return f"198.51.{octet_three}.{octet_four}"


@contextmanager
def _savepoint(connection: psycopg.Connection[object]) -> Iterator[None]:
    savepoint_name = f"sp_{uuid4().hex}"
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SAVEPOINT {}").format(sql.Identifier(savepoint_name)))
    try:
        yield
    except Exception:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sql.Identifier(savepoint_name)))
            cursor.execute(sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(savepoint_name)))
        raise
    else:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(savepoint_name)))
