"""Runtime tests for deterministic auth session policy behavior."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
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
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import SessionIssuanceError
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore
from services.auth.app.config import get_auth_session_absolute_lifetime_seconds


class _FrozenClock:
    """Provide deterministic time controls for session-policy tests."""

    def __init__(self) -> None:
        self._current = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def policy_store_and_clock() -> Iterator[tuple[InMemorySessionIssuanceStore, _FrozenClock]]:
    """Create deterministic in-memory session store with tuned policy defaults."""

    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=60,
        absolute_lifetime_seconds=180,
        warning_window_seconds=20,
        max_concurrent_sessions=2,
    )
    yield store, clock


@pytest.fixture()
def login_client_and_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemorySessionIssuanceStore,
            InMemoryEmailVerificationStore,
            InMemoryPhoneVerificationStore,
        ]
    ]
    ):
    """Create isolated auth app client with deterministic stores for regression checks."""

    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    session_issuance_store = InMemorySessionIssuanceStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    app.state.registration_store = registration_store
    app.state.session_issuance_store = session_issuance_store
    app.state.email_verification_store = email_verification_store
    app.state.phone_verification_store = phone_verification_store
    app.state.login_lockout_store = InMemoryLoginLockoutStore()
    app.state.login_step_up_store = InMemoryLoginStepUpStore()
    with TestClient(app) as client:
        yield (
            client,
            registration_store,
            session_issuance_store,
            email_verification_store,
            phone_verification_store,
        )
    reset_default_registration_store()


def test_session_within_inactivity_and_absolute_bounds_is_active(
    policy_store_and_clock: tuple[InMemorySessionIssuanceStore, _FrozenClock],
) -> None:
    store, _ = policy_store_and_clock
    user_id = uuid4()
    issued = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-a",
    )

    evaluation = store.evaluate_session(session_id=issued.session_id)
    assert evaluation is not None
    assert evaluation.status == "active"
    assert evaluation.reason_code is None
    assert evaluation.is_warning_window is False
    assert evaluation.extension_allowed is False


def test_session_inactivity_timeout_is_enforced_deterministically(
    policy_store_and_clock: tuple[InMemorySessionIssuanceStore, _FrozenClock],
) -> None:
    store, clock = policy_store_and_clock
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )

    clock.advance(seconds=61)
    evaluation = store.evaluate_session(session_id=issued.session_id)
    assert evaluation is not None
    assert evaluation.status == "expired"
    assert evaluation.reason_code == "session_inactivity_timeout"

    repeated = store.evaluate_session(session_id=issued.session_id)
    assert repeated is not None
    assert repeated.status == "expired"
    assert repeated.reason_code == "session_inactivity_timeout"


def test_session_absolute_lifetime_cap_applies_even_with_activity(
    policy_store_and_clock: tuple[InMemorySessionIssuanceStore, _FrozenClock],
) -> None:
    store, clock = policy_store_and_clock
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )

    clock.advance(seconds=50)
    touched_first = store.touch_session_activity(session_id=issued.session_id)
    assert touched_first is not None
    assert touched_first.status == "active"

    clock.advance(seconds=50)
    touched_second = store.touch_session_activity(session_id=issued.session_id)
    assert touched_second is not None
    assert touched_second.status == "active"

    clock.advance(seconds=90)
    evaluation = store.evaluate_session(session_id=issued.session_id)
    assert evaluation is not None
    assert evaluation.status == "expired"
    assert evaluation.reason_code == "session_absolute_expiry"

    repeated = store.evaluate_session(session_id=issued.session_id)
    assert repeated is not None
    assert repeated.status == "expired"
    assert repeated.reason_code == "session_absolute_expiry"


def test_session_warning_window_and_extension_allowance_are_deterministic(
    policy_store_and_clock: tuple[InMemorySessionIssuanceStore, _FrozenClock],
) -> None:
    store, clock = policy_store_and_clock
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )

    clock.advance(seconds=45)
    evaluation_warning = store.evaluate_session(session_id=issued.session_id)
    assert evaluation_warning is not None
    assert evaluation_warning.status == "warning"
    assert evaluation_warning.is_warning_window is True
    assert evaluation_warning.extension_allowed is True

    extended = store.extend_session(session_id=issued.session_id)
    assert extended.status == "active"
    assert extended.is_warning_window is False
    assert extended.extension_allowed is False

    with pytest.raises(SessionIssuanceError) as extension_error:
        store.extend_session(session_id=issued.session_id)
    assert extension_error.value.error_code == "session_extension_not_allowed"
    assert extension_error.value.reason == "session_extension_not_allowed"


def test_session_warning_uses_effective_expiry_when_absolute_cap_is_shorter() -> None:
    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(
        now_provider=clock.now,
        inactivity_timeout_seconds=180,
        absolute_lifetime_seconds=60,
        warning_window_seconds=20,
        max_concurrent_sessions=2,
    )
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )

    issued_eval = store.evaluate_session(session_id=issued.session_id)
    assert issued_eval is not None
    assert issued_eval.expires_at == issued_eval.absolute_expires_at
    assert issued_eval.inactivity_expires_at != issued_eval.absolute_expires_at

    clock.advance(seconds=41)
    warning_eval = store.evaluate_session(session_id=issued.session_id)
    assert warning_eval is not None
    assert warning_eval.status == "warning"
    assert warning_eval.extension_allowed is False

    with pytest.raises(SessionIssuanceError) as extension_error:
        store.extend_session(session_id=issued.session_id)
    assert extension_error.value.error_code == "session_extension_not_allowed"
    assert extension_error.value.reason == "session_extension_not_allowed"


def test_session_concurrency_limit_evicts_oldest_deterministically(
    policy_store_and_clock: tuple[InMemorySessionIssuanceStore, _FrozenClock],
) -> None:
    store, clock = policy_store_and_clock
    user_id = uuid4()
    first = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-1",
    )
    clock.advance(seconds=1)
    second = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-2",
    )
    clock.advance(seconds=1)
    third = store.issue_session(
        user_id=user_id,
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint="device-3",
    )

    assert third.evicted_session_ids == (first.session_id,)

    first_eval = store.evaluate_session(session_id=first.session_id)
    second_eval = store.evaluate_session(session_id=second.session_id)
    third_eval = store.evaluate_session(session_id=third.session_id)
    assert first_eval is not None
    assert first_eval.status == "invalidated"
    assert first_eval.reason_code == "session_concurrency_limit_enforced"
    assert second_eval is not None and second_eval.status in {"active", "warning"}
    assert third_eval is not None and third_eval.status in {"active", "warning"}


def test_session_policy_default_configuration_matches_fr_auth_011_baseline() -> None:
    clock = _FrozenClock()
    store = InMemorySessionIssuanceStore(now_provider=clock.now)
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )
    evaluation = store.evaluate_session(session_id=issued.session_id)
    assert evaluation is not None

    issued_at = datetime.fromisoformat(evaluation.issued_at.replace("Z", "+00:00"))
    inactivity_expires_at = datetime.fromisoformat(
        evaluation.inactivity_expires_at.replace("Z", "+00:00")
    )
    absolute_expires_at = datetime.fromisoformat(
        evaluation.absolute_expires_at.replace("Z", "+00:00")
    )
    warning_started_at = datetime.fromisoformat(
        evaluation.warning_window_started_at.replace("Z", "+00:00")
    )

    assert int((inactivity_expires_at - issued_at).total_seconds()) == 1800
    assert int((absolute_expires_at - issued_at).total_seconds()) == 604800
    assert int((inactivity_expires_at - warning_started_at).total_seconds()) == 600


def test_auth_session_absolute_lifetime_is_capped_to_seven_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS", str(14 * 24 * 60 * 60))
    assert get_auth_session_absolute_lifetime_seconds() == 604800


def test_session_policy_evaluation_is_deterministic_for_same_state(
    policy_store_and_clock: tuple[InMemorySessionIssuanceStore, _FrozenClock],
) -> None:
    store, clock = policy_store_and_clock
    issued = store.issue_session(
        user_id=uuid4(),
        tenant_id="default_tenant",
        role="IndividualTaxpayer",
        device_fingerprint=None,
    )
    clock.advance(seconds=45)
    first = store.evaluate_session(session_id=issued.session_id)
    second = store.evaluate_session(session_id=issued.session_id)
    assert first is not None and second is not None
    assert first.status == "warning"
    assert second.status == "warning"
    assert first.reason_code == second.reason_code
    assert first.is_warning_window == second.is_warning_window
    assert first.extension_allowed == second.extension_allowed


def test_login_session_issuance_still_works_with_policy_engine_enabled(
    login_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
    ],
) -> None:
    (
        client,
        registration_store,
        session_issuance_store,
        _,
        phone_verification_store,
    ) = login_client_and_stores
    user_id = _register_user(
        client=client,
        email="session-policy-login@example.com",
        phone_number="+254744000901",
    )
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-30T12:00:00Z",
    )

    pending_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "session-policy-login-pending",
            "X-Forwarded-For": "203.0.113.32",
        },
        json={"login_id": "+254744000901", "password": "StrongPassw0rd!"},
    )
    pending_payload = _response_json(pending_response)
    assert pending_response.status_code == 200
    assert pending_payload["status"] == "pending_step_up"

    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    final_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "session-policy-login-final",
            "X-Forwarded-For": "203.0.113.32",
        },
        json={
            "login_id": "+254744000901",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    final_payload = _response_json(final_response)
    assert final_response.status_code == 200
    assert final_payload["status"] == "authenticated"
    session_id = UUID(cast(str, cast(dict[str, Any], final_payload["session"])["session_id"]))
    evaluation = session_issuance_store.evaluate_session(session_id=session_id)
    assert evaluation is not None
    assert evaluation.status == "active"


def _register_user(
    *,
    client: TestClient,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "auth-sessions-registration-corr"},
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
    return UUID(cast(str, payload["user_id"]))


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
