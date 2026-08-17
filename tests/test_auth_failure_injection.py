"""Failure-injection regression tests for deterministic auth degraded-path behavior."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
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
from services.auth.app.phone_verification import SmsDeliveryResult
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore
from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome


class _DeterministicSmsDeliveryAdapter:
    """Inject deterministic SMS provider outcomes keyed by normalized phone number."""

    def __init__(
        self,
        *,
        outcomes_by_phone: dict[str, SmsDeliveryResult],
    ) -> None:
        self._outcomes_by_phone = outcomes_by_phone

    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
    ) -> SmsDeliveryResult:
        del purpose
        return self._outcomes_by_phone.get(
            phone_number_normalized,
            SmsDeliveryResult(status="delivered", reason_code="sms_delivery_delivered"),
        )


class _DeterministicEmailDeliveryAdapter:
    """Inject deterministic email provider outcomes keyed by normalized destination email."""

    def __init__(
        self,
        *,
        outcomes_by_email: dict[str, OtpDeliveryOutcome],
    ) -> None:
        self._outcomes_by_email = outcomes_by_email

    def send_otp_challenge(
        self,
        *,
        message: EmailOtpMessage,
    ) -> OtpDeliveryOutcome:
        _ = message.purpose
        return self._outcomes_by_email.get(
            message.email_normalized,
            OtpDeliveryOutcome(
                status="delivered",
                reason_code="email_delivery_provider_delivered",
            ),
        )


@pytest.fixture()
def client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryPhoneVerificationStore,
            InMemorySessionIssuanceStore,
        ]
    ]
):
    """Create deterministic auth app test context for failure-injection paths."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    phone_store = InMemoryPhoneVerificationStore()
    email_store = InMemoryEmailVerificationStore()
    session_store = InMemorySessionIssuanceStore()
    app.state.registration_store = registration_store
    app.state.phone_verification_store = phone_store
    app.state.email_verification_store = email_store
    app.state.session_issuance_store = session_store
    app.state.login_lockout_store = InMemoryLoginLockoutStore()
    app.state.login_step_up_store = InMemoryLoginStepUpStore()
    with TestClient(app) as client:
        yield client, registration_store, phone_store, session_store
    reset_default_registration_store()


def test_failure_injection_sampled_happy_path_still_authenticates(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, registration_store, phone_store, _ = client_and_stores
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="failure.injection.happy.path@example.com",
        phone_number="+254733910001",
    )

    final_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733910001",
        source_ip="203.0.113.201",
        correlation_prefix="failure-injection-happy",
    )

    assert final_payload["status"] == "authenticated"
    assert isinstance(final_payload["access_token"], str)
    assert isinstance(final_payload["refresh_token"], str)


def test_failure_injection_sms_timeout_is_canonical_and_deterministic(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    phone_number = "+254733910002"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_retryable",
                reason_code="sms_delivery_provider_timeout",
            )
        }
    )
    payload = {
        "purpose": "registration_verify",
        "channel": "sms",
        "phone_number": phone_number,
    }
    headers = {
        "Idempotency-Key": "failure-injection-sms-timeout-idem",
        "X-Correlation-ID": "failure-injection-sms-timeout-corr",
    }

    first = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    second = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 409
    assert second.status_code == 409
    assert first_error["error_code"] == "otp_primary_delivery_failed_retryable"
    assert first_error["reason"] == "otp_primary_delivery_failed_retryable"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)
    details = cast(dict[str, object], first_error["details"])
    assert details["delivery_failure_class"] == "failed_retryable"
    assert details["primary_channel"] == "sms"
    assert isinstance(details["retry_after_seconds"], int)


def test_failure_injection_sms_fallback_unavailable_is_canonical(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    phone_number = "+254733910003"
    fallback_email = "failure.injection.fallback@example.com"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_non_retryable",
                reason_code="sms_delivery_provider_unavailable",
            )
        }
    )
    client.app.state.email_delivery_adapter = _DeterministicEmailDeliveryAdapter(
        outcomes_by_email={
            fallback_email: OtpDeliveryOutcome(
                status="failed_non_retryable",
                reason_code="email_delivery_provider_unavailable",
            )
        }
    )
    payload = {
        "purpose": "registration_verify",
        "channel": "sms",
        "phone_number": phone_number,
        "fallback_channel": "email",
        "email": fallback_email,
    }
    headers = {
        "Idempotency-Key": "failure-injection-fallback-unavailable-idem",
        "X-Correlation-ID": "failure-injection-fallback-unavailable-corr",
    }

    first = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    second = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 409
    assert second.status_code == 409
    assert first_error["error_code"] == "otp_fallback_channel_unavailable"
    assert first_error["reason"] == "otp_fallback_channel_unavailable"
    assert set(first_error.keys()) == set(second_error.keys())
    details = cast(dict[str, object], first_error["details"])
    assert details["primary_channel"] == "sms"
    assert details["fallback_channel_requested"] == "email"
    assert details["fallback_channel_attempted"] == "email"


def test_failure_injection_revoked_session_refresh_is_blocked_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, registration_store, phone_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="failure.injection.revoked.refresh@example.com",
        phone_number="+254733910004",
    )
    login_payload = _complete_login(
        client=client,
        phone_store=phone_store,
        login_id="+254733910004",
        source_ip="203.0.113.202",
        correlation_prefix="failure-injection-revocation",
    )
    refresh_token = cast(str, login_payload["refresh_token"])

    logout_response = client.post(
        "/v1/auth/logout",
        headers={
            "Authorization": f"Bearer user_id={user_id}",
            "X-Correlation-ID": "failure-injection-logout-corr",
        },
        json={"revoke_scope": "all_sessions"},
    )
    assert logout_response.status_code == 200

    first_refresh = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "failure-injection-revoked-refresh-corr"},
        json={"refresh_token": refresh_token},
    )
    second_refresh = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "failure-injection-revoked-refresh-corr"},
        json={"refresh_token": refresh_token},
    )
    first_error = _extract_error_detail(first_refresh)
    second_error = _extract_error_detail(second_refresh)

    assert first_refresh.status_code == 401
    assert second_refresh.status_code == 401
    assert first_error["error_code"] == "refresh_token_session_revoked"
    assert first_error["reason"] == "refresh_token_session_revoked"
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
        headers={"X-Correlation-ID": "failure-injection-register-corr"},
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
        verified_at="2026-04-01T10:00:00Z",
    )
    return user_id


def _complete_login(
    *,
    client: TestClient,
    phone_store: InMemoryPhoneVerificationStore,
    login_id: str,
    source_ip: str,
    correlation_prefix: str,
) -> dict[str, Any]:
    pending_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": f"{correlation_prefix}-pending",
            "X-Forwarded-For": source_ip,
        },
        json={"login_id": login_id, "password": "StrongPassw0rd!"},
    )
    pending_payload = _response_json(pending_response)
    assert pending_response.status_code == 200
    assert pending_payload["status"] == "pending_step_up"

    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    final_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": f"{correlation_prefix}-final",
            "X-Forwarded-For": source_ip,
        },
        json={
            "login_id": login_id,
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    final_payload = _response_json(final_response)
    assert final_response.status_code == 200
    return final_payload


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
