"""Runtime tests for deterministic email-verification registration lifecycle flow."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.config import get_auth_otp_policy_for_purpose
from services.auth.app.config import get_email_verification_max_attempts
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome


@pytest.fixture(autouse=True)
def _auth_otp_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "development")
    monkeypatch.setenv("AUTH_OTP_EMAIL_PROVIDER_MODE", "stub")
    monkeypatch.setenv("AUTH_OTP_SMS_PROVIDER_MODE", "stub")
    monkeypatch.delenv("AUTH_OTP_RESEND_WINDOW_SECONDS", raising=False)


@pytest.fixture()
def client_and_stores(monkeypatch: pytest.MonkeyPatch) -> (
    Iterator[tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore]]
):
    """Create isolated auth app client with injected deterministic in-memory stores."""

    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_verification_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, email_verification_store


class _DeterministicEmailDeliveryAdapter:
    """Provide deterministic email delivery outcomes keyed by normalized email."""

    def __init__(
        self,
        *,
        outcomes_by_email: dict[str, OtpDeliveryOutcome] | None = None,
    ) -> None:
        self._outcomes_by_email = outcomes_by_email or {}

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


def test_email_verification_positive_flow_updates_registration_state(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, registration_store, email_verification_store = client_and_stores
    email = "new.user@example.com"
    _register_user(client=client, email=email)

    issued_challenge = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-verify-positive-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-positive-corr"},
        json={
            "challenge_id": str(challenge_id),
            "otp_code": otp_code,
        },
    )

    verify_payload = _response_json(verify_response)
    assert verify_response.status_code == 200
    assert verify_payload["status"] == "verified"
    assert verify_payload["verification_status"] == "verified"
    registered_user = registration_store.get_user_by_email(email_normalized=email.lower())
    assert registered_user is not None
    assert registered_user.verification_state == "verified"
    assert registered_user.verified_at is not None


def test_dev_otp_endpoint_returns_email_challenge_from_app_state_store(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "dev.otp.email@example.com"
    challenge_payload = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="dev-email-otp-idem",
    )
    challenge_id = UUID(cast(str, challenge_payload["challenge_id"]))

    response = client.get(f"/dev/otp/{challenge_id}")
    payload = _response_json(response)

    assert response.status_code == 200
    assert payload["otp_code"] == email_verification_store.get_otp_code_for_challenge(
        challenge_id=challenge_id
    )
    assert payload["channel"] == "email"
    assert payload["purpose"] == "registration_verify"
    assert isinstance(payload["expires_at"], str)
    assert payload["consumed_at"] is None


@pytest.mark.parametrize(
    ("purpose", "email"),
    [
        ("registration_verify", "matrix.registration@example.com"),
        ("login_step_up", "matrix.login@example.com"),
        ("recovery", "matrix.recovery@example.com"),
        ("account_deletion_confirm", "matrix.deletion@example.com"),
    ],
)
def test_email_verification_supported_purpose_matrix_issues_challenge_successfully(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
    purpose: str,
    email: str,
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": f"email-purpose-{purpose}",
            "X-Correlation-ID": f"email-purpose-{purpose}-corr",
        },
        json={
            "purpose": purpose,
            "channel": "email",
            "email": email,
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["status"] == "challenge_issued"
    assert isinstance(payload["challenge_id"], str)
    assert isinstance(payload["expires_at"], str)
    assert "otp_code" not in payload


def test_email_verification_unsupported_purpose_context_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-purpose-unsupported-idem",
            "X-Correlation-ID": "email-purpose-unsupported-corr",
        },
        json={
            "purpose": "phone_change_confirm",
            "channel": "email",
            "email": "unsupported@example.com",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "invalid_otp_challenge_request"
    assert error["reason"] == "unsupported_otp_challenge_context"


def test_email_verification_missing_email_subject_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-subject-missing-idem",
            "X-Correlation-ID": "email-subject-missing-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "invalid_otp_challenge_request"
    assert error["reason"] == "invalid_otp_challenge_request"


def test_email_verification_same_idempotency_key_and_payload_replays_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    payload = {
        "purpose": "registration_verify",
        "channel": "email",
        "email": "idempotency.replay@example.com",
    }
    headers = {
        "Idempotency-Key": "email-idempotency-replay-idem",
        "X-Correlation-ID": "email-idempotency-replay-corr",
    }
    first = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    second = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second_payload == first_payload


def test_email_verification_delivery_timeout_failure_is_deterministic(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    email = "delivery.timeout@example.com"
    client.app.state.email_delivery_adapter = _DeterministicEmailDeliveryAdapter(
        outcomes_by_email={
            email: OtpDeliveryOutcome(
                status="failed_retryable",
                reason_code="email_delivery_provider_timeout",
                provider_ref="email:stub:timeout",
            )
        }
    )
    headers = {
        "Idempotency-Key": "email-delivery-timeout-idem",
        "X-Correlation-ID": "email-delivery-timeout-corr",
    }
    payload = {
        "purpose": "registration_verify",
        "channel": "email",
        "email": email,
    }
    first = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    second = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 409
    assert first_error["error_code"] == "otp_email_delivery_provider_timeout"
    assert first_error["reason"] == "otp_email_delivery_provider_timeout"
    details = cast(dict[str, object], first_error["details"])
    assert details["delivery_failure_class"] == "failed_retryable"
    assert details["retry_after_seconds"] == 60
    assert details["provider_ref"] == "email:stub:timeout"
    assert second.status_code == 409
    assert second_error == first_error


def test_email_verification_provider_misconfiguration_fails_closed(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = client_and_stores
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_OTP_EMAIL_PROVIDER_MODE", "stub")
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-delivery-misconfigured-idem",
            "X-Correlation-ID": "email-delivery-misconfigured-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": "misconfigured.delivery@example.com",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_delivery_provider_misconfigured"
    assert error["reason"] == "otp_delivery_provider_misconfigured"


def test_email_verification_expired_token_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "expired.user@example.com"
    _register_user(client=client, email=email)

    issued_challenge = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-verify-expired-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    email_verification_store.force_expire_challenge(challenge_id=challenge_id)
    otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-expired-corr"},
        json={
            "challenge_id": str(challenge_id),
            "otp_code": otp_code,
        },
    )

    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_expired"
    assert error["message"] == "OTP challenge has expired."
    assert error["reason"] == "otp_expired"


def test_email_verification_invalid_token_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "invalid.user@example.com"
    _register_user(client=client, email=email)

    issued_challenge = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-verify-invalid-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    valid_otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp_code = "000000" if valid_otp_code != "000000" else "999999"
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-invalid-corr"},
        json={
            "challenge_id": str(challenge_id),
            "otp_code": invalid_otp_code,
        },
    )

    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_invalid"
    assert error["message"] == "OTP code is invalid."
    assert error["reason"] == "otp_invalid"


def test_email_verification_replay_is_rejected_after_success(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "replay.user@example.com"
    _register_user(client=client, email=email)

    issued_challenge = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-verify-replay-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    first_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-replay-first-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    second_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-replay-second-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )

    first_payload = _response_json(first_verify)
    second_error = _extract_error_detail(second_verify)
    assert first_verify.status_code == 200
    assert first_payload["verification_status"] == "verified"
    assert second_verify.status_code == 409
    assert second_error["error_code"] == "otp_already_used"
    assert second_error["reason"] == "otp_already_used"


def test_email_verification_attempt_limit_exceeded_is_deterministic(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "attempt.limit@example.com"
    _register_user(client=client, email=email)
    issued_challenge = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-verify-attempt-limit-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    valid_otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp_code = "000000" if valid_otp_code != "000000" else "999999"

    max_attempts = get_email_verification_max_attempts()
    final_error: dict[str, object] | None = None
    for attempt in range(max_attempts):
        response = client.post(
            "/v1/auth/otp/verify",
            headers={"X-Correlation-ID": f"email-verify-attempt-{attempt}"},
            json={"challenge_id": str(challenge_id), "otp_code": invalid_otp_code},
        )
        final_error = _extract_error_detail(response)
    assert final_error is not None
    assert final_error["error_code"] == "otp_attempt_limit_exceeded"
    assert final_error["reason"] == "otp_attempt_limit_exceeded"

    repeated = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-attempt-repeat"},
        json={"challenge_id": str(challenge_id), "otp_code": invalid_otp_code},
    )
    repeated_error = _extract_error_detail(repeated)
    assert repeated.status_code == 409
    assert repeated_error["error_code"] == "otp_attempt_limit_exceeded"
    assert repeated_error["reason"] == "otp_attempt_limit_exceeded"


def test_email_verification_wrong_purpose_challenge_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "wrong.purpose@example.com"
    _register_user(client=client, email=email)
    challenge_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-verify-wrong-purpose-idem",
            "X-Correlation-ID": "email-verify-wrong-purpose-corr",
        },
        json={
            "purpose": "login_step_up",
            "channel": "email",
            "email": email,
        },
    )
    payload = _response_json(challenge_response)
    assert challenge_response.status_code == 201
    challenge_id = UUID(cast(str, payload["challenge_id"]))
    otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-wrong-purpose-verify-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_challenge_context_mismatch"
    assert error["reason"] == "otp_challenge_context_mismatch"


def test_email_verification_wrong_subject_context_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    unknown_email = "unknown.subject@example.com"
    challenge_payload = _issue_challenge(
        client=client,
        email=unknown_email,
        idempotency_key="email-verify-wrong-subject-idem",
    )
    challenge_id = UUID(cast(str, challenge_payload["challenge_id"]))
    otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-wrong-subject-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_challenge_context_mismatch"
    assert error["reason"] == "otp_challenge_context_mismatch"


def test_email_verification_challenge_issuance_is_existence_neutral(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    existing_email = "known.user@example.com"
    unknown_email = "unknown.user@example.com"
    _register_user(client=client, email=existing_email)

    existing_response = _issue_challenge(
        client=client,
        email=existing_email,
        idempotency_key="email-verify-known-idem",
    )
    unknown_response = _issue_challenge(
        client=client,
        email=unknown_email,
        idempotency_key="email-verify-unknown-idem",
    )

    assert existing_response["status"] == "challenge_issued"
    assert unknown_response["status"] == "challenge_issued"
    assert set(existing_response.keys()) == set(unknown_response.keys())
    assert "account_exists" not in existing_response
    assert "account_exists" not in unknown_response


def test_email_verification_resend_throttle_is_enforced_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    email = "email.resend.throttle@example.com"
    _register_user(client=client, email=email)
    first = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-resend-throttle-first-idem",
    )
    second = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-resend-throttle-second-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": email,
        },
    )
    third = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-resend-throttle-third-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": email,
        },
    )
    error = _extract_error_detail(second)
    repeated_error = _extract_error_detail(third)
    assert first["status"] == "challenge_issued"
    assert second.status_code == 409
    assert third.status_code == 409
    assert error["error_code"] == "otp_resend_throttled"
    assert error["reason"] == "otp_resend_throttled"
    assert repeated_error["error_code"] == "otp_resend_throttled"
    assert repeated_error["reason"] == "otp_resend_throttled"
    assert set(error) == set(repeated_error)
    details = cast(dict[str, object], error["details"])
    assert isinstance(details["retry_after_seconds"], int)
    assert isinstance(details["resend_remaining_count"], int)
    assert isinstance(details["window_expires_at"], str)


def test_email_verification_resend_after_min_interval_succeeds(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "email.resend.after.interval@example.com"
    _register_user(client=client, email=email)
    first = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-resend-interval-first-idem",
    )
    email_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        email_normalized=email,
        seconds=120,
    )
    second = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-resend-interval-second-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": email,
        },
    )
    second_payload = _response_json(second)
    assert second.status_code == 201
    assert second_payload["status"] == "challenge_issued"
    assert second_payload["challenge_id"] != first["challenge_id"]


def test_email_verification_resend_limit_is_enforced_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY", "1")
    client, _, email_verification_store = client_and_stores
    email = "email.resend.limit@example.com"
    _register_user(client=client, email=email)

    _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-resend-limit-first-idem",
    )
    email_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        email_normalized=email,
        seconds=120,
    )
    second = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-resend-limit-second-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": email,
        },
    )
    assert second.status_code == 201

    email_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        email_normalized=email,
        seconds=120,
    )
    third = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-resend-limit-third-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": email,
        },
    )
    error = _extract_error_detail(third)
    assert third.status_code == 409
    assert error["error_code"] == "otp_resend_limit_reached"
    assert error["reason"] == "otp_resend_limit_reached"
    details = cast(dict[str, object], error["details"])
    assert isinstance(details["retry_after_seconds"], int)
    assert details["resend_remaining_count"] == 0
    assert isinstance(details["window_expires_at"], str)


def test_email_verification_resend_invalidates_previous_otp_challenge(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "email.old.challenge.invalidated@example.com"
    _register_user(client=client, email=email)
    first = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-resend-invalidate-first-idem",
    )
    first_challenge_id = UUID(cast(str, first["challenge_id"]))
    first_otp = email_verification_store.get_otp_code_for_challenge(challenge_id=first_challenge_id)
    email_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        email_normalized=email,
        seconds=120,
    )
    _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-resend-invalidate-second-idem",
    )

    verify_old = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "email-verify-old-challenge-after-resend-corr"},
        json={"challenge_id": str(first_challenge_id), "otp_code": first_otp},
    )
    error = _extract_error_detail(verify_old)
    assert verify_old.status_code == 409
    assert error["error_code"] == "otp_already_used"
    assert error["reason"] == "otp_already_used"


def test_email_verification_registration_policy_defaults_match_governed_lifecycle() -> None:
    policy = get_auth_otp_policy_for_purpose("registration_verify")
    assert policy.ttl_seconds == 600
    assert policy.max_attempts == 3
    assert policy.resend_min_interval_seconds == 120
    assert policy.resend_max_per_window == 5
    assert policy.resend_window_seconds == 86400


def test_email_verification_reissue_after_attempt_limit_is_resend_throttled(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, _, email_verification_store = client_and_stores
    email = "email.cooldown.active@example.com"
    _register_user(client=client, email=email)
    issued = _issue_challenge(
        client=client,
        email=email,
        idempotency_key="email-cooldown-first-idem",
    )
    challenge_id = UUID(cast(str, issued["challenge_id"]))
    valid_otp = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp = "000000" if valid_otp != "000000" else "999999"

    max_attempts = get_email_verification_max_attempts()
    for attempt in range(max_attempts):
        response = client.post(
            "/v1/auth/otp/verify",
            headers={"X-Correlation-ID": f"email-verify-cooldown-attempt-{attempt}"},
            json={"challenge_id": str(challenge_id), "otp_code": invalid_otp},
        )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_attempt_limit_exceeded"
    details = cast(dict[str, object], error["details"])
    assert details["attempts_remaining"] == 0

    reissue = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "email-cooldown-second-idem",
            "X-Correlation-ID": "email-cooldown-second-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "email",
            "email": email,
        },
    )
    reissue_error = _extract_error_detail(reissue)
    assert reissue.status_code == 409
    assert reissue_error["error_code"] == "otp_resend_throttled"
    assert reissue_error["reason"] == "otp_resend_throttled"
    reissue_details = cast(dict[str, object], reissue_error["details"])
    assert isinstance(reissue_details["retry_after_seconds"], int)
    assert isinstance(reissue_details["window_expires_at"], str)
    assert isinstance(reissue_details["resend_remaining_count"], int)


def _register_user(*, client: TestClient, email: str) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": f"reg-{uuid4()}"},
        json={
            "email": email,
            "phone_number": f"+254700{uuid4().int % 1_000_000:06d}",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _issue_challenge(
    *,
    client: TestClient,
    email: str,
    idempotency_key: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
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
    assert "otp_code" not in detail
    assert "step_up_otp_code" not in detail
    assert "proof_code" not in detail
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
