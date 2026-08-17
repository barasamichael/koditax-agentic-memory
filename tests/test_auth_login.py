"""Runtime tests for deterministic login with mandatory OTP step-up gating."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.config import get_auth_login_phone_otp_enabled
from services.auth.app.config import get_auth_otp_channel_policy_for_purpose
from services.auth.app.login import LoginStepUpState
from services.auth.app.login import LoginRequest
from services.auth.app.login import login_with_credentials
from services.auth.app.login import InMemoryLoginStepUpStore
from services.auth.app.login import InMemoryLoginLockoutStore
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import build_password_hash
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore
from services.auth.app.otp_delivery_adapters import StubSmsOtpDeliveryAdapter

_FAILED_ATTEMPT_WINDOW_SECONDS = 60
_LOCKOUT_DURATION_SECONDS = 120
_MAX_FAILED_ATTEMPTS = 3


class _FrozenClock:
    """Provide deterministic lockout clock control for login tests."""

    def __init__(self) -> None:
        self._current = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def client_and_store(
    monkeypatch: pytest.MonkeyPatch,
) -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemorySessionIssuanceStore,
            InMemoryEmailVerificationStore,
            InMemoryPhoneVerificationStore,
            InMemoryLoginLockoutStore,
            InMemoryLoginStepUpStore,
            _FrozenClock,
        ]
    ]
):
    """Create isolated auth app client with deterministic in-memory stores."""

    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    session_issuance_store = InMemorySessionIssuanceStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
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
    app.state.email_verification_store = email_verification_store
    app.state.phone_verification_store = phone_verification_store
    app.state.login_lockout_store = login_lockout_store
    app.state.login_step_up_store = login_step_up_store

    with TestClient(app) as test_client:
        yield (
            test_client,
            registration_store,
            session_issuance_store,
            email_verification_store,
            phone_verification_store,
            login_lockout_store,
            login_step_up_store,
            lockout_clock,
        )
    reset_default_registration_store()


def test_login_requires_step_up_before_session_issuance(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, session_issuance_store, _, _, _, _, _ = client_and_store
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.pending.stepup@example.com",
        phone_number="+254733410001",
    )

    pending_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-required-corr",
            "X-Forwarded-For": "203.0.113.20",
        },
        json={"login_id": "+254733410001", "password": "StrongPassw0rd!"},
    )
    pending_payload = _response_json(pending_response)

    assert pending_response.status_code == 200
    assert pending_payload["login_status"] == "pending_step_up"
    assert pending_payload["status"] == "pending_step_up"
    assert pending_payload["step_up_required"] is True
    assert pending_payload["step_up_purpose"] == "login_step_up"
    assert pending_payload["step_up_channel"] == "email"
    assert "access_token" not in pending_payload
    assert "refresh_token" not in pending_payload
    assert session_issuance_store.get_total_session_count() == 0
    assert registration_store.get_user_by_id(user_id=user_id) is not None


def test_login_phone_otp_policy_defaults_to_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_LOGIN_PHONE_OTP_ENABLED", raising=False)
    policy = get_auth_otp_channel_policy_for_purpose("login_step_up")
    assert get_auth_login_phone_otp_enabled() is False
    assert policy.enabled is False
    assert policy.channel == "email"


def test_login_phone_otp_policy_can_be_reenabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_LOGIN_PHONE_OTP_ENABLED", "true")
    registration_store = InMemoryRegistrationStore()
    session_issuance_store = InMemorySessionIssuanceStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    login_lockout_store = InMemoryLoginLockoutStore(
        max_failed_attempts=_MAX_FAILED_ATTEMPTS,
        failed_attempt_window_seconds=_FAILED_ATTEMPT_WINDOW_SECONDS,
        lockout_window_seconds=_LOCKOUT_DURATION_SECONDS,
        now_provider=_FrozenClock().now,
    )
    login_step_up_store = InMemoryLoginStepUpStore()

    created = registration_store.register_user(
        email_normalized="login.reenable@example.com",
        phone_number_normalized="+254733410099",
        kra_pin_hash=sha256(b"A123456789Z").hexdigest(),
        password_hash=build_password_hash(password="StrongPassw0rd!"),
        role="IndividualTaxpayer",
        created_at="2026-03-30T12:00:00Z",
    )
    registration_store.mark_user_email_verified(
        user_id=created.user_id,
        verified_at="2026-03-30T12:01:00Z",
    )
    pending = login_with_credentials(
        payload=LoginRequest(
            login_id="+254733410099",
            password="StrongPassw0rd!",
        ).model_dump(mode="python"),
        source_ip="203.0.113.250",
        registration_store=registration_store,
        session_issuance_store=session_issuance_store,
        login_lockout_store=login_lockout_store,
        login_step_up_store=login_step_up_store,
        email_verification_store=email_verification_store,
        phone_verification_store=phone_verification_store,
        sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
    )
    assert pending.status == "pending_step_up"
    assert pending.step_up_channel == "sms"
    challenge = phone_verification_store.get_challenge(challenge_id=pending.step_up_challenge_id)
    assert challenge is not None
    success = login_with_credentials(
        payload=LoginRequest(
            login_id="+254733410099",
            password="StrongPassw0rd!",
            step_up_challenge_id=pending.step_up_challenge_id,
            step_up_otp_code=challenge.otp_code,
        ).model_dump(mode="python"),
        source_ip="203.0.113.250",
        registration_store=registration_store,
        session_issuance_store=session_issuance_store,
        login_lockout_store=login_lockout_store,
        login_step_up_store=login_step_up_store,
        email_verification_store=email_verification_store,
        phone_verification_store=phone_verification_store,
        sms_delivery_adapter=StubSmsOtpDeliveryAdapter(),
    )
    assert success.status == "authenticated"


def test_login_partial_step_up_payload_is_rejected_with_step_up_required_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.required.partial@example.com",
        phone_number="+254733410018",
    )
    payload = {
        "login_id": "+254733410018",
        "password": "StrongPassw0rd!",
        "step_up_challenge_id": str(UUID("11111111-1111-1111-1111-111111111111")),
    }
    headers = {
        "X-Correlation-ID": "login-step-up-required-partial-corr",
        "X-Forwarded-For": "203.0.113.36",
    }

    first = client.post("/v1/auth/login", headers=headers, json=payload)
    second = client.post("/v1/auth/login", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 403
    assert second.status_code == 403
    assert first_error["error_code"] == "login_step_up_required"
    assert first_error["reason"] == "login_step_up_required"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_login_pending_step_up_response_is_deterministic_for_retries(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, session_issuance_store, _, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.pending.retry@example.com",
        phone_number="+254733410009",
    )

    headers = {
        "X-Correlation-ID": "login-step-up-retry-corr",
        "X-Forwarded-For": "203.0.113.120",
    }
    payload = {"login_id": "+254733410009", "password": "StrongPassw0rd!"}
    first_response = client.post("/v1/auth/login", headers=headers, json=payload)
    second_response = client.post("/v1/auth/login", headers=headers, json=payload)
    first_payload = _response_json(first_response)
    second_payload = _response_json(second_response)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_payload["login_status"] == "pending_step_up"
    assert second_payload["login_status"] == "pending_step_up"
    assert first_payload["status"] == "pending_step_up"
    assert second_payload["status"] == "pending_step_up"
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)
    assert session_issuance_store.get_total_session_count() == 0


def test_login_email_identifier_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _, _, _, _ = client_and_store
    response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-email-identifier-rejected",
            "X-Forwarded-For": "203.0.113.201",
        },
        json={"login_id": "user@example.com", "password": "StrongPassw0rd!"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "login_identifier_unsupported_type"
    assert error["reason"] == "login_identifier_unsupported_type"


def test_login_malformed_phone_identifier_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _, _, _, _ = client_and_store
    response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-phone-format-rejected",
            "X-Forwarded-For": "203.0.113.202",
        },
        json={"login_id": "abc-123", "password": "StrongPassw0rd!"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "login_identifier_invalid_format"
    assert error["reason"] == "login_identifier_invalid_format"


def test_login_unknown_phone_identifier_returns_canonical_invalid_credentials(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _, _, _, _ = client_and_store
    response = client.post(
        "/v1/auth/login",
        headers={"X-Correlation-ID": "login-unknown-phone", "X-Forwarded-For": "203.0.113.203"},
        json={"login_id": "+254733419999", "password": "StrongPassw0rd!"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["error_code"] == "login_invalid_credentials"
    assert error["reason"] == "login_invalid_credentials"


def test_login_with_valid_credentials_and_step_up_otp_issues_authenticated_session(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, session_issuance_store, email_store, _, _, _, _ = client_and_store
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.success@example.com",
        phone_number="+254733410002",
    )

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410002",
        password="StrongPassw0rd!",
        correlation_id="login-step-up-success-pending",
        source_ip="203.0.113.21",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    authenticated_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-success-final",
            "X-Forwarded-For": "203.0.113.21",
        },
        json={
            "login_id": "+254733410002",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    authenticated_payload = _response_json(authenticated_response)

    assert authenticated_response.status_code == 200
    assert authenticated_payload["status"] == "authenticated"
    assert isinstance(authenticated_payload["access_token"], str)
    assert isinstance(authenticated_payload["refresh_token"], str)
    session = cast(dict[str, Any], authenticated_payload["session"])
    assert session["user_id"] == str(user_id)
    assert session_issuance_store.get_total_session_count() == 1


def test_login_local_kenyan_phone_identifier_normalizes_and_authenticates(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, session_issuance_store, email_store, _, _, _, _ = client_and_store
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.phone.local-format@example.com",
        phone_number="+254733410017",
    )

    pending_payload = _start_step_up_login(
        client=client,
        login_id="0733410017",
        password="StrongPassw0rd!",
        correlation_id="login-local-phone-pending",
        source_ip="203.0.113.35",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    authenticated_response = client.post(
        "/v1/auth/login",
        headers={"X-Correlation-ID": "login-local-phone-final", "X-Forwarded-For": "203.0.113.35"},
        json={
            "login_id": "+254733410017",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    authenticated_payload = _response_json(authenticated_response)

    assert authenticated_response.status_code == 200
    assert authenticated_payload["status"] == "authenticated"
    session = cast(dict[str, Any], authenticated_payload["session"])
    assert session["user_id"] == str(user_id)
    assert session_issuance_store.get_total_session_count() == 1


def test_login_invalid_step_up_otp_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, email_store, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.invalid@example.com",
        phone_number="+254733410003",
    )

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410003",
        password="StrongPassw0rd!",
        correlation_id="login-step-up-invalid-pending",
        source_ip="203.0.113.22",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    valid_otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp_code = "000000" if valid_otp_code != "000000" else "999999"

    first_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-invalid-repeat",
            "X-Forwarded-For": "203.0.113.22",
        },
        json={
            "login_id": "+254733410003",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": invalid_otp_code,
        },
    )
    second_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-invalid-repeat",
            "X-Forwarded-For": "203.0.113.22",
        },
        json={
            "login_id": "+254733410003",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": invalid_otp_code,
        },
    )

    first_error = _extract_error_detail(first_response)
    second_error = _extract_error_detail(second_response)
    assert first_response.status_code == 409
    assert first_error["error_code"] == "login_step_up_otp_invalid"
    assert first_error["reason"] == "login_step_up_otp_invalid"
    assert second_response.status_code == 409
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_login_step_up_otp_attempt_limit_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, email_store, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.attempt-limit@example.com",
        phone_number="+254733410015",
    )

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410015",
        password="StrongPassw0rd!",
        correlation_id="login-step-up-attempt-limit-pending",
        source_ip="203.0.113.29",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    challenge_record = email_store.get_challenge(challenge_id=challenge_id)
    assert challenge_record is not None
    invalid_otp_code = "000000"

    for attempt_index in range(3):
        response = client.post(
            "/v1/auth/login",
            headers={
                "X-Correlation-ID": "login-step-up-attempt-limit-final",
                "X-Forwarded-For": "203.0.113.29",
            },
            json={
                "login_id": "+254733410015",
                "password": "StrongPassw0rd!",
                "step_up_challenge_id": str(challenge_id),
                "step_up_otp_code": invalid_otp_code,
            },
        )
        error = _extract_error_detail(response)
        assert response.status_code == 409
        persisted = email_store.get_challenge(challenge_id=challenge_id)
        assert persisted is not None
        if attempt_index < 2:
            assert error["error_code"] == "login_step_up_otp_invalid"
            assert error["reason"] == "login_step_up_otp_invalid"
            assert persisted.failed_attempt_count == attempt_index + 1
        else:
            assert error["error_code"] == "login_step_up_otp_attempt_limit_exceeded"
            assert error["reason"] == "login_step_up_otp_attempt_limit_exceeded"
            assert persisted.failed_attempt_count == 3


def test_login_expired_step_up_otp_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, email_store, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.expired@example.com",
        phone_number="+254733410004",
    )
    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410004",
        password="StrongPassw0rd!",
        correlation_id="login-step-up-expired-pending",
        source_ip="203.0.113.23",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    email_store.force_expire_challenge(challenge_id=challenge_id)

    response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-expired-final",
            "X-Forwarded-For": "203.0.113.23",
        },
        json={
            "login_id": "+254733410004",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "login_step_up_challenge_expired"
    assert error["reason"] == "login_step_up_challenge_expired"


def test_login_step_up_challenge_reuse_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    (
        client,
        registration_store,
        _session_store,
        email_store,
        _phone_store,
        _lockout_store,
        login_step_up_store,
        _clock,
    ) = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.reuse@example.com",
        phone_number="+254733410016",
    )

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410016",
        password="StrongPassw0rd!",
        correlation_id="login-step-up-reuse-pending",
        source_ip="203.0.113.30",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    first_success = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-reuse-first",
            "X-Forwarded-For": "203.0.113.30",
        },
        json={
            "login_id": "+254733410016",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    assert first_success.status_code == 200

    challenge_record = email_store.get_challenge(challenge_id=challenge_id)
    assert challenge_record is not None
    login_step_up_store.set_step_up_state(
        login_id_normalized="+254733410016",
        source_ip="203.0.113.30",
        step_up_state=LoginStepUpState(
            challenge_id=challenge_id,
            challenge_channel="email",
            challenge_expires_at=challenge_record.expires_at.isoformat().replace("+00:00", "Z"),
        ),
    )
    replay_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-reuse-replay",
            "X-Forwarded-For": "203.0.113.30",
        },
        json={
            "login_id": "+254733410016",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    replay_error = _extract_error_detail(replay_response)
    assert replay_response.status_code == 409
    assert replay_error["error_code"] == "login_step_up_challenge_already_used"
    assert replay_error["reason"] == "login_step_up_challenge_already_used"


def test_login_step_up_context_mismatch_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, email_store, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.owner@example.com",
        phone_number="+254733410005",
    )
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.other@example.com",
        phone_number="+254733410006",
    )

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410005",
        password="StrongPassw0rd!",
        correlation_id="login-step-up-mismatch-pending",
        source_ip="203.0.113.24",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    mismatch_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-mismatch-final",
            "X-Forwarded-For": "203.0.113.24",
        },
        json={
            "login_id": "+254733410006",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    mismatch_error = _extract_error_detail(mismatch_response)
    assert mismatch_response.status_code == 409
    assert mismatch_error["error_code"] == "login_step_up_challenge_invalid"
    assert mismatch_error["reason"] == "login_step_up_challenge_invalid"


def test_login_step_up_wrong_purpose_challenge_is_rejected_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    (
        client,
        registration_store,
        _session_store,
        email_store,
        _phone_store,
        _lockout_store,
        login_step_up_store,
        _clock,
    ) = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.stepup.wrong-purpose@example.com",
        phone_number="+254733410010",
    )

    otp_challenge_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "login-step-up-wrong-purpose-otp-idem",
            "X-Correlation-ID": "login-step-up-wrong-purpose-otp-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": "login.stepup.wrong-purpose@example.com",
        },
    )
    otp_challenge_payload = _response_json(otp_challenge_response)
    assert otp_challenge_response.status_code == 201
    challenge_id = UUID(cast(str, otp_challenge_payload["challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    challenge_record = email_store.get_challenge(challenge_id=challenge_id)
    assert challenge_record is not None

    login_step_up_store.set_step_up_state(
        login_id_normalized="+254733410010",
        source_ip="203.0.113.124",
        step_up_state=LoginStepUpState(
            challenge_id=challenge_id,
            challenge_channel="email",
            challenge_expires_at=challenge_record.expires_at.isoformat().replace(
                "+00:00",
                "Z",
            ),
        ),
    )
    response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-step-up-wrong-purpose-final-corr",
            "X-Forwarded-For": "203.0.113.124",
        },
        json={
            "login_id": "+254733410010",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "login_step_up_challenge_invalid"
    assert error["reason"] == "login_step_up_challenge_invalid"


def test_login_lockout_still_applies_under_mfa_gate(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _, _, _, lockout_clock = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.lockout.mfa@example.com",
        phone_number="+254733410007",
    )
    headers = {"X-Correlation-ID": "login-lockout-mfa-corr", "X-Forwarded-For": "203.0.113.25"}
    invalid_payload = {"login_id": "+254733410007", "password": "WrongPassw0rd!"}

    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold_error = _extract_error_detail(threshold)
    assert threshold.status_code == 403
    assert threshold_error["error_code"] == "login_lockout_threshold_exceeded"
    assert threshold_error["reason"] == "login_lockout_threshold_exceeded"
    assert isinstance(threshold_error["lockout_expires_at"], str)
    assert isinstance(threshold_error["lockout_remaining_seconds"], int)
    assert threshold_error["lockout_remaining_seconds"] == _LOCKOUT_DURATION_SECONDS

    active = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_repeat = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_error = _extract_error_detail(active)
    active_repeat_error = _extract_error_detail(active_repeat)
    assert active.status_code == 403
    assert active_error["error_code"] == "login_lockout_active"
    assert active_error["reason"] == "login_lockout_active"
    assert isinstance(active_error["lockout_expires_at"], str)
    assert isinstance(active_error["lockout_remaining_seconds"], int)
    assert active_error["lockout_remaining_seconds"] <= _LOCKOUT_DURATION_SECONDS
    assert active_repeat.status_code == 403
    assert canonical_json_dumps(active_error) == canonical_json_dumps(active_repeat_error)

    lockout_clock.advance(seconds=_LOCKOUT_DURATION_SECONDS + 1)
    recovery_pending = client.post(
        "/v1/auth/login",
        headers=headers,
        json={"login_id": "+254733410007", "password": "StrongPassw0rd!"},
    )
    recovery_payload = _response_json(recovery_pending)
    assert recovery_pending.status_code == 200
    assert recovery_payload["login_status"] == "pending_step_up"
    assert recovery_payload["status"] == "pending_step_up"


def test_login_lockout_counter_resets_after_attempt_window_gap(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _, _, _, lockout_clock = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.lockout.window-reset@example.com",
        phone_number="+254733410012",
    )
    headers = {
        "X-Correlation-ID": "login-lockout-window-reset-corr",
        "X-Forwarded-For": "203.0.113.28",
    }
    invalid_payload = {"login_id": "+254733410012", "password": "WrongPassw0rd!"}

    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    lockout_clock.advance(seconds=_FAILED_ATTEMPT_WINDOW_SECONDS + 1)

    third_after_gap = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    third_after_gap_error = _extract_error_detail(third_after_gap)
    assert third_after_gap.status_code == 401
    assert third_after_gap_error["error_code"] == "login_invalid_credentials"
    assert third_after_gap_error["reason"] == "login_invalid_credentials"


def test_login_failed_attempts_from_different_ips_do_not_share_lockout_state(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.lockout.different-ip@example.com",
        phone_number="+254733410013",
    )
    base_headers = {"X-Correlation-ID": "login-lockout-different-ip-corr"}
    invalid_payload = {"login_id": "+254733410013", "password": "WrongPassw0rd!"}

    for suffix in ("31", "31", "31", "32", "32"):
        client.post(
            "/v1/auth/login",
            headers={**base_headers, "X-Forwarded-For": f"203.0.113.{suffix}"},
            json=invalid_payload,
        )

    shared_lockout_check = client.post(
        "/v1/auth/login",
        headers={**base_headers, "X-Forwarded-For": "203.0.113.32"},
        json=invalid_payload,
    )
    error = _extract_error_detail(shared_lockout_check)
    assert shared_lockout_check.status_code == 403
    assert error["error_code"] == "login_lockout_threshold_exceeded"
    assert error["reason"] == "login_lockout_threshold_exceeded"


def test_login_successful_authentication_clears_failed_attempt_counter(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, email_store, _, _, _, _ = client_and_store
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="login.lockout.reset-on-success@example.com",
        phone_number="+254733410014",
    )
    headers = {
        "X-Correlation-ID": "login-lockout-reset-on-success-corr",
        "X-Forwarded-For": "203.0.113.34",
    }
    invalid_payload = {"login_id": "+254733410014", "password": "WrongPassw0rd!"}

    client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    client.post("/v1/auth/login", headers=headers, json=invalid_payload)

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410014",
        password="StrongPassw0rd!",
        correlation_id="login-lockout-reset-on-success-pending",
        source_ip="203.0.113.34",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    final_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-lockout-reset-on-success-final",
            "X-Forwarded-For": "203.0.113.34",
        },
        json={
            "login_id": "+254733410014",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    assert final_response.status_code == 200

    post_success_failure = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    post_success_failure_error = _extract_error_detail(post_success_failure)
    assert post_success_failure.status_code == 401
    assert post_success_failure_error["error_code"] == "login_invalid_credentials"
    assert post_success_failure_error["reason"] == "login_invalid_credentials"


def test_login_migrates_legacy_sha256_password_hash_to_bcrypt_after_successful_authentication(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, email_store, _, _, _, _ = client_and_store
    legacy_password = "StrongPassw0rd!"
    legacy_record = registration_store.register_user(
        email_normalized="login.legacy.hash@example.com",
        phone_number_normalized="+254733410008",
        kra_pin_hash=sha256(b"A123456789Z").hexdigest(),
        password_hash=sha256(legacy_password.encode("utf-8")).hexdigest(),
        role="IndividualTaxpayer",
        created_at="2026-03-30T12:00:00Z",
    )
    registration_store.mark_user_email_verified(
        user_id=legacy_record.user_id,
        verified_at="2026-03-30T12:01:00Z",
    )
    before_login_record = registration_store.get_user_by_id(user_id=legacy_record.user_id)
    assert before_login_record is not None
    assert len(before_login_record.password_hash) == 64

    pending_payload = _start_step_up_login(
        client=client,
        login_id="+254733410008",
        password=legacy_password,
        correlation_id="login-legacy-hash-pending",
        source_ip="203.0.113.26",
    )
    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    final_response = client.post(
        "/v1/auth/login",
        headers={"X-Correlation-ID": "login-legacy-hash-final", "X-Forwarded-For": "203.0.113.26"},
        json={
            "login_id": "+254733410008",
            "password": legacy_password,
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )

    final_payload = _response_json(final_response)
    assert final_response.status_code == 200
    assert final_payload["status"] == "authenticated"
    after_login_record = registration_store.get_user_by_id(user_id=legacy_record.user_id)
    assert after_login_record is not None
    assert after_login_record.password_hash.startswith("$2")


def test_login_with_unsupported_password_hash_format_fails_deterministically(
    client_and_store: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemorySessionIssuanceStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryLoginLockoutStore,
        InMemoryLoginStepUpStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, _, _, _, _ = client_and_store
    malformed_record = registration_store.register_user(
        email_normalized="login.malformed.hash@example.com",
        phone_number_normalized="+254733410011",
        kra_pin_hash=sha256(b"A123456789Z").hexdigest(),
        password_hash="not-a-supported-hash",
        role="IndividualTaxpayer",
        created_at="2026-03-30T12:00:00Z",
    )
    registration_store.mark_user_email_verified(
        user_id=malformed_record.user_id,
        verified_at="2026-03-30T12:01:00Z",
    )

    response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "login-malformed-hash-corr",
            "X-Forwarded-For": "203.0.113.27",
        },
        json={"login_id": "+254733410011", "password": "StrongPassw0rd!"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["error_code"] == "password_hash_verification_failed"
    assert error["reason"] == "password_hash_verification_failed"


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "login-registration-corr"},
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
        verified_at="2026-03-30T12:00:00Z",
    )
    return user_id


def _start_step_up_login(
    *,
    client: TestClient,
    login_id: str,
    password: str,
    correlation_id: str,
    source_ip: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/login",
        headers={"X-Correlation-ID": correlation_id, "X-Forwarded-For": source_ip},
        json={"login_id": login_id, "password": password},
    )
    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["login_status"] == "pending_step_up"
    assert payload["status"] == "pending_step_up"
    return payload


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    assert "password" not in detail
    assert "step_up_otp_code" not in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
