"""Adversarial regression tests for deterministic auth abuse and replay handling."""

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
from services.auth.app.password_reset import InMemoryPasswordResetStore
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore

_FAILED_ATTEMPT_WINDOW_SECONDS = 60
_LOCKOUT_DURATION_SECONDS = 120
_MAX_FAILED_ATTEMPTS = 3


class _FrozenClock:
    """Provide deterministic lockout clock control for adversarial login tests."""

    def __init__(self) -> None:
        self._current = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


@pytest.fixture()
def adversarial_client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryPhoneVerificationStore,
            InMemoryPasswordResetStore,
            _FrozenClock,
        ]
    ]
):
    """Create deterministic auth app test context for adversarial regressions."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    phone_store = InMemoryPhoneVerificationStore()
    email_store = InMemoryEmailVerificationStore()
    password_reset_store = InMemoryPasswordResetStore()
    lockout_clock = _FrozenClock()
    lockout_store = InMemoryLoginLockoutStore(
        max_failed_attempts=_MAX_FAILED_ATTEMPTS,
        failed_attempt_window_seconds=_FAILED_ATTEMPT_WINDOW_SECONDS,
        lockout_window_seconds=_LOCKOUT_DURATION_SECONDS,
        now_provider=lockout_clock.now,
    )
    app.state.registration_store = registration_store
    app.state.phone_verification_store = phone_store
    app.state.email_verification_store = email_store
    app.state.password_reset_store = password_reset_store
    app.state.login_lockout_store = lockout_store
    app.state.login_step_up_store = InMemoryLoginStepUpStore()
    app.state.session_issuance_store = InMemorySessionIssuanceStore()
    with TestClient(app) as client:
        yield client, registration_store, phone_store, password_reset_store, lockout_clock
    reset_default_registration_store()


def test_adversarial_login_lockout_threshold_and_active_are_deterministic(
    adversarial_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, _, lockout_clock = adversarial_client_and_stores
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="adversarial.lockout@example.com",
        phone_number="+254733920001",
    )

    headers = {
        "X-Correlation-ID": "adversarial-lockout-corr",
        "X-Forwarded-For": "203.0.113.211",
    }
    invalid_payload = {"login_id": "+254733920001", "password": "WrongPassw0rd!"}

    first = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    second = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    threshold_error = _extract_error_detail(threshold)

    assert first.status_code == 401
    assert second.status_code == 401
    assert first_error["error_code"] == "login_invalid_credentials"
    assert first_error["reason"] == "login_invalid_credentials"
    assert second_error["error_code"] == "login_invalid_credentials"
    assert second_error["reason"] == "login_invalid_credentials"

    assert threshold.status_code == 403
    assert threshold_error["error_code"] == "login_lockout_threshold_exceeded"
    assert threshold_error["reason"] == "login_lockout_threshold_exceeded"
    assert isinstance(threshold_error["lockout_expires_at"], str)
    assert isinstance(threshold_error["lockout_remaining_seconds"], int)

    active = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_repeat = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_error = _extract_error_detail(active)
    active_repeat_error = _extract_error_detail(active_repeat)
    assert active.status_code == 403
    assert active_repeat.status_code == 403
    assert active_error["error_code"] == "login_lockout_active"
    assert active_error["reason"] == "login_lockout_active"
    assert active_repeat_error["error_code"] == "login_lockout_active"
    assert active_repeat_error["reason"] == "login_lockout_active"
    assert set(active_error.keys()) == set(active_repeat_error.keys())
    assert isinstance(active_error["lockout_remaining_seconds"], int)
    assert isinstance(active_repeat_error["lockout_remaining_seconds"], int)

    lockout_clock.advance(seconds=_LOCKOUT_DURATION_SECONDS + 1)
    after_expiry = client.post(
        "/v1/auth/login",
        headers=headers,
        json={"login_id": "+254733920001", "password": "StrongPassw0rd!"},
    )
    after_expiry_payload = _response_json(after_expiry)
    assert after_expiry.status_code == 200
    assert after_expiry_payload["status"] == "pending_step_up"


def test_adversarial_phone_otp_invalid_replay_and_expired_paths_are_canonical(
    adversarial_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        _FrozenClock,
    ],
) -> None:
    client, _, phone_store, _, _ = adversarial_client_and_stores
    phone_number_invalid = "+254733920002"
    phone_number_replay = "+254733920012"
    phone_number_expired = "+254733920022"
    _register_pending_user(
        client=client,
        email="adversarial.otp.invalid@example.com",
        phone_number=phone_number_invalid,
    )
    _register_pending_user(
        client=client,
        email="adversarial.otp.replay@example.com",
        phone_number=phone_number_replay,
    )
    _register_pending_user(
        client=client,
        email="adversarial.otp.expired@example.com",
        phone_number=phone_number_expired,
    )

    invalid_challenge = _issue_phone_challenge(
        client=client,
        phone_number=phone_number_invalid,
        idempotency_key="adversarial-otp-invalid-idem",
    )
    invalid_challenge_id = UUID(cast(str, invalid_challenge["challenge_id"]))
    valid_otp = phone_store.get_otp_code_for_challenge(challenge_id=invalid_challenge_id)
    invalid_otp = "000000" if valid_otp != "000000" else "999999"
    first_invalid = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "adversarial-otp-invalid-corr"},
        json={"challenge_id": str(invalid_challenge_id), "otp_code": invalid_otp},
    )
    second_invalid = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "adversarial-otp-invalid-corr"},
        json={"challenge_id": str(invalid_challenge_id), "otp_code": invalid_otp},
    )
    first_invalid_error = _extract_error_detail(first_invalid)
    second_invalid_error = _extract_error_detail(second_invalid)
    assert first_invalid.status_code == 409
    assert second_invalid.status_code == 409
    assert first_invalid_error["error_code"] == "otp_invalid"
    assert first_invalid_error["reason"] == "otp_invalid"
    assert second_invalid_error["error_code"] == "otp_invalid"
    assert second_invalid_error["reason"] == "otp_invalid"
    assert set(first_invalid_error.keys()) == set(second_invalid_error.keys())

    replay_challenge = _issue_phone_challenge(
        client=client,
        phone_number=phone_number_replay,
        idempotency_key="adversarial-otp-replay-idem",
    )
    replay_challenge_id = UUID(cast(str, replay_challenge["challenge_id"]))
    replay_otp = phone_store.get_otp_code_for_challenge(challenge_id=replay_challenge_id)
    first_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "adversarial-otp-replay-first-corr"},
        json={"challenge_id": str(replay_challenge_id), "otp_code": replay_otp},
    )
    replay_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "adversarial-otp-replay-second-corr"},
        json={"challenge_id": str(replay_challenge_id), "otp_code": replay_otp},
    )
    replay_error = _extract_error_detail(replay_verify)
    assert first_verify.status_code == 200
    assert replay_verify.status_code == 409
    assert replay_error["error_code"] == "otp_already_used"
    assert replay_error["reason"] == "otp_already_used"

    expired_challenge = _issue_phone_challenge(
        client=client,
        phone_number=phone_number_expired,
        idempotency_key="adversarial-otp-expired-idem",
    )
    expired_challenge_id = UUID(cast(str, expired_challenge["challenge_id"]))
    phone_store.force_expire_challenge(challenge_id=expired_challenge_id)
    expired_otp = phone_store.get_otp_code_for_challenge(challenge_id=expired_challenge_id)
    expired_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "adversarial-otp-expired-corr"},
        json={"challenge_id": str(expired_challenge_id), "otp_code": expired_otp},
    )
    expired_error = _extract_error_detail(expired_verify)
    assert expired_verify.status_code == 409
    assert expired_error["error_code"] == "otp_expired"
    assert expired_error["reason"] == "otp_expired"


def test_adversarial_password_reset_replay_and_expired_tokens_are_canonical(
    adversarial_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        _FrozenClock,
    ],
) -> None:
    client, registration_store, _, password_reset_store, _ = adversarial_client_and_stores
    email = "adversarial.password.reset@example.com"
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email=email,
        phone_number="+254733920003",
    )

    replay_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="adversarial-password-reset-replay-idem",
        correlation_id="adversarial-password-reset-replay-corr",
    )
    replay_challenge_id = UUID(cast(str, replay_challenge["challenge_id"]))
    replay_code = password_reset_store.get_reset_code_for_challenge(
        challenge_id=replay_challenge_id
    )
    first_confirm = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "adversarial-password-reset-confirm-first-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "N3wStrongPassw0rd!",
        },
    )
    replay_confirm = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "adversarial-password-reset-confirm-replay-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "AnotherStrongPassw0rd!",
        },
    )
    replay_repeat = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "adversarial-password-reset-confirm-replay-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "AnotherStrongPassw0rd!",
        },
    )
    replay_error = _extract_error_detail(replay_confirm)
    replay_repeat_error = _extract_error_detail(replay_repeat)
    assert first_confirm.status_code == 200
    assert replay_confirm.status_code == 409
    assert replay_repeat.status_code == 409
    assert replay_error["error_code"] == "password_reset_token_already_used"
    assert replay_error["reason"] == "password_reset_token_already_used"
    assert canonical_json_dumps(replay_error) == canonical_json_dumps(replay_repeat_error)

    expired_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="adversarial-password-reset-expired-idem",
        correlation_id="adversarial-password-reset-expired-corr",
    )
    expired_challenge_id = UUID(cast(str, expired_challenge["challenge_id"]))
    password_reset_store.force_expire_challenge(challenge_id=expired_challenge_id)
    expired_code = password_reset_store.get_reset_code_for_challenge(
        challenge_id=expired_challenge_id
    )
    expired_confirm = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "adversarial-password-reset-expired-confirm-corr"},
        json={
            "challenge_id": str(expired_challenge_id),
            "reset_code": expired_code,
            "new_password": "Y3tAnotherStrongPassw0rd!",
        },
    )
    expired_repeat = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "adversarial-password-reset-expired-confirm-corr"},
        json={
            "challenge_id": str(expired_challenge_id),
            "reset_code": expired_code,
            "new_password": "Y3tAnotherStrongPassw0rd!",
        },
    )
    expired_error = _extract_error_detail(expired_confirm)
    expired_repeat_error = _extract_error_detail(expired_repeat)
    assert expired_confirm.status_code == 409
    assert expired_repeat.status_code == 409
    assert expired_error["error_code"] == "password_reset_token_expired"
    assert expired_error["reason"] == "password_reset_token_expired"
    assert canonical_json_dumps(expired_error) == canonical_json_dumps(expired_repeat_error)


def test_adversarial_malformed_login_payload_is_rejected_safely_and_deterministically(
    adversarial_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _ = adversarial_client_and_stores
    headers = {
        "X-Correlation-ID": "adversarial-malformed-login-corr",
        "X-Forwarded-For": "203.0.113.212",
    }
    malformed_payload = {
        "login_id": {"unexpected": "object"},
        "password": ["not-a-string"],
    }

    first = client.post("/v1/auth/login", headers=headers, json=malformed_payload)
    second = client.post("/v1/auth/login", headers=headers, json=malformed_payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 400
    assert second.status_code == 400
    assert first_error["error_code"] == "invalid_login_request"
    assert first_error["reason"] == "invalid_login_request"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_adversarial_malformed_refresh_token_failure_class_is_stable(
    adversarial_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        _FrozenClock,
    ],
) -> None:
    client, _, _, _, _ = adversarial_client_and_stores
    headers = {"X-Correlation-ID": "adversarial-refresh-malformed-corr"}

    first = client.post("/v1/auth/refresh", headers=headers, json={"refresh_token": "not-a-token"})
    second = client.post("/v1/auth/refresh", headers=headers, json={"refresh_token": "not-a-token"})
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 401
    assert second.status_code == 401
    assert first_error["error_code"] == "refresh_token_malformed"
    assert first_error["reason"] == "refresh_token_malformed"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "adversarial-register-corr"},
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
        verified_at="2026-04-01T12:00:00Z",
    )
    return user_id


def _register_pending_user(
    *,
    client: TestClient,
    email: str,
    phone_number: str,
) -> UUID:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "adversarial-register-pending-corr"},
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


def _issue_phone_challenge(
    *,
    client: TestClient,
    phone_number: str,
    idempotency_key: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": f"{idempotency_key}-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _initiate_password_reset(
    *,
    client: TestClient,
    email: str,
    idempotency_key: str,
    correlation_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/password-reset/initiate",
        headers={
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        },
        json={
            "purpose": "password_reset",
            "channel": "email",
            "email": email,
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "password" not in detail
    assert "otp_code" not in detail
    assert "step_up_otp_code" not in detail
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
