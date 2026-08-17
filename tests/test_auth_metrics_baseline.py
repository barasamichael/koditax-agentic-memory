"""Verify auth metrics baseline coverage and deterministic emission behavior."""

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
from services.auth.app.metrics import MetricEvent
from services.auth.app.metrics import MetricsPolicyError
from services.auth.app.metrics import AUTH_LOGIN_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_LOGIN_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_OAUTH_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_LOCKOUT_APPLIED_TOTAL
from services.auth.app.metrics import AUTH_OTP_VERIFY_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_OTP_VERIFY_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_OTP_CHALLENGE_ISSUED_TOTAL
from services.auth.app.metrics import get_default_auth_metrics_emitter
from services.auth.app.metrics import AUTH_SESSION_REFRESH_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_SESSION_REFRESH_SUCCESS_TOTAL
from services.auth.app.metrics import reset_default_auth_metrics_emitter
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore


@pytest.fixture()
def client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryEmailVerificationStore,
            InMemoryPhoneVerificationStore,
            InMemorySessionIssuanceStore,
        ]
    ]
):
    """Create isolated auth client with deterministic store and metric state."""

    reset_default_registration_store()
    reset_default_auth_metrics_emitter()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_store = InMemoryEmailVerificationStore()
    phone_store = InMemoryPhoneVerificationStore()
    session_store = InMemorySessionIssuanceStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_store
    app.state.phone_verification_store = phone_store
    app.state.session_issuance_store = session_store
    app.state.login_lockout_store = InMemoryLoginLockoutStore(
        max_failed_attempts=3,
        failed_attempt_window_seconds=600,
        lockout_window_seconds=300,
    )
    app.state.login_step_up_store = InMemoryLoginStepUpStore()
    app.state.auth_metrics_emitter = get_default_auth_metrics_emitter()
    with TestClient(app) as client:
        yield client, registration_store, email_store, phone_store, session_store
    reset_default_registration_store()
    reset_default_auth_metrics_emitter()


def test_success_paths_emit_login_otp_and_refresh_metrics(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, phone_store, _ = client_and_stores
    _register_user(
        client=client,
        email="metrics.success@example.com",
        phone_number="+254733700101",
    )
    challenge_id = _issue_phone_registration_challenge(
        client=client,
        phone_number="+254733700101",
        idempotency_key="metrics-success-registration-idem",
        correlation_id="metrics-success-registration-corr",
    )
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    _verify_otp(
        client=client,
        challenge_id=challenge_id,
        otp_code=otp_code,
        correlation_id="metrics-success-registration-verify-corr",
    )

    first_login = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "metrics-success-login-step-up-corr",
            "X-Forwarded-For": "203.0.113.11",
        },
        json={"login_id": "+254733700101", "password": "StrongPassw0rd!"},
    )
    assert first_login.status_code == 200
    first_payload = _response_json(first_login)
    assert first_payload["status"] == "pending_step_up"
    step_up_challenge_id = UUID(cast(str, first_payload["step_up_challenge_id"]))
    step_up_otp = phone_store.get_otp_code_for_challenge(challenge_id=step_up_challenge_id)

    second_login = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "metrics-success-login-complete-corr",
            "X-Forwarded-For": "203.0.113.11",
        },
        json={
            "login_id": "+254733700101",
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(step_up_challenge_id),
            "step_up_otp_code": step_up_otp,
        },
    )
    assert second_login.status_code == 200
    login_payload = _response_json(second_login)
    assert login_payload["status"] == "authenticated"

    refresh_response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "metrics-success-refresh-corr"},
        json={"refresh_token": login_payload["refresh_token"]},
    )
    assert refresh_response.status_code == 200

    events = get_default_auth_metrics_emitter().snapshot()
    assert _contains_event(events, metric_id=AUTH_LOGIN_SUCCESS_TOTAL, dimensions={})
    assert _contains_event(
        events,
        metric_id=AUTH_OTP_VERIFY_SUCCESS_TOTAL,
        dimensions={"channel": "sms", "purpose": "registration_verify"},
    )
    assert _contains_event(events, metric_id=AUTH_SESSION_REFRESH_SUCCESS_TOTAL, dimensions={})
    assert _contains_event(
        events,
        metric_id=AUTH_OTP_CHALLENGE_ISSUED_TOTAL,
        dimensions={"channel": "sms", "purpose": "login_step_up", "provider": "stub"},
    )


def test_failure_paths_emit_reason_coded_metrics_without_sensitive_dimensions(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _, _ = client_and_stores
    _register_user(
        client=client,
        email="metrics.failure@example.com",
        phone_number="+254733700102",
    )
    challenge_id = _issue_phone_registration_challenge(
        client=client,
        phone_number="+254733700102",
        idempotency_key="metrics-failure-registration-idem",
        correlation_id="metrics-failure-registration-corr",
    )

    invalid_verify = _verify_otp(
        client=client,
        challenge_id=challenge_id,
        otp_code="000000",
        correlation_id="metrics-failure-otp-invalid-corr",
        expected_status=409,
    )
    assert invalid_verify["reason"] == "otp_invalid"

    invalid_login = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "metrics-failure-login-corr",
            "X-Forwarded-For": "198.51.100.41",
        },
        json={"login_id": "+254733700102", "password": "WrongPassword!"},
    )
    assert invalid_login.status_code == 401
    assert _extract_error_detail(invalid_login)["reason"] == "login_invalid_credentials"

    invalid_refresh = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "metrics-failure-refresh-corr"},
        json={"refresh_token": "  "},
    )
    assert invalid_refresh.status_code == 401
    assert _extract_error_detail(invalid_refresh)["reason"] == "refresh_token_malformed"

    events = get_default_auth_metrics_emitter().snapshot()
    assert _contains_event(
        events,
        metric_id=AUTH_OTP_VERIFY_FAILURE_TOTAL,
        dimensions={
            "reason_code": "otp_invalid",
            "channel": "sms",
            "purpose": "registration_verify",
        },
    )
    assert _contains_event(
        events,
        metric_id=AUTH_LOGIN_FAILURE_TOTAL,
        dimensions={"reason_code": "login_invalid_credentials"},
    )
    assert _contains_event(
        events,
        metric_id=AUTH_SESSION_REFRESH_FAILURE_TOTAL,
        dimensions={"reason_code": "refresh_token_malformed"},
    )

    for event in events:
        for key, value in event.dimensions.items():
            normalized_key = key.lower()
            normalized_value = value.lower()
            assert "password" not in normalized_key
            assert "token" not in normalized_key
            assert "otp_code" not in normalized_key
            assert "password" not in normalized_value
            assert "bearer " not in normalized_value


def test_lockout_threshold_and_active_lockout_emit_lockout_metric(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, phone_store, _ = client_and_stores
    _register_user(
        client=client,
        email="metrics.lockout@example.com",
        phone_number="+254733700103",
    )
    challenge_id = _issue_phone_registration_challenge(
        client=client,
        phone_number="+254733700103",
        idempotency_key="metrics-lockout-registration-idem",
        correlation_id="metrics-lockout-registration-corr",
    )
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    _verify_otp(
        client=client,
        challenge_id=challenge_id,
        otp_code=otp_code,
        correlation_id="metrics-lockout-registration-verify-corr",
    )
    reset_default_auth_metrics_emitter()

    for _ in range(2):
        response = client.post(
            "/v1/auth/login",
            headers={
                "X-Correlation-ID": "metrics-lockout-invalid-corr",
                "X-Forwarded-For": "198.51.100.55",
            },
            json={"login_id": "+254733700103", "password": "WrongPassword!"},
        )
        assert response.status_code == 401

    threshold_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "metrics-lockout-threshold-corr",
            "X-Forwarded-For": "198.51.100.55",
        },
        json={"login_id": "+254733700103", "password": "WrongPassword!"},
    )
    assert threshold_response.status_code == 403
    assert _extract_error_detail(threshold_response)["reason"] == "login_lockout_threshold_exceeded"

    active_response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "metrics-lockout-active-corr",
            "X-Forwarded-For": "198.51.100.55",
        },
        json={"login_id": "+254733700103", "password": "WrongPassword!"},
    )
    assert active_response.status_code == 403
    assert _extract_error_detail(active_response)["reason"] == "login_lockout_active"

    events = get_default_auth_metrics_emitter().snapshot()
    assert _contains_event(
        events,
        metric_id=AUTH_LOCKOUT_APPLIED_TOTAL,
        dimensions={"reason_code": "login_lockout_threshold_exceeded"},
    )
    assert _contains_event(
        events,
        metric_id=AUTH_LOCKOUT_APPLIED_TOTAL,
        dimensions={"reason_code": "login_lockout_active"},
    )


def test_same_failure_class_emits_stable_metric_shape(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemorySessionIssuanceStore,
    ],
) -> None:
    client, _, _, _, _ = client_and_stores

    first = _run_malformed_refresh_failure_capture(client=client)
    second = _run_malformed_refresh_failure_capture(client=client)
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_metrics_policy_blocks_sensitive_dimensions_and_supports_oauth_failure_baseline() -> None:
    reset_default_auth_metrics_emitter()
    emitter = get_default_auth_metrics_emitter()

    with pytest.raises(MetricsPolicyError) as error_info:
        emitter.increment_counter(
            AUTH_LOGIN_FAILURE_TOTAL,
            dimensions={"reason_code": "login_invalid_credentials", "password": "secret"},
        )
    assert error_info.value.reason == "sensitive_dimension_key"
    assert emitter.snapshot() == ()

    emitter.increment_counter(
        AUTH_OAUTH_FAILURE_TOTAL,
        dimensions={"reason_code": "oauth_provider_unavailable", "provider": "allowlisted_primary"},
    )
    events = emitter.snapshot()
    assert len(events) == 1
    assert events[0].metric_id == AUTH_OAUTH_FAILURE_TOTAL
    assert events[0].dimensions == {
        "provider": "allowlisted_primary",
        "reason_code": "oauth_provider_unavailable",
    }


def test_non_blocking_metric_emission_warns_on_policy_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_default_auth_metrics_emitter()
    emitter = get_default_auth_metrics_emitter()
    warnings: list[str] = []

    def _capture_warning(message: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        warnings.append(str(message))

    monkeypatch.setattr("services.auth.app.metrics.LOGGER.warning", _capture_warning)

    emitter.increment_counter_non_blocking(
        AUTH_LOGIN_FAILURE_TOTAL,
        dimensions={"reason_code": "login_invalid_credentials", "password": "secret"},
    )

    assert emitter.snapshot() == ()
    assert warnings
    assert "unknown_metric_id" not in warnings[0]
    assert "sensitive_dimension_key" in warnings[0]
    assert "auth.login.failure_total" in warnings[0]


def _run_malformed_refresh_failure_capture(*, client: TestClient) -> list[dict[str, object]]:
    reset_default_auth_metrics_emitter()
    response = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "metrics-determinism-refresh-corr"},
        json={"refresh_token": ""},
    )
    assert response.status_code == 401
    captured = [
        _event_to_dict(event)
        for event in get_default_auth_metrics_emitter().snapshot()
        if event.metric_id == AUTH_SESSION_REFRESH_FAILURE_TOTAL
    ]
    return captured


def _register_user(*, client: TestClient, email: str, phone_number: str) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "metrics-registration-corr"},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    assert response.status_code == 201


def _issue_phone_registration_challenge(
    *,
    client: TestClient,
    phone_number: str,
    idempotency_key: str,
    correlation_id: str,
) -> UUID:
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={"Idempotency-Key": idempotency_key, "X-Correlation-ID": correlation_id},
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    assert response.status_code == 201
    payload = _response_json(response)
    return UUID(cast(str, payload["challenge_id"]))


def _verify_otp(
    *,
    client: TestClient,
    challenge_id: UUID,
    otp_code: str,
    correlation_id: str,
    expected_status: int = 200,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": correlation_id},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    assert response.status_code == expected_status
    return _response_json(response) if expected_status == 200 else _extract_error_detail(response)


def _contains_event(
    events: tuple[MetricEvent, ...],
    *,
    metric_id: str,
    dimensions: dict[str, str],
) -> bool:
    for event in events:
        if event.metric_id != metric_id:
            continue
        if all(event.dimensions.get(key) == value for key, value in dimensions.items()):
            return True
    return False


def _event_to_dict(event: MetricEvent) -> dict[str, object]:
    return {
        "metric_id": event.metric_id,
        "metric_type": event.metric_type,
        "value": event.value,
        "dimensions": event.dimensions,
    }


def _response_json(response: Any) -> dict[str, Any]:
    return cast(dict[str, Any], response.json())


def _extract_error_detail(response: Any) -> dict[str, Any]:
    payload = cast(dict[str, Any], response.json())
    detail = payload.get("detail", {})
    assert isinstance(detail, dict)
    return cast(dict[str, Any], detail)
