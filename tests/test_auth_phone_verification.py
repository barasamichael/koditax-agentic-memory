"""Runtime tests for deterministic phone-verification registration lifecycle flow."""

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
from services.auth.app.config import get_phone_verification_max_attempts
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import SmsDeliveryResult
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore
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
    Iterator[tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore]]
):
    """Create isolated auth app client with injected deterministic in-memory stores."""

    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_verification_store
    app.state.phone_verification_store = phone_verification_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, phone_verification_store


class _DeterministicSmsDeliveryAdapter:
    """Provide deterministic SMS delivery outcomes by normalized phone number."""

    def __init__(
        self,
        *,
        outcomes_by_phone: dict[str, SmsDeliveryResult] | None = None,
    ) -> None:
        self._outcomes_by_phone = outcomes_by_phone or {}

    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
        otp_code: str,
    ) -> SmsDeliveryResult:
        del purpose, otp_code
        return self._outcomes_by_phone.get(
            phone_number_normalized,
            SmsDeliveryResult(status="delivered", reason_code="sms_delivery_delivered"),
        )


class _DeterministicEmailDeliveryAdapter:
    """Provide deterministic email delivery outcomes by normalized email destination."""

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
                reason_code="email_delivery_delivered",
            ),
        )


def test_phone_verification_positive_flow_updates_registration_state(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, registration_store, phone_verification_store = client_and_stores
    phone_number = "+254700111111"
    _register_user(client=client, phone_number=phone_number)

    issued_challenge = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-positive-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-positive-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )

    verify_payload = _response_json(verify_response)
    assert verify_response.status_code == 200
    assert verify_payload["status"] == "verified"
    assert verify_payload["verification_status"] == "verified"
    registered_user = registration_store.get_user_by_phone(phone_number_normalized=phone_number)
    assert registered_user is not None
    assert registered_user.verification_state == "verified"
    assert registered_user.verified_at is not None


def test_dev_otp_endpoint_returns_phone_challenge_from_app_state_store(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111199"
    challenge_payload = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="dev-phone-otp-idem",
    )
    challenge_id = UUID(cast(str, challenge_payload["challenge_id"]))

    response = client.get(f"/dev/otp/{challenge_id}")
    payload = _response_json(response)

    assert response.status_code == 200
    assert payload["otp_code"] == phone_verification_store.get_otp_code_for_challenge(
        challenge_id=challenge_id
    )
    assert payload["channel"] == "sms"
    assert payload["purpose"] == "registration_verify"
    assert isinstance(payload["expires_at"], str)
    assert payload["consumed_at"] is None


@pytest.mark.parametrize(
    ("purpose", "phone_number"),
    [
        ("registration_verify", "+254700211111"),
        ("login_step_up", "+254700211112"),
        ("recovery", "+254700211113"),
        ("phone_change_confirm", "+254700211114"),
    ],
)
def test_phone_verification_supported_purpose_matrix_issues_challenge_successfully(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
    purpose: str,
    phone_number: str,
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": f"phone-purpose-{purpose}",
            "X-Correlation-ID": f"phone-purpose-{purpose}-corr",
        },
        json={
            "purpose": purpose,
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["status"] == "challenge_issued"
    assert isinstance(payload["challenge_id"], str)
    assert isinstance(payload["expires_at"], str)
    assert "otp_code" not in payload


def test_phone_verification_unsupported_purpose_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-purpose-unsupported-idem",
            "X-Correlation-ID": "phone-purpose-unsupported-corr",
        },
        json={
            "purpose": "unsupported_purpose",
            "channel": "sms",
            "phone_number": "+254700211115",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "invalid_otp_challenge_request"
    assert error["reason"] == "unsupported_otp_challenge_context"


def test_phone_verification_missing_phone_subject_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-subject-missing-idem",
            "X-Correlation-ID": "phone-subject-missing-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "invalid_otp_challenge_request"
    assert error["reason"] == "invalid_otp_challenge_request"


def test_phone_verification_same_idempotency_key_and_payload_replays_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    payload = {
        "purpose": "registration_verify",
        "channel": "sms",
        "phone_number": "+254700211116",
    }
    headers = {
        "Idempotency-Key": "phone-idempotency-replay-idem",
        "X-Correlation-ID": "phone-idempotency-replay-corr",
    }
    first = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    second = client.post("/v1/auth/otp/challenges", headers=headers, json=payload)
    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second_payload == first_payload


def test_phone_verification_expired_otp_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111112"
    _register_user(client=client, phone_number=phone_number)
    issued_challenge = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-expired-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    phone_verification_store.force_expire_challenge(challenge_id=challenge_id)
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-expired-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )

    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_expired"
    assert error["reason"] == "otp_expired"


def test_phone_verification_invalid_otp_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111113"
    _register_user(client=client, phone_number=phone_number)
    issued_challenge = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-invalid-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    valid_otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp_code = "000000" if valid_otp_code != "000000" else "999999"
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-invalid-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": invalid_otp_code},
    )

    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_invalid"
    assert error["reason"] == "otp_invalid"


def test_phone_verification_replay_is_rejected_after_success(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111114"
    _register_user(client=client, phone_number=phone_number)
    issued_challenge = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-replay-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    first_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-replay-first-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    second_verify = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-replay-second-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )

    first_payload = _response_json(first_verify)
    second_error = _extract_error_detail(second_verify)
    assert first_verify.status_code == 200
    assert first_payload["verification_status"] == "verified"
    assert second_verify.status_code == 409
    assert second_error["error_code"] == "otp_already_used"
    assert second_error["reason"] == "otp_already_used"


def test_phone_verification_attempt_limit_exceeded_is_deterministic(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111115"
    _register_user(client=client, phone_number=phone_number)
    issued_challenge = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-attempt-limit-idem",
    )
    challenge_id = UUID(cast(str, issued_challenge["challenge_id"]))
    valid_otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp_code = "000000" if valid_otp_code != "000000" else "999999"

    max_attempts = get_phone_verification_max_attempts()
    final_error: dict[str, object] | None = None
    for attempt in range(max_attempts):
        response = client.post(
            "/v1/auth/otp/verify",
            headers={"X-Correlation-ID": f"phone-verify-attempt-{attempt}"},
            json={"challenge_id": str(challenge_id), "otp_code": invalid_otp_code},
        )
        final_error = _extract_error_detail(response)
    assert final_error is not None
    assert final_error["error_code"] == "otp_attempt_limit_exceeded"
    assert final_error["reason"] == "otp_attempt_limit_exceeded"

    repeated = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-attempt-repeat"},
        json={"challenge_id": str(challenge_id), "otp_code": invalid_otp_code},
    )
    repeated_error = _extract_error_detail(repeated)
    assert repeated.status_code == 409
    assert repeated_error["error_code"] == "otp_attempt_limit_exceeded"
    assert repeated_error["reason"] == "otp_attempt_limit_exceeded"


def test_phone_verification_wrong_purpose_challenge_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, registration_store, phone_verification_store = client_and_stores
    phone_number = "+254700211201"
    _register_user(client=client, phone_number=phone_number)
    challenge_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-wrong-purpose-idem",
            "X-Correlation-ID": "phone-verify-wrong-purpose-corr",
        },
        json={
            "purpose": "login_step_up",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    payload = _response_json(challenge_response)
    assert challenge_response.status_code == 201
    challenge_id = UUID(cast(str, payload["challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-wrong-purpose-verify-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    error = _extract_error_detail(verify_response)
    assert registration_store.get_user_by_phone(phone_number_normalized=phone_number) is not None
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_challenge_context_mismatch"
    assert error["reason"] == "otp_challenge_context_mismatch"


def test_phone_verification_wrong_subject_context_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    unknown_phone = "+254700211202"
    challenge_payload = _issue_challenge(
        client=client,
        phone_number=unknown_phone,
        idempotency_key="phone-verify-wrong-subject-idem",
    )
    challenge_id = UUID(cast(str, challenge_payload["challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-wrong-subject-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    error = _extract_error_detail(verify_response)
    assert verify_response.status_code == 409
    assert error["error_code"] == "otp_challenge_context_mismatch"
    assert error["reason"] == "otp_challenge_context_mismatch"


def test_phone_verification_challenge_issuance_remains_non_enumerating(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    known_phone = "+254700111116"
    unknown_phone = "+254700111117"
    _register_user(client=client, phone_number=known_phone)

    known_response = _issue_challenge(
        client=client,
        phone_number=known_phone,
        idempotency_key="phone-verify-known-idem",
    )
    unknown_response = _issue_challenge(
        client=client,
        phone_number=unknown_phone,
        idempotency_key="phone-verify-unknown-idem",
    )

    assert known_response["status"] == "challenge_issued"
    assert unknown_response["status"] == "challenge_issued"
    assert set(known_response.keys()) == set(unknown_response.keys())
    assert "account_exists" not in known_response
    assert "account_exists" not in unknown_response


def test_phone_verification_resend_throttle_is_enforced_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    phone_number = "+254700111118"
    _register_user(client=client, phone_number=phone_number)

    first = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-throttle-first-idem",
    )
    second_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-throttle-second-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )

    second_error = _extract_error_detail(second_response)
    assert first["status"] == "challenge_issued"
    assert second_response.status_code == 409
    assert second_error["error_code"] == "otp_resend_throttled"
    assert second_error["reason"] == "otp_resend_throttled"
    details = cast(dict[str, object], second_error["details"])
    assert isinstance(details["retry_after_seconds"], int)
    assert isinstance(details["resend_remaining_count"], int)
    assert isinstance(details["window_expires_at"], str)


def test_phone_verification_resend_after_min_interval_succeeds(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111134"
    _register_user(client=client, phone_number=phone_number)
    first_response = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-resend-after-window-first-idem",
    )
    phone_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        phone_number_normalized=phone_number,
        seconds=120,
    )
    second = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-resend-after-window-second-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    second_payload = _response_json(second)
    assert second.status_code == 201
    assert second_payload["status"] == "challenge_issued"
    assert second_payload["challenge_id"] != first_response["challenge_id"]


def test_phone_verification_resend_limit_is_enforced_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY", "1")
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111135"
    _register_user(client=client, phone_number=phone_number)

    _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-resend-limit-first-idem",
    )
    phone_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        phone_number_normalized=phone_number,
        seconds=120,
    )
    second = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-resend-limit-second-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    assert second.status_code == 201

    phone_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        phone_number_normalized=phone_number,
        seconds=120,
    )
    third = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-resend-limit-third-idem",
            "X-Correlation-ID": f"challenge-{uuid4()}",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
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


def test_phone_verification_resend_invalidates_previous_otp_challenge(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111136"
    _register_user(client=client, phone_number=phone_number)
    first = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-invalidate-first-idem",
    )
    first_challenge_id = UUID(cast(str, first["challenge_id"]))
    first_otp = phone_verification_store.get_otp_code_for_challenge(challenge_id=first_challenge_id)
    phone_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        phone_number_normalized=phone_number,
        seconds=120,
    )
    _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-invalidate-second-idem",
    )

    verify_old = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-old-challenge-after-resend-corr"},
        json={"challenge_id": str(first_challenge_id), "otp_code": first_otp},
    )
    error = _extract_error_detail(verify_old)
    assert verify_old.status_code == 409
    assert error["error_code"] == "otp_already_used"
    assert error["reason"] == "otp_already_used"


def test_phone_verification_new_challenge_resets_attempt_counter(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111137"
    _register_user(client=client, phone_number=phone_number)
    first = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-attempt-reset-first-idem",
    )
    first_challenge_id = UUID(cast(str, first["challenge_id"]))
    first_valid_otp = phone_verification_store.get_otp_code_for_challenge(
        challenge_id=first_challenge_id
    )
    first_invalid_otp = "000000" if first_valid_otp != "000000" else "999999"
    first_invalid = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-attempt-reset-first-invalid-corr"},
        json={"challenge_id": str(first_challenge_id), "otp_code": first_invalid_otp},
    )
    first_invalid_error = _extract_error_detail(first_invalid)
    assert first_invalid.status_code == 409
    assert first_invalid_error["error_code"] == "otp_invalid"

    phone_verification_store.force_backdate_subject_state(
        purpose="registration_verify",
        phone_number_normalized=phone_number,
        seconds=120,
    )
    second = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-attempt-reset-second-idem",
    )
    second_challenge_id = UUID(cast(str, second["challenge_id"]))
    second_valid_otp = phone_verification_store.get_otp_code_for_challenge(
        challenge_id=second_challenge_id
    )
    second_invalid_otp = "000000" if second_valid_otp != "000000" else "999999"
    second_invalid = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "phone-verify-attempt-reset-second-invalid-corr"},
        json={"challenge_id": str(second_challenge_id), "otp_code": second_invalid_otp},
    )
    second_invalid_error = _extract_error_detail(second_invalid)
    assert second_invalid.status_code == 409
    assert second_invalid_error["error_code"] == "otp_invalid"
    details = cast(dict[str, object], second_invalid_error["details"])
    assert isinstance(details["attempts_remaining"], int)
    assert cast(int, details["attempts_remaining"]) >= 1


def test_phone_verification_registration_policy_defaults_match_governed_lifecycle() -> None:
    policy = get_auth_otp_policy_for_purpose("registration_verify")
    assert policy.ttl_seconds == 600
    assert policy.max_attempts == 3
    assert policy.resend_min_interval_seconds == 120
    assert policy.resend_max_per_window == 5
    assert policy.resend_window_seconds == 86400


def test_phone_verification_reissue_after_attempt_limit_is_resend_throttled(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, phone_verification_store = client_and_stores
    phone_number = "+254700111138"
    _register_user(client=client, phone_number=phone_number)
    issued = _issue_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="phone-verify-cooldown-first-idem",
    )
    challenge_id = UUID(cast(str, issued["challenge_id"]))
    valid_otp = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    invalid_otp = "000000" if valid_otp != "000000" else "999999"

    max_attempts = get_phone_verification_max_attempts()
    for attempt in range(max_attempts):
        response = client.post(
            "/v1/auth/otp/verify",
            headers={"X-Correlation-ID": f"phone-verify-cooldown-attempt-{attempt}"},
            json={"challenge_id": str(challenge_id), "otp_code": invalid_otp},
        )
    last_error = _extract_error_detail(response)
    assert response.status_code == 409
    assert last_error["error_code"] == "otp_attempt_limit_exceeded"
    last_details = cast(dict[str, object], last_error["details"])
    assert last_details["attempts_remaining"] == 0

    reissue = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-cooldown-second-idem",
            "X-Correlation-ID": "phone-verify-cooldown-second-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
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


def test_phone_verification_sms_delivery_failure_is_deterministic_and_replay_safe(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    phone_number = "+254700111119"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_retryable",
                reason_code="sms_delivery_provider_timeout",
            )
        }
    )

    first_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-sms-fail-idem",
            "X-Correlation-ID": "phone-verify-sms-fail-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )
    second_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-sms-fail-idem",
            "X-Correlation-ID": "phone-verify-sms-fail-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )

    first_error = _extract_error_detail(first_response)
    second_error = _extract_error_detail(second_response)
    assert first_response.status_code == 409
    assert first_error["error_code"] == "otp_primary_delivery_failed_retryable"
    assert first_error["reason"] == "otp_primary_delivery_failed_retryable"
    details = cast(dict[str, object], first_error["details"])
    assert details["delivery_failure_class"] == "failed_retryable"
    assert details["primary_channel"] == "sms"
    assert details["retry_after_seconds"] == 60
    assert "fallback_channel_attempted" not in details
    assert second_response.status_code == 409
    assert second_error == first_error


def test_phone_verification_sms_provider_unavailable_without_fallback_is_canonical(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    phone_number = "+254700111129"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_non_retryable",
                reason_code="sms_delivery_provider_unavailable",
            )
        }
    )

    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-sms-provider-unavailable-idem",
            "X-Correlation-ID": "phone-verify-sms-provider-unavailable-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_primary_delivery_failed_non_retryable"
    assert error["reason"] == "otp_primary_delivery_failed_non_retryable"
    details = cast(dict[str, object], error["details"])
    assert details["delivery_failure_class"] == "failed_non_retryable"
    assert details["primary_channel"] == "sms"
    assert "retry_after_seconds" not in details


def test_phone_verification_sms_failure_fallback_to_email_succeeds_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    phone_number = "+254700111120"
    fallback_email = "fallback.delivery@example.com"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_non_retryable",
                reason_code="sms_delivery_provider_unavailable",
            )
        }
    )

    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-email-idem",
            "X-Correlation-ID": "phone-verify-fallback-email-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
            "fallback_channel": "email",
            "email": fallback_email,
        },
    )

    payload = _response_json(response)
    challenge_id = UUID(cast(str, payload["challenge_id"]))
    email_store = cast(InMemoryEmailVerificationStore, client.app.state.email_verification_store)
    assert response.status_code == 201
    assert payload["status"] == "challenge_issued"
    assert email_store.get_challenge(challenge_id=challenge_id) is not None


def test_phone_verification_sms_failure_with_unavailable_fallback_is_rejected(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    phone_number = "+254700111121"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_retryable",
                reason_code="sms_delivery_provider_unavailable",
            )
        }
    )
    client.app.state.email_delivery_adapter = _DeterministicEmailDeliveryAdapter(
        outcomes_by_email={
            "fallback.unavailable@example.com": OtpDeliveryOutcome(
                status="failed_non_retryable",
                reason_code="email_delivery_provider_unavailable",
            )
        }
    )

    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-unavailable-idem",
            "X-Correlation-ID": "phone-verify-fallback-unavailable-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
            "fallback_channel": "email",
            "email": "fallback.unavailable@example.com",
        },
    )
    repeated = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-unavailable-idem",
            "X-Correlation-ID": "phone-verify-fallback-unavailable-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
            "fallback_channel": "email",
            "email": "fallback.unavailable@example.com",
        },
    )

    error = _extract_error_detail(response)
    repeated_error = _extract_error_detail(repeated)
    assert response.status_code == 409
    assert repeated.status_code == 409
    assert error["error_code"] == "otp_fallback_channel_unavailable"
    assert error["reason"] == "otp_fallback_channel_unavailable"
    assert repeated_error["error_code"] == "otp_fallback_channel_unavailable"
    assert repeated_error["reason"] == "otp_fallback_channel_unavailable"
    assert set(error) == set(repeated_error)
    details = cast(dict[str, object], error["details"])
    assert details["delivery_failure_class"] == "failed_retryable"
    assert details["primary_channel"] == "sms"
    assert details["fallback_channel_requested"] == "email"
    assert "retry_after_seconds" not in details
    assert details["fallback_channel_attempted"] == "email"


def test_phone_verification_sms_failure_with_policy_disallowed_fallback_is_rejected(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = client_and_stores
    phone_number = "+254700111122"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            phone_number: SmsDeliveryResult(
                status="failed_non_retryable",
                reason_code="sms_delivery_provider_unavailable",
            )
        }
    )
    monkeypatch.setenv("AUTH_OTP_SMS_EMAIL_FALLBACK_ENABLED", "false")

    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-disallowed-idem",
            "X-Correlation-ID": "phone-verify-fallback-disallowed-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": phone_number,
            "fallback_channel": "email",
            "email": "fallback.policy@example.com",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_fallback_not_allowed_for_purpose"
    assert error["reason"] == "otp_fallback_not_allowed_for_purpose"
    details = cast(dict[str, object], error["details"])
    assert details["primary_channel"] == "sms"
    assert details["fallback_channel_requested"] == "email"


def test_phone_verification_fallback_for_disallowed_purpose_is_rejected(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-purpose-disallowed-idem",
            "X-Correlation-ID": "phone-verify-fallback-purpose-disallowed-corr",
        },
        json={
            "purpose": "login_step_up",
            "channel": "sms",
            "phone_number": "+254700111132",
            "fallback_channel": "email",
            "email": "fallback.stepup@example.com",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_fallback_not_allowed_for_purpose"
    assert error["reason"] == "otp_fallback_not_allowed_for_purpose"
    details = cast(dict[str, object], error["details"])
    assert details["primary_channel"] == "sms"
    assert details["fallback_channel_requested"] == "email"


def test_phone_verification_fallback_missing_required_context_is_rejected(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-context-missing-idem",
            "X-Correlation-ID": "phone-verify-fallback-context-missing-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": "+254700111133",
            "fallback_channel": "email",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_fallback_context_missing"
    assert error["reason"] == "otp_fallback_context_missing"
    details = cast(dict[str, object], error["details"])
    assert details["primary_channel"] == "sms"
    assert details["fallback_channel_requested"] == "email"


def test_phone_verification_unsupported_fallback_channel_is_rejected_deterministically(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-fallback-unsupported-idem",
            "X-Correlation-ID": "phone-verify-fallback-unsupported-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": "+254700111130",
            "fallback_channel": "sms",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "invalid_otp_challenge_request"
    assert error["reason"] == "unsupported_otp_fallback_channel"


def test_phone_verification_sms_failure_conflicting_idempotency_reuse_is_rejected(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
) -> None:
    client, _, _ = client_and_stores
    first_phone = "+254700111123"
    second_phone = "+254700111124"
    client.app.state.sms_delivery_adapter = _DeterministicSmsDeliveryAdapter(
        outcomes_by_phone={
            first_phone: SmsDeliveryResult(
                status="failed_retryable",
                reason_code="sms_delivery_provider_timeout",
            ),
            second_phone: SmsDeliveryResult(
                status="failed_retryable",
                reason_code="sms_delivery_provider_timeout",
            ),
        }
    )

    first_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-sms-fail-conflict-idem",
            "X-Correlation-ID": "phone-verify-sms-fail-conflict-first-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": first_phone,
        },
    )
    second_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-sms-fail-conflict-idem",
            "X-Correlation-ID": "phone-verify-sms-fail-conflict-second-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": second_phone,
        },
    )

    first_error = _extract_error_detail(first_response)
    second_error = _extract_error_detail(second_response)
    assert first_response.status_code == 409
    assert first_error["error_code"] == "otp_primary_delivery_failed_retryable"
    assert second_response.status_code == 409
    assert second_error["error_code"] == "idempotency_key_conflict"
    assert second_error["reason"] == "idempotency_key_reused_with_different_request"


def test_phone_verification_provider_misconfiguration_fails_closed(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryPhoneVerificationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = client_and_stores
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_OTP_SMS_PROVIDER_MODE", "stub")
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": "phone-verify-provider-misconfigured-idem",
            "X-Correlation-ID": "phone-verify-provider-misconfigured-corr",
        },
        json={
            "purpose": "registration_verify",
            "channel": "sms",
            "phone_number": "+254700111131",
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "otp_delivery_provider_misconfigured"
    assert error["reason"] == "otp_delivery_provider_misconfigured"


def _register_user(*, client: TestClient, phone_number: str) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": f"reg-{uuid4()}"},
        json={
            "email": f"user-{uuid4()}@example.com",
            "phone_number": phone_number,
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
    phone_number: str,
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
            "channel": "sms",
            "phone_number": phone_number,
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
