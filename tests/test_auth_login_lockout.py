"""Focused deterministic lockout tests for login second-factor flow."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.login import InMemoryLoginStepUpStore
from services.auth.app.login import InMemoryLoginLockoutStore
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore

_FAILED_ATTEMPT_WINDOW_SECONDS = 60
_LOCKOUT_DURATION_SECONDS = 120
_MAX_FAILED_ATTEMPTS = 3


class _FrozenClock:
    """Provide deterministic lockout-clock control for assertions."""

    def __init__(self) -> None:
        self._current = datetime(2026, 3, 31, 8, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def client_and_state() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryPhoneVerificationStore,
            InMemoryLoginLockoutStore,
            _FrozenClock,
        ]
    ]
):
    """Create isolated auth app client with deterministic in-memory state."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    session_issuance_store = InMemorySessionIssuanceStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    lockout_clock = _FrozenClock()
    login_lockout_store = InMemoryLoginLockoutStore(
        max_failed_attempts=_MAX_FAILED_ATTEMPTS,
        failed_attempt_window_seconds=_FAILED_ATTEMPT_WINDOW_SECONDS,
        lockout_window_seconds=_LOCKOUT_DURATION_SECONDS,
        now_provider=lockout_clock.now,
    )
    login_step_up_store = InMemoryLoginStepUpStore()

    app.state.registration_store = registration_store
    app.state.session_issuance_store = session_issuance_store
    app.state.phone_verification_store = phone_verification_store
    app.state.email_verification_store = email_verification_store
    app.state.login_lockout_store = login_lockout_store
    app.state.login_step_up_store = login_step_up_store

    with TestClient(app) as test_client:
        yield (
            test_client,
            registration_store,
            phone_verification_store,
            login_lockout_store,
            lockout_clock,
        )
    reset_default_registration_store()


def test_login_lockout_threshold_and_active_state_are_deterministic(
    client_and_state: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _ = client_and_state
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="lockout.threshold@example.com",
        phone_number="+254733430001",
    )

    headers = {"X-Correlation-ID": "lockout-threshold-corr", "X-Forwarded-For": "198.51.100.10"}
    invalid_payload = {"login_id": "+254733430001", "password": "WrongPassw0rd!"}

    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold_error = _extract_error_detail(threshold)
    assert threshold.status_code == 403
    assert threshold_error["error_code"] == "login_lockout_threshold_exceeded"
    assert threshold_error["reason"] == "login_lockout_threshold_exceeded"
    assert isinstance(threshold_error["lockout_expires_at"], str)
    assert threshold_error["lockout_remaining_seconds"] == _LOCKOUT_DURATION_SECONDS

    active_one = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_two = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_one_error = _extract_error_detail(active_one)
    active_two_error = _extract_error_detail(active_two)
    assert active_one.status_code == 403
    assert active_one_error["error_code"] == "login_lockout_active"
    assert active_one_error["reason"] == "login_lockout_active"
    assert canonical_json_dumps(active_one_error) == canonical_json_dumps(active_two_error)


def test_login_lockout_expires_and_returns_to_pending_step_up(
    client_and_state: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, lockout_clock = client_and_state
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="lockout.recovery@example.com",
        phone_number="+254733430002",
    )

    headers = {"X-Correlation-ID": "lockout-recovery-corr", "X-Forwarded-For": "198.51.100.11"}
    invalid_payload = {"login_id": "+254733430002", "password": "WrongPassw0rd!"}
    for _ in range(_MAX_FAILED_ATTEMPTS):
        client.post("/v1/auth/login", headers=headers, json=invalid_payload)

    locked = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    locked_error = _extract_error_detail(locked)
    assert locked.status_code == 403
    assert locked_error["error_code"] == "login_lockout_active"

    lockout_clock.advance(seconds=_LOCKOUT_DURATION_SECONDS + 1)
    after_lockout = client.post(
        "/v1/auth/login",
        headers=headers,
        json={"login_id": "+254733430002", "password": "StrongPassw0rd!"},
    )
    payload = _response_json(after_lockout)
    assert after_lockout.status_code == 200
    assert payload["status"] == "pending_step_up"
    assert payload["login_status"] == "pending_step_up"
    assert "access_token" not in payload
    assert "refresh_token" not in payload


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "lockout-register-corr"},
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
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-31T08:00:00Z",
    )
    return user_id


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
