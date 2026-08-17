"""Verify structured auth logging and deterministic redaction policy behavior."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.main import list_auth_structured_logs
from services.auth.app.main import reset_auth_structured_logs
from services.auth.app.login import InMemoryLoginStepUpStore
from services.auth.app.login import InMemoryLoginLockoutStore
from services.auth.app.logging import emit_auth_structured_log
from services.auth.app.logging import get_default_auth_structured_log_store
from services.auth.app.logging import reset_default_auth_structured_log_store
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
        ]
    ]
):
    """Create isolated auth client with deterministic in-memory stores."""

    reset_default_registration_store()
    reset_default_auth_structured_log_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_store = InMemoryEmailVerificationStore()
    phone_store = InMemoryPhoneVerificationStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_store
    app.state.phone_verification_store = phone_store
    app.state.session_issuance_store = InMemorySessionIssuanceStore()
    app.state.login_lockout_store = InMemoryLoginLockoutStore(
        max_failed_attempts=3,
        failed_attempt_window_seconds=600,
        lockout_window_seconds=300,
    )
    app.state.login_step_up_store = InMemoryLoginStepUpStore()

    with TestClient(app) as client:
        yield client, registration_store, email_store, phone_store
    reset_default_registration_store()
    reset_default_auth_structured_log_store()


def test_representative_flows_emit_required_structured_log_fields(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
    ],
) -> None:
    client, _, _, phone_store = client_and_stores
    _register_user(
        client=client,
        email="logging.required.fields@example.com",
        phone_number="+254733810101",
        correlation_id="auth-log-register-corr-001",
    )
    challenge_id = _issue_phone_registration_challenge(
        client=client,
        phone_number="+254733810101",
        idempotency_key="auth-log-otp-idem-001",
        correlation_id="auth-log-otp-corr-001",
    )
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    _verify_otp(
        client=client,
        challenge_id=challenge_id,
        otp_code=otp_code,
        correlation_id="auth-log-otp-verify-corr-001",
        expected_status=200,
    )
    invalid_login = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "auth-log-login-fail-corr-001",
            "X-Forwarded-For": "203.0.113.70",
        },
        json={"login_id": "+254733810101", "password": "WrongPassw0rd!"},
    )
    assert invalid_login.status_code == 401

    events = list_auth_structured_logs(app_instance=cast(FastAPI, client.app))
    assert events

    registration_event = _find_event(
        events=events,
        event_type="auth.registration",
        event_status="succeeded",
    )
    assert registration_event is not None
    assert registration_event["reason_code"] is None
    assert registration_event["correlation_id"] == "auth-log-register-corr-001"
    assert registration_event["user_id"] is not None

    login_failure_event = _find_event(
        events=events,
        event_type="auth.login",
        event_status="failed",
        reason_code="login_invalid_credentials",
    )
    assert login_failure_event is not None
    assert login_failure_event["correlation_id"] == "auth-log-login-fail-corr-001"
    assert login_failure_event["trace_id"]

    required_keys = {
        "event_type",
        "event_status",
        "reason_code",
        "trace_id",
        "correlation_id",
        "user_id",
        "tenant_id",
        "details",
    }
    assert set(registration_event.keys()) == required_keys
    assert set(login_failure_event.keys()) == required_keys


def test_structured_logging_redacts_sensitive_details_deterministically() -> None:
    reset_default_auth_structured_log_store()
    emit_auth_structured_log(
        event_type="auth.redaction_test",
        event_status="failed",
        reason_code="redaction_validation",
        trace_id="a" * 64,
        correlation_id="auth-log-redaction-corr-001",
        user_id=None,
        tenant_id="default_tenant",
        details={
            "password": "PlainTextPassword!",
            "otp_code": "123456",
            "authorization": "Bearer very-secret-token",
            "nested": {"refresh_token": "token-001", "proof_blob": "proof-value"},
            "callback_url": "https://example.com/path?token=abc123&safe=1",
        },
    )

    event = get_default_auth_structured_log_store().snapshot()[-1]
    details = cast(dict[str, object], event["details"])
    nested = cast(dict[str, object], details["nested"])
    assert details["password"] == "***redacted***"
    assert details["otp_code"] == "***redacted***"
    assert details["authorization"] == "***redacted***"
    assert nested["refresh_token"] == "***redacted***"
    assert nested["proof_blob"] == "***redacted***"
    assert (
        details["callback_url"]
        == "https://example.com/path?token=%2A%2A%2Aredacted%2A%2A%2A&safe=1"
    )


def test_structured_logging_redacts_provider_destination_fields() -> None:
    reset_default_auth_structured_log_store()
    emit_auth_structured_log(
        event_type="auth.provider_delivery",
        event_status="failed",
        reason_code="otp_sms_delivery_provider_rejected",
        trace_id="b" * 64,
        correlation_id="auth-log-provider-redaction-corr-001",
        user_id=None,
        tenant_id="default_tenant",
        details={
            "recipient": "person@example.com",
            "destination_phone": "+254700123456",
            "destination_email": "person@example.com",
            "provider_ref": "sms:provider:message-1",
        },
    )
    event = get_default_auth_structured_log_store().snapshot()[-1]
    details = cast(dict[str, object], event["details"])
    assert details["recipient"] == "***redacted***"
    assert details["destination_phone"] == "***redacted***"
    assert details["destination_email"] == "***redacted***"
    assert details["provider_ref"] == "sms:provider:message-1"


def test_structured_logging_warns_when_log_store_append_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingStructuredLogStore:
        def append(self, event: dict[str, object]) -> None:
            del event
            raise RuntimeError("structured log store unavailable")

    with caplog.at_level(logging.WARNING, logger="auth.structured"):
        emit_auth_structured_log(
            event_type="auth.redaction_test",
            event_status="failed",
            reason_code="store_append_failure",
            trace_id="c" * 64,
            correlation_id="auth-log-store-warning-corr-001",
            user_id=None,
            tenant_id="default_tenant",
            details={"safe_key": "safe_value"},
            structured_log_store=_FailingStructuredLogStore(),
        )

    assert "store_append_failed" in caplog.text
    assert "auth-log-store-warning-corr-001" in caplog.text


def test_same_invalid_event_class_emits_stable_log_schema(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    first = _capture_invalid_login_log_shape(client=client)
    second = _capture_invalid_login_log_shape(client=client)
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_failure_logs_preserve_reason_code_without_sensitive_leakage(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
    ],
) -> None:
    client, _, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "auth-log-invalid-format-corr-001",
            "X-Forwarded-For": "203.0.113.71",
        },
        json={"login_id": "bad-format", "password": "StrongPassw0rd!"},
    )
    assert response.status_code == 400

    events = list_auth_structured_logs(app_instance=cast(FastAPI, client.app))
    failed_event = _find_event(
        events=events,
        event_type="auth.login",
        event_status="failed",
        reason_code="login_identifier_invalid_format",
    )
    assert failed_event is not None
    _assert_no_sensitive_markers(cast(dict[str, object], failed_event["details"]))


def _capture_invalid_login_log_shape(*, client: TestClient) -> list[dict[str, object]]:
    reset_auth_structured_logs(app_instance=cast(FastAPI, client.app))
    response = client.post(
        "/v1/auth/login",
        headers={"X-Correlation-ID": "auth-log-schema-corr-001", "X-Forwarded-For": "203.0.113.72"},
        json={"login_id": "invalid", "password": "StrongPassw0rd!"},
    )
    assert response.status_code == 400
    events = list_auth_structured_logs(app_instance=cast(FastAPI, client.app))
    return [dict(event) for event in events]


def _find_event(
    *,
    events: tuple[dict[str, object], ...] | tuple[Any, ...],
    event_type: str,
    event_status: str,
    reason_code: str | None = None,
) -> dict[str, object] | None:
    for event in events:
        normalized = cast(dict[str, object], event)
        if normalized.get("event_type") != event_type:
            continue
        if normalized.get("event_status") != event_status:
            continue
        if reason_code is not None and normalized.get("reason_code") != reason_code:
            continue
        return normalized
    return None


def _register_user(
    *,
    client: TestClient,
    email: str,
    phone_number: str,
    correlation_id: str,
) -> None:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": correlation_id},
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
    payload = cast(dict[str, Any], response.json())
    return UUID(cast(str, payload["challenge_id"]))


def _verify_otp(
    *,
    client: TestClient,
    challenge_id: UUID,
    otp_code: str,
    correlation_id: str,
    expected_status: int,
) -> None:
    response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": correlation_id},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    assert response.status_code == expected_status


def _assert_no_sensitive_markers(value: object) -> None:
    banned_tokens = ("password", "otp_code", "access_token", "refresh_token", "secret")
    if isinstance(value, dict):
        for key, nested in value.items():
            assert str(key).lower() not in banned_tokens
            _assert_no_sensitive_markers(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_no_sensitive_markers(nested)
        return
    if isinstance(value, str):
        normalized = value.lower()
        assert "bearer " not in normalized
        assert "token=" not in normalized
