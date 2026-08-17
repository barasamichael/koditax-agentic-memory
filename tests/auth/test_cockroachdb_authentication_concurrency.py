"""CockroachDB auth concurrency and reconstruction torture coverage."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
import os
from typing import Any
from typing import cast
from typing import TypeVar
from uuid import UUID
from uuid import uuid4
import threading

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.auth.app import config as auth_config
from services.auth.app.account_deletion import AccountDeletionRequestError
from services.auth.app.account_deletion import cancel_account_deletion_request
from services.auth.app.account_deletion import confirm_account_deletion_request
from services.auth.app.account_deletion import create_account_deletion_request
from services.auth.app.account_deletion import execute_account_deletion_request
from services.auth.app.account_deletion import PersistentAccountDeletionRequestStore
from services.auth.app.config import DEFAULT_AUTH_TENANT_ID
from services.auth.app.login import PersistentLoginLockoutStore
from services.auth.app.main import InMemoryAuthAuditStore
from services.auth.app.main import create_app
from services.auth.app.otp_delivery_adapters import StubSmsOtpDeliveryAdapter
from services.auth.app.password_reset import PasswordResetConfirmRequest
from services.auth.app.password_reset import PasswordResetError
from services.auth.app.password_reset import PasswordResetInitiateRequest
from services.auth.app.password_reset import PersistentPasswordResetStore
from services.auth.app.password_reset import confirm_password_reset_challenge
from services.auth.app.password_reset import initiate_password_reset_challenge
from services.auth.app.phone_change import PhoneChangeError
from services.auth.app.phone_change import PersistentPhoneChangeStore
from services.auth.app.phone_change import confirm_phone_change_request
from services.auth.app.phone_change import create_phone_change_request
from services.auth.app.persistence_support import DATABASE_URL_ENV_VAR
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.registration import DelegationConflictError
from services.auth.app.registration import PersistentRegistrationStore
from services.auth.app.registration import RegistrationConflictError
from services.auth.app.registration import PersistentDelegationStore
from services.auth.app.session_issuance import PersistentSessionIssuanceStore
from services.auth.app.session_issuance import SessionIssuanceError
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import reset_default_session_issuance_store
from services.auth.app.login import reset_default_login_lockout_store
from services.auth.app.phone_verification import reset_default_phone_verification_store
from services.auth.app.email_verification import reset_default_email_verification_store
from shared.validation.db_migrate import apply_migrations
from shared.validation.db_migrate import discover_migration_files

T = TypeVar("T")


class _FrozenClock:
    def __init__(self) -> None:
        self._current = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture(scope="session")
def cockroach_auth_database_url() -> str:
    database_url = load_auth_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for CockroachDB auth tests.")

    _ensure_auth_migrations_applied(database_url=database_url)
    _set_persistent_runtime_env(database_url=database_url)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            row = cursor.fetchone()

    if row is None:
        pytest.skip("DATABASE_URL validation did not return a database row.")
    if str(row[0]) != "kodi_dev":
        pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")
    if "CockroachDB" not in str(row[1]):
        pytest.skip("DATABASE_URL does not target CockroachDB.")
    return database_url


@contextmanager
def _persistent_auth_client() -> Iterator[TestClient]:
    _reset_auth_defaults()
    app = create_app()
    app.state.auth_audit_store = InMemoryAuthAuditStore()
    with TestClient(app) as client:
        yield client
    _reset_auth_defaults()


def _register_active_user(
    *,
    client: TestClient,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "concurrency-register-active-corr"},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    user_id = UUID(cast(str, payload["user_id"]))
    app_state = cast(Any, client.app).state
    registration_store = cast(PersistentRegistrationStore, app_state.registration_store)
    registration_store.mark_user_phone_verified(
        user_id=user_id,
        verified_at="2026-08-11T10:00:00Z",
    )
    return user_id


def _complete_login(
    *,
    client: TestClient,
    phone_number: str,
    source_ip: str,
    correlation_prefix: str,
) -> dict[str, object]:
    pending = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": f"{correlation_prefix}-pending",
            "X-Forwarded-For": source_ip,
        },
        json={"login_id": phone_number, "password": "StrongPassw0rd!"},
    )
    pending_payload = _response_json(pending)
    assert pending.status_code == 200
    assert pending_payload["status"] == "pending_step_up"

    challenge_id = cast(str, pending_payload["step_up_challenge_id"])
    email_store = cast(Any, client.app).state.email_verification_store
    code = cast(str, email_store.get_otp_code_for_challenge(challenge_id=UUID(challenge_id)))

    authenticated = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": f"{correlation_prefix}-complete",
            "X-Forwarded-For": source_ip,
        },
        json={
            "login_id": phone_number,
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": challenge_id,
            "step_up_otp_code": code,
        },
    )
    payload = _response_json(authenticated)
    assert authenticated.status_code == 200
    assert payload["status"] == "authenticated"
    return cast(dict[str, object], payload)


def _build_auth_header(*, user_id: UUID, role: str = "IndividualTaxpayer") -> str:
    return f"Bearer user_id={user_id};tenant_id={DEFAULT_AUTH_TENANT_ID};role={role}"


def _build_phone_number(*, suffix: str) -> str:
    return f"+25479915{suffix}"


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _run_race(
    callables: Sequence[Callable[[], T]],
    *,
    timeout_seconds: float = 30.0,
) -> list[tuple[str, object]]:
    barrier = threading.Barrier(len(callables))

    def _invoke(fn: Callable[[], T]) -> tuple[str, object]:
        barrier.wait(timeout=timeout_seconds)
        try:
            return ("ok", fn())
        except Exception as error:
            return ("error", error)

    with ThreadPoolExecutor(max_workers=len(callables)) as executor:
        futures = [executor.submit(_invoke, fn) for fn in callables]
        return [
            cast(tuple[str, object], future.result(timeout=timeout_seconds))
            for future in futures
        ]


def _split_race_results(
    results: Sequence[tuple[str, object]],
) -> tuple[list[object], list[object]]:
    successes = [value for status, value in results if status == "ok"]
    errors = [value for status, value in results if status == "error"]
    return successes, errors


def _cleanup_user_artifacts(
    *,
    database_url: str,
    user_id: UUID,
    email: str,
    phone: str,
    old_phone: str | None = None,
    session_ids: Sequence[UUID] = (),
) -> None:
    cleanup_subjects = {email, phone}
    if old_phone is not None:
        cleanup_subjects.add(old_phone)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM auth_login_lockouts WHERE login_id_normalized = %s",
                (phone,),
            )
            if old_phone is not None and old_phone != phone:
                cursor.execute(
                    "DELETE FROM auth_login_lockouts WHERE login_id_normalized = %s",
                    (old_phone,),
                )
            cursor.execute(
                "DELETE FROM auth_login_step_up_states WHERE login_id_normalized = %s",
                (phone,),
            )
            if old_phone is not None and old_phone != phone:
                cursor.execute(
                    "DELETE FROM auth_login_step_up_states WHERE login_id_normalized = %s",
                    (old_phone,),
                )
            cursor.execute(
                "DELETE FROM auth_phone_change_audit_events WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_phone_change_requests WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_reauth_proofs WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_otp_proofs WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_audit_events WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_notifications WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_incidents WHERE actor_user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM auth_account_deletion_requests WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                "DELETE FROM delegations WHERE principal_user_id = %s OR delegate_user_id = %s",
                (user_id, user_id),
            )
            for subject in cleanup_subjects:
                cursor.execute(
                    "DELETE FROM auth_otp_challenges WHERE subject_normalized = %s",
                    (subject,),
                )
                cursor.execute(
                    "DELETE FROM auth_password_reset_challenges WHERE subject_normalized = %s",
                    (subject,),
                )
            if session_ids:
                cursor.execute(
                    "DELETE FROM auth_session_refresh_tokens WHERE session_id = ANY(%s::uuid[])",
                    (list(session_ids),),
                )
                cursor.execute(
                    "DELETE FROM sessions WHERE id = ANY(%s::uuid[])",
                    (list(session_ids),),
                )
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        connection.commit()


def _ensure_auth_migrations_applied(*, database_url: str) -> None:
    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.auth_session_refresh_tokens')")
                refresh_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'users'
                      AND column_name = 'password_hash'
                    """
                )
                password_hash_row = cursor.fetchone()
                if refresh_row is not None and refresh_row[0] is not None and password_hash_row:
                    return
    except psycopg.Error:
        pytest.skip("Auth persistence database is not reachable.")

    repo_root = Path(__file__).resolve().parents[2]
    try:
        apply_migrations(
            database_url,
            discover_migration_files(repo_root),
            repo_root,
        )
    except Exception as error:  # pragma: no cover
        pytest.skip(f"Auth persistence migrations could not be applied: {error}")


def _reset_auth_defaults() -> None:
    reset_default_email_verification_store()
    reset_default_login_lockout_store()
    reset_default_phone_verification_store()
    reset_default_registration_store()
    reset_default_session_issuance_store()


def _set_persistent_runtime_env(*, database_url: str) -> None:
    os.environ["AUTH_SECRET_RUNTIME_MODE"] = "production"
    os.environ[DATABASE_URL_ENV_VAR] = database_url
    os.environ[auth_config.AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR] = "a" * 40
    os.environ[auth_config.AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR] = "b" * 40
    os.environ[auth_config.AUTH_ENCRYPTION_KEY_ACTIVE_ENV_VAR] = "c" * 40
    os.environ[auth_config.AUTH_IDEMPOTENCY_SIGNING_SECRET_ENV_VAR] = "d" * 40
    os.environ[auth_config.AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR] = "e" * 40
    os.environ[auth_config.AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR] = "f" * 40
    os.environ.pop(auth_config.AUTH_SESSION_SIGNING_KEY_NEXT_ENV_VAR, None)
    os.environ.pop(auth_config.AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR, None)
    os.environ.pop(auth_config.AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR, None)


def test_concurrent_duplicate_registration_is_durable_and_reconstructable(
    cockroach_auth_database_url: str,
) -> None:
    email = f"race.registration.{uuid4().hex}@example.com"
    same_phone = _build_phone_number(suffix="0001")
    store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)

    try:
        results = _run_race(
            [
                lambda: store.register_user(
                    email_normalized=email,
                    phone_number_normalized=same_phone,
                    kra_pin_hash="1" * 64,
                    password_hash="2" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:00:00Z",
                ),
                lambda: store.register_user(
                    email_normalized=email,
                    phone_number_normalized=same_phone,
                    kra_pin_hash="1" * 64,
                    password_hash="2" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:00:00Z",
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        conflict = cast(RegistrationConflictError, errors[0])
        assert conflict.reason in {
            "registration_duplicate_email_or_phone",
            "registration_duplicate_email",
            "registration_duplicate_phone",
        }

        winner = cast(object, successes[0])
        winner_user_id = cast(UUID, getattr(winner, "user_id"))
        reconstructed = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
        user = reconstructed.get_user_by_email(email_normalized=email)
        assert user is not None
        assert user.user_id == winner_user_id
        assert user.phone_number_normalized == same_phone
    finally:
        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE email_encrypted = %s", (email,))
            connection.commit()


def test_concurrent_registration_same_email_same_phone_settles_to_one_owner(
    cockroach_auth_database_url: str,
) -> None:
    email = f"race.registration.same-email.{uuid4().hex}@example.com"
    phone = _build_phone_number(suffix="0002")
    store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)

    try:
        results = _run_race(
            [
                lambda: store.register_user(
                    email_normalized=email,
                    phone_number_normalized=phone,
                    kra_pin_hash="3" * 64,
                    password_hash="4" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:01:00Z",
                ),
                lambda: store.register_user(
                    email_normalized=email,
                    phone_number_normalized=phone,
                    kra_pin_hash="3" * 64,
                    password_hash="4" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:01:00Z",
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert cast(RegistrationConflictError, errors[0]).reason in {
            "registration_duplicate_email_or_phone",
            "registration_duplicate_email",
            "registration_duplicate_phone",
        }

        reconstructed = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
        persisted = reconstructed.get_user_by_email(email_normalized=email)
        assert persisted is not None
        assert persisted.phone_number_normalized == phone
    finally:
        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE email_encrypted = %s", (email,))
            connection.commit()


def test_concurrent_registration_same_email_different_phone_settles_to_one_owner(
    cockroach_auth_database_url: str,
) -> None:
    email = f"race.registration.same-email.{uuid4().hex}@example.com"
    phone_a = _build_phone_number(suffix="0003")
    phone_b = _build_phone_number(suffix="0004")
    store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)

    try:
        results = _run_race(
            [
                lambda: store.register_user(
                    email_normalized=email,
                    phone_number_normalized=phone_a,
                    kra_pin_hash="3" * 64,
                    password_hash="4" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:01:00Z",
                ),
                lambda: store.register_user(
                    email_normalized=email,
                    phone_number_normalized=phone_b,
                    kra_pin_hash="3" * 64,
                    password_hash="4" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:01:00Z",
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert cast(RegistrationConflictError, errors[0]).reason in {
            "registration_duplicate_email_or_phone",
            "registration_duplicate_email",
            "registration_duplicate_phone",
        }

        reconstructed = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
        persisted = reconstructed.get_user_by_email(email_normalized=email)
        assert persisted is not None
        assert persisted.phone_number_normalized in {phone_a, phone_b}
    finally:
        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE email_encrypted = %s", (email,))
            connection.commit()


def test_concurrent_registration_same_phone_different_email_settles_to_one_owner(
    cockroach_auth_database_url: str,
) -> None:
    phone = _build_phone_number(suffix="0005")
    email_a = f"race.registration.same-phone-a.{uuid4().hex}@example.com"
    email_b = f"race.registration.same-phone-b.{uuid4().hex}@example.com"
    store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)

    try:
        results = _run_race(
            [
                lambda: store.register_user(
                    email_normalized=email_a,
                    phone_number_normalized=phone,
                    kra_pin_hash="3" * 64,
                    password_hash="4" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:01:00Z",
                ),
                lambda: store.register_user(
                    email_normalized=email_b,
                    phone_number_normalized=phone,
                    kra_pin_hash="3" * 64,
                    password_hash="4" * 64,
                    role="IndividualTaxpayer",
                    created_at="2026-08-11T10:01:00Z",
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert cast(RegistrationConflictError, errors[0]).reason in {
            "registration_duplicate_email_or_phone",
            "registration_duplicate_email",
            "registration_duplicate_phone",
        }

        reconstructed = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
        persisted = reconstructed.get_user_by_phone(phone_number_normalized=phone)
        assert persisted is not None
        assert persisted.email_normalized in {email_a, email_b}
    finally:
        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE phone_number_encrypted = %s", (phone,))
            connection.commit()


def test_concurrent_refresh_rotation_preserves_single_winner_and_reconstruction(
    cockroach_auth_database_url: str,
) -> None:
    session_store = PersistentSessionIssuanceStore(
        database_url=cockroach_auth_database_url,
        max_concurrent_sessions=3,
    )
    email = f"race.refresh.{uuid4().hex}@example.com"
    phone = _build_phone_number(suffix="0006")

    with _persistent_auth_client() as client:
        user_id = _register_active_user(
            client=client,
            email=email,
            phone_number=phone,
        )
        login_payload = _complete_login(
            client=client,
            phone_number=phone,
            source_ip="198.51.100.50",
            correlation_prefix="refresh-race",
        )

    refresh_token = cast(str, login_payload["refresh_token"])
    session_id = UUID(str(login_payload["session"]["session_id"]))

    try:
        results = _run_race(
            [
                lambda: session_store.refresh_session(refresh_token=refresh_token),
                lambda: session_store.refresh_session(refresh_token=refresh_token),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], SessionIssuanceError)
        assert cast(SessionIssuanceError, errors[0]).reason == "refresh_token_reused"

        reconstructed = PersistentSessionIssuanceStore(
            database_url=cockroach_auth_database_url,
            max_concurrent_sessions=3,
        )
        session = reconstructed.get_session(session_id=session_id)
        assert session is not None
        assert session.user_id == user_id
        assert reconstructed.evaluate_session(session_id=session_id) is not None

        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT is_consumed, session_id
                    FROM auth_session_refresh_tokens
                    WHERE refresh_token_hash = %s
                    """,
                    (sha256(refresh_token.encode("utf-8")).hexdigest(),),
                )
                old_token_row = cursor.fetchone()
        assert old_token_row is not None
        assert bool(old_token_row[0]) is True
        assert UUID(str(old_token_row[1])) == session_id
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email,
            phone=phone,
            session_ids=(session_id,),
        )


def test_refresh_vs_revocation_settles_to_a_revoked_session_state(
    cockroach_auth_database_url: str,
) -> None:
    store = PersistentSessionIssuanceStore(
        database_url=cockroach_auth_database_url,
        max_concurrent_sessions=3,
    )
    email = f"race.refresh.revoke.{uuid4().hex}@example.com"
    phone = _build_phone_number(suffix="0007")

    with _persistent_auth_client() as client:
        user_id = _register_active_user(
            client=client,
            email=email,
            phone_number=phone,
        )
        login_payload = _complete_login(
            client=client,
            phone_number=phone,
            source_ip="198.51.100.51",
            correlation_prefix="refresh-revoke-race",
        )

    session_id = UUID(str(login_payload["session"]["session_id"]))
    refresh_token = cast(str, login_payload["refresh_token"])

    try:
        results = _run_race(
            [
                lambda: store.refresh_session(refresh_token=refresh_token),
                lambda: store.revoke_session(user_id=user_id, session_id=session_id),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) in {1, 2}
        assert len(errors) in {0, 1}

        reconstructed = PersistentSessionIssuanceStore(
            database_url=cockroach_auth_database_url,
            max_concurrent_sessions=3,
        )
        session = reconstructed.get_session(session_id=session_id)
        assert session is not None
        assert session.is_invalidated is True
        assert session.invalidated_reason == "session_revoked"

        if successes:
            successful_refresh = next(
                (
                    cast(object, payload)
                    for status, payload in results
                    if status == "ok" and hasattr(payload, "refresh_token")
                ),
                None,
            )
            if successful_refresh is not None:
                refresh_result = cast(object, successful_refresh)
                rotated_token = cast(str, getattr(refresh_result, "refresh_token"))
                with pytest.raises(SessionIssuanceError):
                    reconstructed.refresh_session(refresh_token=rotated_token)

        with pytest.raises(SessionIssuanceError):
            reconstructed.refresh_session(refresh_token=refresh_token)
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email,
            phone=phone,
            session_ids=(session_id,),
        )


def test_concurrent_session_issuance_enforces_limit_and_survives_reconstruction(
    cockroach_auth_database_url: str,
) -> None:
    clock = _FrozenClock()
    email = f"race.sessions.{uuid4().hex}@example.com"
    phone = _build_phone_number(suffix="0008")
    issued_session_ids: tuple[UUID, ...] = ()
    with _persistent_auth_client() as client:
        user_id = _register_active_user(
            client=client,
            email=email,
            phone_number=phone,
        )

    store = PersistentSessionIssuanceStore(
        database_url=cockroach_auth_database_url,
        now_provider=clock.now,
        max_concurrent_sessions=1,
    )

    try:
        results = _run_race(
            [
                lambda: store.issue_session(
                    user_id=user_id,
                    tenant_id=DEFAULT_AUTH_TENANT_ID,
                    role="IndividualTaxpayer",
                    device_fingerprint="device-a",
                ),
                lambda: store.issue_session(
                    user_id=user_id,
                    tenant_id=DEFAULT_AUTH_TENANT_ID,
                    role="IndividualTaxpayer",
                    device_fingerprint="device-b",
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 2
        assert not errors

        issued_session_ids = tuple(
            cast(UUID, getattr(cast(object, result), "session_id"))
            for result in successes
        )
        assert len(issued_session_ids) == 2

        reconstructed = PersistentSessionIssuanceStore(
            database_url=cockroach_auth_database_url,
            now_provider=clock.now,
            max_concurrent_sessions=1,
        )
        sessions = reconstructed.get_sessions_for_user(user_id=user_id)
        assert len(sessions) == 2
        active_sessions = [record for record in sessions if not record.is_invalidated]
        invalidated_sessions = [record for record in sessions if record.is_invalidated]
        assert len(active_sessions) == 1
        assert len(invalidated_sessions) == 1
        assert invalidated_sessions[0].invalidated_reason == (
            "session_concurrency_limit_enforced"
        )
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email,
            phone=phone,
            session_ids=issued_session_ids,
        )


def test_concurrent_failed_login_attempts_increment_lockout_durably(
    cockroach_auth_database_url: str,
) -> None:
    clock = _FrozenClock()
    login_id = _build_phone_number(suffix="0008")
    source_ip = "198.51.100.60"
    store = PersistentLoginLockoutStore(
        database_url=cockroach_auth_database_url,
        max_failed_attempts=3,
        failed_attempt_window_seconds=600,
        lockout_window_seconds=900,
        now_provider=clock.now,
    )

    try:
        first = store.register_failed_attempt(login_id_normalized=login_id, source_ip=source_ip)
        assert first is None

        results = _run_race(
            [
                lambda: store.register_failed_attempt(
                    login_id_normalized=login_id,
                    source_ip=source_ip,
                ),
                lambda: store.register_failed_attempt(
                    login_id_normalized=login_id,
                    source_ip=source_ip,
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert not errors
        assert len(successes) == 2
        assert any(result is not None for result in successes)

        reconstructed = PersistentLoginLockoutStore(
            database_url=cockroach_auth_database_url,
            max_failed_attempts=3,
            failed_attempt_window_seconds=600,
            lockout_window_seconds=900,
            now_provider=clock.now,
        )
        lockout = reconstructed.get_active_lockout(
            login_id_normalized=login_id,
            source_ip=source_ip,
        )
        assert lockout is not None

        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT failed_attempt_count, lockout_expires_at
                    FROM auth_login_lockouts
                    WHERE login_id_normalized = %s
                      AND source_ip = %s
                    """,
                    (login_id, source_ip),
                )
                row = cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 0
        assert row[1] is not None
    finally:
        store.clear_failed_attempts(login_id_normalized=login_id, source_ip=source_ip)


def test_concurrent_password_reset_confirmation_consumes_once_and_reconstructs(
    cockroach_auth_database_url: str,
) -> None:
    phone = _build_phone_number(suffix="0009")
    email = f"race.password-reset.{uuid4().hex}@example.com"

    with _persistent_auth_client() as client:
        user_id = _register_active_user(client=client, email=email, phone_number=phone)

    registration_store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
    password_reset_store = PersistentPasswordResetStore(
        database_url=cockroach_auth_database_url
    )

    try:
        challenge = initiate_password_reset_challenge(
            request_model=PasswordResetInitiateRequest(
                purpose="password_reset",
                channel="sms",
                phone_number=phone,
            ),
            idempotency_key=f"password-reset:{phone}",
            registration_store=registration_store,
            password_reset_store=password_reset_store,
        )
        challenge_record = password_reset_store.get_challenge(challenge_id=challenge.challenge_id)
        assert challenge_record is not None

        new_password_one = f"{uuid4().hex[:8]}-Aa1!Password"
        new_password_two = f"{uuid4().hex[:8]}-Bb2!Password"
        results = _run_race(
            [
                lambda: confirm_password_reset_challenge(
                    request_model=PasswordResetConfirmRequest(
                        challenge_id=challenge.challenge_id,
                        reset_code=challenge_record.reset_code,
                        new_password=new_password_one,
                    ),
                    registration_store=registration_store,
                    password_reset_store=password_reset_store,
                ),
                lambda: confirm_password_reset_challenge(
                    request_model=PasswordResetConfirmRequest(
                        challenge_id=challenge.challenge_id,
                        reset_code=challenge_record.reset_code,
                        new_password=new_password_two,
                    ),
                    registration_store=registration_store,
                    password_reset_store=password_reset_store,
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], PasswordResetError)
        assert cast(PasswordResetError, errors[0]).reason in {
            "password_reset_token_already_used",
            "password_reset_token_invalid",
        }

        reconstructed_registration = PersistentRegistrationStore(
            database_url=cockroach_auth_database_url
        )
        assert reconstructed_registration.is_password_valid(
            user_id=user_id,
            password=new_password_one,
        ) ^ reconstructed_registration.is_password_valid(
            user_id=user_id,
            password=new_password_two,
        )
        assert not reconstructed_registration.is_password_valid(
            user_id=user_id,
            password="StrongPassw0rd!",
        )
        reconstructed_reset_store = PersistentPasswordResetStore(
            database_url=cockroach_auth_database_url
        )
        persisted_challenge = reconstructed_reset_store.get_challenge(
            challenge_id=challenge.challenge_id
        )
        assert persisted_challenge is not None
        assert persisted_challenge.consumed_at is not None
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=phone,
        )


def test_concurrent_phone_change_confirmation_consumes_once_and_updates_phone(
    cockroach_auth_database_url: str,
) -> None:
    original_phone = _build_phone_number(suffix="0010")
    target_phone = _build_phone_number(suffix="0011")
    email = f"race.phone-change.{uuid4().hex}@example.com"
    with _persistent_auth_client() as client:
        user_id = _register_active_user(
            client=client,
            email=email,
            phone_number=original_phone,
        )
        registration_store = cast(
            PersistentRegistrationStore, client.app.state.registration_store
        )
        phone_verification_store = cast(
            Any, client.app.state.phone_verification_store
        )
        phone_change_store = PersistentPhoneChangeStore(
            database_url=cockroach_auth_database_url
        )

        try:
            request = create_phone_change_request(
                payload={
                    "new_phone_number": target_phone,
                    "current_password": "StrongPassw0rd!",
                },
                authorization_header=_build_auth_header(user_id=user_id),
                idempotency_key=f"phone-change:{user_id}:request",
                correlation_id=None,
                registration_store=registration_store,
                phone_verification_store=phone_verification_store,
                phone_change_store=phone_change_store,
                sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
            )
            challenge = phone_verification_store.get_challenge(
                challenge_id=request.step_up_challenge_id
            )
            assert challenge is not None

            results = _run_race(
                [
                    lambda: confirm_phone_change_request(
                        payload={
                            "request_id": request.request_id,
                            "step_up_challenge_id": request.step_up_challenge_id,
                            "step_up_otp_code": challenge.otp_code,
                        },
                        authorization_header=_build_auth_header(user_id=user_id),
                        idempotency_key=f"phone-change:{user_id}:confirm-a",
                        correlation_id=None,
                        registration_store=registration_store,
                        phone_verification_store=phone_verification_store,
                        phone_change_store=phone_change_store,
                    ),
                    lambda: confirm_phone_change_request(
                        payload={
                            "request_id": request.request_id,
                            "step_up_challenge_id": request.step_up_challenge_id,
                            "step_up_otp_code": challenge.otp_code,
                        },
                        authorization_header=_build_auth_header(user_id=user_id),
                        idempotency_key=f"phone-change:{user_id}:confirm-b",
                        correlation_id=None,
                        registration_store=registration_store,
                        phone_verification_store=phone_verification_store,
                        phone_change_store=phone_change_store,
                    ),
                ]
            )
            successes, errors = _split_race_results(results)
            assert len(successes) == 1
            assert len(errors) == 1
            assert isinstance(errors[0], PhoneChangeError)

            reconstructed_registration = PersistentRegistrationStore(
                database_url=cockroach_auth_database_url
            )
            updated_user = reconstructed_registration.get_user_by_id(user_id=user_id)
            assert updated_user is not None
            assert updated_user.phone_number_normalized == target_phone

            reconstructed_phone_change_store = PersistentPhoneChangeStore(
                database_url=cockroach_auth_database_url
            )
            persisted_request = reconstructed_phone_change_store.get_request_by_id(
                request_id=request.request_id
            )
            assert persisted_request is not None
            assert persisted_request.phone_change_state == "confirmed"
            assert persisted_request.confirmed_at is not None
        finally:
            _cleanup_user_artifacts(
                database_url=cockroach_auth_database_url,
                user_id=user_id,
                email=email.lower(),
                phone=target_phone,
                old_phone=original_phone,
            )


def test_concurrent_account_deletion_execute_and_cancel_settle_to_one_terminal_state(
    cockroach_auth_database_url: str,
) -> None:
    email = f"race.account-deletion.{uuid4().hex}@example.com"
    phone = _build_phone_number(suffix="0012")

    with _persistent_auth_client() as client:
        user_id = _register_active_user(client=client, email=email, phone_number=phone)
        login_payload = _complete_login(
            client=client,
            phone_number=phone,
            source_ip="198.51.100.70",
            correlation_prefix="account-deletion-race",
        )

    registration_store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
    deletion_store = PersistentAccountDeletionRequestStore(
        database_url=cockroach_auth_database_url
    )
    session_id = UUID(str(login_payload["session"]["session_id"]))

    try:
        request = create_account_deletion_request(
            payload={"request_reason": "account closure"},
            authorization_header=_build_auth_header(user_id=user_id),
            idempotency_key=f"account-deletion:{user_id}:request",
            correlation_id=None,
            registration_store=registration_store,
            account_deletion_store=deletion_store,
        )
        reauth_proof = deletion_store.issue_test_reauth_proof(
            user_id=user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=request.request_id,
        )
        otp_verification_id = deletion_store.issue_test_otp_verification_proof(
            user_id=user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=request.request_id,
        )
        confirm_account_deletion_request(
            payload={
                "request_id": request.request_id,
                "reauth_proof": reauth_proof,
                "otp_verification_id": otp_verification_id,
            },
            authorization_header=_build_auth_header(user_id=user_id),
            idempotency_key=f"account-deletion:{user_id}:confirm",
            correlation_id=None,
            registration_store=registration_store,
            account_deletion_store=deletion_store,
        )
        deletion_store.force_request_cooldown_expired(request_id=request.request_id)

        results = _run_race(
            [
                lambda: execute_account_deletion_request(
                    payload={"request_id": request.request_id},
                    authorization_header=_build_auth_header(user_id=user_id),
                    idempotency_key=f"account-deletion:{user_id}:execute-a",
                    correlation_id=None,
                    registration_store=registration_store,
                    account_deletion_store=deletion_store,
                ),
                lambda: cancel_account_deletion_request(
                    payload={"request_id": request.request_id},
                    authorization_header=_build_auth_header(user_id=user_id),
                    idempotency_key=f"account-deletion:{user_id}:cancel-b",
                    correlation_id=None,
                    registration_store=registration_store,
                    account_deletion_store=deletion_store,
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AccountDeletionRequestError)

        reconstructed_deletion_store = PersistentAccountDeletionRequestStore(
            database_url=cockroach_auth_database_url
        )
        persisted_request = reconstructed_deletion_store.get_request_by_id(
            request_id=request.request_id
        )
        assert persisted_request is not None
        assert persisted_request.deletion_state in {"cancelled", "executed"}

        persisted_user = registration_store.get_user_by_id(user_id=user_id)
        assert persisted_user is not None
        if persisted_request.deletion_state == "executed":
            assert persisted_user.account_state == "disabled"
            assert persisted_user.deletion_lifecycle_state == "tombstoned"
            session_store = PersistentSessionIssuanceStore(
                database_url=cockroach_auth_database_url,
                now_provider=_FrozenClock().now,
            )
            session = session_store.get_session(session_id=session_id)
            assert session is not None
            assert session.is_invalidated is True
        else:
            assert persisted_user.account_state == "active"
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=phone,
            session_ids=(session_id,),
        )


def test_repeated_account_deletion_execution_is_single_terminal_transition(
    cockroach_auth_database_url: str,
) -> None:
    email = f"race.account-deletion.repeat.{uuid4().hex}@example.com"
    phone = _build_phone_number(suffix="0013")

    with _persistent_auth_client() as client:
        user_id = _register_active_user(client=client, email=email, phone_number=phone)

    registration_store = PersistentRegistrationStore(database_url=cockroach_auth_database_url)
    deletion_store = PersistentAccountDeletionRequestStore(
        database_url=cockroach_auth_database_url
    )

    try:
        request = create_account_deletion_request(
            payload={"request_reason": "account closure"},
            authorization_header=_build_auth_header(user_id=user_id),
            idempotency_key=f"account-deletion:{user_id}:repeat-request",
            correlation_id=None,
            registration_store=registration_store,
            account_deletion_store=deletion_store,
        )
        reauth_proof = deletion_store.issue_test_reauth_proof(
            user_id=user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=request.request_id,
        )
        otp_verification_id = deletion_store.issue_test_otp_verification_proof(
            user_id=user_id,
            tenant_id=DEFAULT_AUTH_TENANT_ID,
            request_id=request.request_id,
        )
        confirm_account_deletion_request(
            payload={
                "request_id": request.request_id,
                "reauth_proof": reauth_proof,
                "otp_verification_id": otp_verification_id,
            },
            authorization_header=_build_auth_header(user_id=user_id),
            idempotency_key=f"account-deletion:{user_id}:repeat-confirm",
            correlation_id=None,
            registration_store=registration_store,
            account_deletion_store=deletion_store,
        )
        deletion_store.force_request_cooldown_expired(request_id=request.request_id)

        results = _run_race(
            [
                lambda: execute_account_deletion_request(
                    payload={"request_id": request.request_id},
                    authorization_header=_build_auth_header(user_id=user_id),
                    idempotency_key=f"account-deletion:{user_id}:execute-1",
                    correlation_id=None,
                    registration_store=registration_store,
                    account_deletion_store=deletion_store,
                ),
                lambda: execute_account_deletion_request(
                    payload={"request_id": request.request_id},
                    authorization_header=_build_auth_header(user_id=user_id),
                    idempotency_key=f"account-deletion:{user_id}:execute-2",
                    correlation_id=None,
                    registration_store=registration_store,
                    account_deletion_store=deletion_store,
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AccountDeletionRequestError)

        reconstructed_deletion_store = PersistentAccountDeletionRequestStore(
            database_url=cockroach_auth_database_url
        )
        persisted_request = reconstructed_deletion_store.get_request_by_id(
            request_id=request.request_id
        )
        assert persisted_request is not None
        assert persisted_request.deletion_state == "executed"
        assert persisted_request.executed_at is not None
        assert persisted_request.revoked_session_count is not None
        persisted_user = registration_store.get_user_by_id(user_id=user_id)
        assert persisted_user is not None
        assert persisted_user.deletion_lifecycle_state == "tombstoned"
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=user_id,
            email=email.lower(),
            phone=phone,
        )


def test_concurrent_delegation_grant_produces_one_active_pair(
    cockroach_auth_database_url: str,
) -> None:
    with _persistent_auth_client() as client:
        principal_user_id = _register_active_user(
            client=client,
            email=f"race.delegation.principal.{uuid4().hex}@example.com",
            phone_number=_build_phone_number(suffix="0014"),
        )
        delegate_user_id = _register_active_user(
            client=client,
            email=f"race.delegation.delegate.{uuid4().hex}@example.com",
            phone_number=_build_phone_number(suffix="0015"),
        )

    store = PersistentDelegationStore(database_url=cockroach_auth_database_url)

    try:
        results = _run_race(
            [
                lambda: store.grant_delegation(
                    principal_user_id=principal_user_id,
                    delegate_user_id=delegate_user_id,
                    granted_at="2026-08-11T10:00:00Z",
                ),
                lambda: store.grant_delegation(
                    principal_user_id=principal_user_id,
                    delegate_user_id=delegate_user_id,
                    granted_at="2026-08-11T10:00:00Z",
                ),
            ]
        )
        successes, errors = _split_race_results(results)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], DelegationConflictError)

        reconstructed = PersistentDelegationStore(database_url=cockroach_auth_database_url)
        active = reconstructed.get_active_delegation(
            principal_user_id=principal_user_id,
            delegate_user_id=delegate_user_id,
        )
        assert active is not None

        with psycopg.connect(cockroach_auth_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) AS total_count,
                           count(*) FILTER (WHERE is_active) AS active_count
                    FROM delegations
                    WHERE principal_user_id = %s
                      AND delegate_user_id = %s
                    """,
                    (principal_user_id, delegate_user_id),
                )
                row = cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 1
        assert int(row[1]) == 1
    finally:
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=principal_user_id,
            email=f"race.delegation.principal.{uuid4().hex}@example.com",
            phone=_build_phone_number(suffix="0014"),
        )
        _cleanup_user_artifacts(
            database_url=cockroach_auth_database_url,
            user_id=delegate_user_id,
            email=f"race.delegation.delegate.{uuid4().hex}@example.com",
            phone=_build_phone_number(suffix="0015"),
        )
