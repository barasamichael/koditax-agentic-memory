"""Runtime tests for deterministic Phase 8 registration baseline endpoint."""

from __future__ import annotations

import re
import json
from uuid import UUID
from typing import Any
from typing import cast
from datetime import UTC
from datetime import datetime
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import PersistentRegistrationStore
from services.auth.app.registration import get_default_registration_store
from services.auth.app.registration import reset_default_registration_store


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Create isolated auth app client with deterministic registration store reset."""

    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    reset_default_registration_store()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    reset_default_registration_store()


def test_registration_positive_request_succeeds(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-positive-corr"},
        json={
            "email": "sample.user@example.com",
            "phone_number": "+254700000001",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["registration_status"] == "pending_verification"
    UUID(cast(str, payload["user_id"]))
    assert cast(str, payload["created_at"]).endswith("Z")
    assert "password" not in payload
    assert "kra_pin" not in payload

    registration_store = get_default_registration_store()
    registered_user = registration_store.get_user_by_id(user_id=UUID(cast(str, payload["user_id"])))
    assert registered_user is not None
    assert len(registered_user.kra_pin_hash) == 64
    assert registered_user.kra_pin_hash != "A123456789Z"
    assert registered_user.password_hash.startswith("$2")
    assert not re.fullmatch(r"[0-9a-f]{64}", registered_user.password_hash)
    bcrypt_match = re.match(r"^\$2[aby]\$(\d{2})\$", registered_user.password_hash)
    assert bcrypt_match is not None
    assert int(bcrypt_match.group(1)) >= 12
    assert registered_user.password_history_hashes == (registered_user.password_hash,)
    assert registered_user.account_state == "pending_verification"
    assert registered_user.verification_state == "pending_verification"


def test_registration_invalid_email_is_rejected_deterministically(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-invalid-email-corr"},
        json={
            "email": "invalid-email",
            "phone_number": "+254700000002",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "registration_invalid_email"
    assert error["message"] == "Registration email format is invalid."
    assert error["reason"] == "registration_invalid_email"


def test_registration_weak_password_is_rejected_deterministically(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-weak-password-corr"},
        json={
            "email": "user.weak-password@example.com",
            "phone_number": "+254700000003",
            "kra_pin": "A123456789Z",
            "password": "weak-pass",
            "role": "IndividualTaxpayer",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "registration_weak_password"
    assert error["reason"] == "registration_weak_password"


def test_registration_duplicate_phone_is_rejected_deterministically(
    client: TestClient,
) -> None:
    first = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-duplicate-first-corr"},
        json={
            "email": "dup.user@example.com",
            "phone_number": "+254700000004",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    duplicate = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-duplicate-second-corr"},
        json={
            "email": "dup.user.2@example.com",
            "phone_number": "+254700000004",
            "kra_pin": "A123456788Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    assert first.status_code == 201
    error = _extract_error_detail(duplicate)
    assert duplicate.status_code == 409
    assert error["error_code"] == "registration_duplicate_phone"
    assert error["message"] == "Registration request conflicts with an existing account."
    assert error["reason"] == "registration_duplicate_phone"


def test_registration_repeated_invalid_input_returns_identical_error_payload(
    client: TestClient,
) -> None:
    headers = {"X-Correlation-ID": "reg-determinism-corr"}
    invalid_payload = {
        "email": "not-an-email",
        "phone_number": "+254700000005",
        "kra_pin": "A123456789Z",
        "password": "StrongPassw0rd!",
        "role": "IndividualTaxpayer",
    }

    first = client.post("/v1/auth/register", headers=headers, json=invalid_payload)
    second = client.post("/v1/auth/register", headers=headers, json=invalid_payload)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_registration_missing_kra_pin_is_rejected_deterministically(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-missing-kra-corr"},
        json={
            "email": "missing.kra@example.com",
            "phone_number": "+254700000006",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "registration_missing_kra_pin"
    assert error["reason"] == "registration_missing_kra_pin"


def test_registration_invalid_kra_pin_is_rejected_deterministically(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "reg-invalid-kra-corr"},
        json={
            "email": "invalid.kra@example.com",
            "phone_number": "+254700000007",
            "kra_pin": "123456789",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "registration_invalid_kra_pin"
    assert error["reason"] == "registration_invalid_kra_pin"


def test_persistent_registration_reuses_one_user_identity_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PersistentRegistrationStore(database_url="postgresql://example.invalid/kodi_dev")
    seen_user_ids: list[UUID] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self._params: tuple[object, ...] | None = None

        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(
            self,
            exc_type: object | None,
            exc: object | None,
            tb: object | None,
        ) -> bool:
            return False

        def execute(
            self,
            sql: str,
            params: tuple[object, ...] | None = None,
        ) -> None:
            del sql
            assert params is not None
            self._params = params
            seen_user_ids.append(cast(UUID, params[0]))

        def fetchone(self) -> tuple[object, ...] | None:
            assert self._params is not None
            params = self._params
            return (
                params[0],
                params[1],
                params[2],
                params[3],
                params[7],
                params[4],
                params[5],
                params[9],
                params[10],
                None,
                None,
                params[11],
                None,
                json.loads(cast(str, params[8])),
            )

    class _FakeConnection:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    def _fake_execute_auth_database_transaction(
        *,
        database_url: str,
        transaction_callback: Any,
        reconcile_callback: Any = None,
        retry_policy: Any = None,
        retry_jitter: float = 0.0,
        sleep: Any = None,
        metrics_emitter: Any = None,
    ) -> RegisteredUserRecord:
        del database_url, retry_policy, retry_jitter, sleep, metrics_emitter, reconcile_callback
        fake_connection = _FakeConnection()
        first_result = transaction_callback(fake_connection)
        second_result = transaction_callback(fake_connection)
        assert first_result.user_id == second_result.user_id
        return second_result

    monkeypatch.setattr(
        "services.auth.app.registration.execute_auth_database_transaction",
        _fake_execute_auth_database_transaction,
    )

    record = store.register_user(
        email_normalized="retry.user@example.com",
        phone_number_normalized="+254700123456",
        kra_pin_hash="a" * 64,
        password_hash="$2b$12$C6UzMDM.H6dfI/f/IK5hVe7F0KqX1n4nJb9j6R6JrW9C5Z1Xz5n8G",
        role="IndividualTaxpayer",
        created_at="2026-08-08T00:00:00Z",
    )

    assert record.user_id == seen_user_ids[0]
    assert seen_user_ids[0] == seen_user_ids[1]


def test_persistent_registration_reconciles_ambiguous_commit_by_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PersistentRegistrationStore(database_url="postgresql://example.invalid/kodi_dev")
    expected_user_id = UUID("11111111-1111-1111-1111-111111111111")
    reconciled_record = RegisteredUserRecord(
        user_id=expected_user_id,
        email_normalized="ambiguous.user@example.com",
        phone_number_normalized="+254700123457",
        kra_pin_hash="b" * 64,
        password_hash="$2b$12$C6UzMDM.H6dfI/f/IK5hVe7F0KqX1n4nJb9j6R6JrW9C5Z1Xz5n8G",
        role="IndividualTaxpayer",
        created_at="2026-08-08T00:00:00Z",
        verification_state="pending_verification",
        verified_at=None,
    )

    class _FakeCursor:
        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(
            self,
            exc_type: object | None,
            exc: object | None,
            tb: object | None,
        ) -> bool:
            return False

        def execute(
            self,
            sql: str,
            params: tuple[object, ...] | None = None,
        ) -> None:
            del sql, params

        def fetchone(self) -> tuple[object, ...] | None:
            return (
                expected_user_id,
                "ambiguous.user@example.com",
                "+254700123457",
                "b" * 64,
                "$2b$12$C6UzMDM.H6dfI/f/IK5hVe7F0KqX1n4nJb9j6R6JrW9C5Z1Xz5n8G",
                "IndividualTaxpayer",
                datetime(2026, 8, 8, 0, 0, tzinfo=UTC),
                "pending_verification",
                "pending_verification",
                None,
                None,
                "none",
                None,
                ["$2b$12$C6UzMDM.H6dfI/f/IK5hVe7F0KqX1n4nJb9j6R6JrW9C5Z1Xz5n8G"],
            )

    class _FakeConnection:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    def _fake_execute_auth_database_transaction(
        *,
        database_url: str,
        transaction_callback: Any,
        reconcile_callback: Any = None,
        retry_policy: Any = None,
        retry_jitter: float = 0.0,
        sleep: Any = None,
        metrics_emitter: Any = None,
    ) -> RegisteredUserRecord:
        del database_url, retry_policy, retry_jitter, sleep, metrics_emitter
        assert reconcile_callback is not None
        transaction_callback(_FakeConnection())
        return reconcile_callback()

    monkeypatch.setattr(
        "services.auth.app.registration.execute_auth_database_transaction",
        _fake_execute_auth_database_transaction,
    )
    monkeypatch.setattr("services.auth.app.registration.uuid4", lambda: expected_user_id)
    monkeypatch.setattr(
        store,
        "get_user_by_id",
        lambda *, user_id: reconciled_record if user_id == expected_user_id else None,
    )

    record = store.register_user(
        email_normalized="ambiguous.user@example.com",
        phone_number_normalized="+254700123457",
        kra_pin_hash="b" * 64,
        password_hash="$2b$12$C6UzMDM.H6dfI/f/IK5hVe7F0KqX1n4nJb9j6R6JrW9C5Z1Xz5n8G",
        role="IndividualTaxpayer",
        created_at="2026-08-08T00:00:00Z",
    )

    assert record == reconciled_record


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
