"""Integration regression suite for Phase 8.2 auth lifecycle behavior."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.password_reset import InMemoryPasswordResetStore
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
            InMemoryPasswordResetStore,
        ]
    ]
):
    """Create isolated auth app client with deterministic in-memory lifecycle stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_store = InMemoryEmailVerificationStore()
    phone_store = InMemoryPhoneVerificationStore()
    reset_store = InMemoryPasswordResetStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_store
    app.state.phone_verification_store = phone_store
    app.state.password_reset_store = reset_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, email_store, phone_store, reset_store
    reset_default_registration_store()


def test_lifecycle_happy_path_email_verification_to_password_reset(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
    ],
) -> None:
    client, registration_store, email_store, _, reset_store = client_and_stores
    email = "integration.email.happy@example.com"
    phone = "+254722550001"
    _register_user(
        client=client, email=email, phone_number=phone, correlation_id="lifecycle-email-reg-corr"
    )

    email_challenge = _issue_email_challenge(
        client=client,
        email=email,
        idempotency_key="lifecycle-email-challenge-idem",
        correlation_id="lifecycle-email-challenge-corr",
    )
    email_challenge_id = UUID(cast(str, email_challenge["challenge_id"]))
    email_otp = email_store.get_otp_code_for_challenge(challenge_id=email_challenge_id)
    verify_response = _verify_otp(
        client=client,
        challenge_id=email_challenge_id,
        otp_code=email_otp,
        correlation_id="lifecycle-email-verify-corr",
    )
    assert verify_response["status"] == "verified"

    registered_user = registration_store.get_user_by_email(email_normalized=email.lower())
    assert registered_user is not None
    assert registered_user.account_state == "active"
    before_hash = registered_user.password_hash

    reset_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="lifecycle-email-reset-idem",
        correlation_id="lifecycle-email-reset-corr",
    )
    reset_challenge_id = UUID(cast(str, reset_challenge["challenge_id"]))
    reset_code = reset_store.get_reset_code_for_challenge(challenge_id=reset_challenge_id)
    confirm_response = _confirm_password_reset(
        client=client,
        challenge_id=reset_challenge_id,
        reset_code=reset_code,
        new_password="B3tterPassw0rd!Now",
        correlation_id="lifecycle-email-reset-confirm-corr",
    )
    assert confirm_response["status"] == "password_updated"

    after_user = registration_store.get_user_by_email(email_normalized=email.lower())
    assert after_user is not None
    assert after_user.password_hash != before_hash


def test_lifecycle_happy_path_phone_verification_to_password_reset(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
    ],
) -> None:
    client, registration_store, _, phone_store, reset_store = client_and_stores
    email = "integration.phone.happy@example.com"
    phone = "+254722550002"
    _register_user(
        client=client, email=email, phone_number=phone, correlation_id="lifecycle-phone-reg-corr"
    )

    phone_challenge = _issue_phone_challenge(
        client=client,
        phone_number=phone,
        idempotency_key="lifecycle-phone-challenge-idem",
        correlation_id="lifecycle-phone-challenge-corr",
    )
    phone_challenge_id = UUID(cast(str, phone_challenge["challenge_id"]))
    phone_otp = phone_store.get_otp_code_for_challenge(challenge_id=phone_challenge_id)
    verify_response = _verify_otp(
        client=client,
        challenge_id=phone_challenge_id,
        otp_code=phone_otp,
        correlation_id="lifecycle-phone-verify-corr",
    )
    assert verify_response["status"] == "verified"

    registered_user = registration_store.get_user_by_phone(phone_number_normalized=phone)
    assert registered_user is not None
    assert registered_user.account_state == "active"

    reset_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="lifecycle-phone-reset-idem",
        correlation_id="lifecycle-phone-reset-corr",
    )
    reset_challenge_id = UUID(cast(str, reset_challenge["challenge_id"]))
    reset_code = reset_store.get_reset_code_for_challenge(challenge_id=reset_challenge_id)
    confirm_response = _confirm_password_reset(
        client=client,
        challenge_id=reset_challenge_id,
        reset_code=reset_code,
        new_password="An0therStrongPass!",
        correlation_id="lifecycle-phone-reset-confirm-corr",
    )
    assert confirm_response["status"] == "password_updated"


def test_lifecycle_duplicate_registration_conflict_is_canonical_and_deterministic(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
    ],
) -> None:
    client, _, _, _, _ = client_and_stores
    _register_user(
        client=client,
        email="integration.duplicate@example.com",
        phone_number="+254722550003",
        correlation_id="lifecycle-duplicate-first-corr",
    )

    duplicate_payload = {
        "email": "integration.duplicate@example.com",
        "phone_number": "+254722550099",
        "kra_pin": "A123456789Z",
        "password": "StrongPassw0rd!",
        "role": "IndividualTaxpayer",
    }
    headers = {"X-Correlation-ID": "lifecycle-duplicate-second-corr"}
    first_duplicate = client.post("/v1/auth/register", headers=headers, json=duplicate_payload)
    second_duplicate = client.post("/v1/auth/register", headers=headers, json=duplicate_payload)

    first_error = _extract_error_detail(first_duplicate)
    second_error = _extract_error_detail(second_duplicate)
    assert first_duplicate.status_code == 409
    assert second_duplicate.status_code == 409
    assert first_error["error_code"] == "registration_duplicate_email"
    assert first_error["message"] == "Registration request conflicts with an existing account."
    assert first_error["reason"] == "registration_duplicate_email"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_lifecycle_verification_challenge_invalid_expired_and_replay_paths_are_canonical(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
    ],
) -> None:
    client, _, email_store, _, _ = client_and_stores
    email = "integration.verify.failures@example.com"
    _register_user(
        client=client,
        email=email,
        phone_number="+254722550004",
        correlation_id="lifecycle-verify-reg-corr",
    )

    challenge = _issue_email_challenge(
        client=client,
        email=email,
        idempotency_key="lifecycle-verify-failures-idem",
        correlation_id="lifecycle-verify-failures-corr",
    )
    challenge_id = UUID(cast(str, challenge["challenge_id"]))
    valid_otp = email_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    invalid_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "lifecycle-verify-invalid-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": "000000"},
    )
    invalid_error = _extract_error_detail(invalid_response)
    assert invalid_response.status_code == 409
    assert invalid_error["error_code"] == "otp_invalid"
    assert invalid_error["reason"] == "otp_invalid"

    email_store.force_expire_challenge(challenge_id=challenge_id)
    expired_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "lifecycle-verify-expired-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": valid_otp},
    )
    expired_error = _extract_error_detail(expired_response)
    assert expired_response.status_code == 409
    assert expired_error["error_code"] == "otp_expired"
    assert expired_error["reason"] == "otp_expired"

    replay_email = "integration.verify.replay@example.com"
    _register_user(
        client=client,
        email=replay_email,
        phone_number="+254722550044",
        correlation_id="lifecycle-verify-replay-reg-corr",
    )
    replay_challenge = _issue_email_challenge(
        client=client,
        email=replay_email,
        idempotency_key="lifecycle-verify-replay-idem",
        correlation_id="lifecycle-verify-replay-corr",
    )
    replay_challenge_id = UUID(cast(str, replay_challenge["challenge_id"]))
    replay_otp = email_store.get_otp_code_for_challenge(challenge_id=replay_challenge_id)
    first_verify = _verify_otp(
        client=client,
        challenge_id=replay_challenge_id,
        otp_code=replay_otp,
        correlation_id="lifecycle-verify-replay-first-corr",
    )
    second_verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "lifecycle-verify-replay-second-corr"},
        json={"challenge_id": str(replay_challenge_id), "otp_code": replay_otp},
    )
    second_error = _extract_error_detail(second_verify_response)
    assert first_verify["status"] == "verified"
    assert second_verify_response.status_code == 409
    assert second_error["error_code"] == "otp_already_used"
    assert second_error["reason"] == "otp_already_used"


def test_lifecycle_password_reset_invalid_expired_replay_and_forbidden_state_paths_are_canonical(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
    ],
) -> None:
    client, registration_store, email_store, _, reset_store = client_and_stores
    email = "integration.reset.failures@example.com"
    _register_user(
        client=client,
        email=email,
        phone_number="+254722550005",
        correlation_id="lifecycle-reset-reg-corr",
    )

    email_challenge = _issue_email_challenge(
        client=client,
        email=email,
        idempotency_key="lifecycle-reset-verify-idem",
        correlation_id="lifecycle-reset-verify-corr",
    )
    email_challenge_id = UUID(cast(str, email_challenge["challenge_id"]))
    email_otp = email_store.get_otp_code_for_challenge(challenge_id=email_challenge_id)
    _verify_otp(
        client=client,
        challenge_id=email_challenge_id,
        otp_code=email_otp,
        correlation_id="lifecycle-reset-verify-active-corr",
    )

    invalid_payload = {
        "challenge_id": str(uuid4()),
        "reset_code": "123456",
        "new_password": "StrongAfterReset1!",
    }
    invalid_headers = {"X-Correlation-ID": "lifecycle-reset-invalid-corr"}
    invalid_first = client.post(
        "/v1/auth/password-reset/confirm",
        headers=invalid_headers,
        json=invalid_payload,
    )
    invalid_second = client.post(
        "/v1/auth/password-reset/confirm",
        headers=invalid_headers,
        json=invalid_payload,
    )
    invalid_first_error = _extract_error_detail(invalid_first)
    invalid_second_error = _extract_error_detail(invalid_second)
    assert invalid_first.status_code == 409
    assert invalid_first_error["error_code"] == "password_reset_token_invalid"
    assert invalid_first_error["message"] == "Password reset token is invalid."
    assert invalid_first_error["reason"] == "password_reset_token_invalid"
    assert canonical_json_dumps(invalid_second_error) == canonical_json_dumps(invalid_first_error)

    expired_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="lifecycle-reset-expired-idem",
        correlation_id="lifecycle-reset-expired-corr",
    )
    expired_challenge_id = UUID(cast(str, expired_challenge["challenge_id"]))
    reset_store.force_expire_challenge(challenge_id=expired_challenge_id)
    expired_code = reset_store.get_reset_code_for_challenge(challenge_id=expired_challenge_id)
    expired_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "lifecycle-reset-expired-confirm-corr"},
        json={
            "challenge_id": str(expired_challenge_id),
            "reset_code": expired_code,
            "new_password": "StrongAfterReset2!",
        },
    )
    expired_error = _extract_error_detail(expired_response)
    assert expired_response.status_code == 409
    assert expired_error["error_code"] == "password_reset_token_expired"
    assert expired_error["message"] == "Password reset token has expired."
    assert expired_error["reason"] == "password_reset_token_expired"

    replay_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="lifecycle-reset-replay-idem",
        correlation_id="lifecycle-reset-replay-corr",
    )
    replay_challenge_id = UUID(cast(str, replay_challenge["challenge_id"]))
    replay_code = reset_store.get_reset_code_for_challenge(challenge_id=replay_challenge_id)
    first_reset = _confirm_password_reset(
        client=client,
        challenge_id=replay_challenge_id,
        reset_code=replay_code,
        new_password="StrongAfterReset3!",
        correlation_id="lifecycle-reset-replay-first-corr",
    )
    second_reset_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "lifecycle-reset-replay-second-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "StrongAfterReset4!",
        },
    )
    second_reset_error = _extract_error_detail(second_reset_response)
    assert first_reset["status"] == "password_updated"
    assert second_reset_response.status_code == 409
    assert second_reset_error["error_code"] == "password_reset_token_already_used"
    assert second_reset_error["message"] == "Password reset token was already used."
    assert second_reset_error["reason"] == "password_reset_token_already_used"

    locked_challenge = _initiate_password_reset(
        client=client,
        email=email,
        idempotency_key="lifecycle-reset-locked-idem",
        correlation_id="lifecycle-reset-locked-corr",
    )
    locked_challenge_id = UUID(cast(str, locked_challenge["challenge_id"]))
    locked_code = reset_store.get_reset_code_for_challenge(challenge_id=locked_challenge_id)
    user = registration_store.get_user_by_email(email_normalized=email.lower())
    assert user is not None
    registration_store.lock_user(user_id=user.user_id)
    locked_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "lifecycle-reset-locked-confirm-corr"},
        json={
            "challenge_id": str(locked_challenge_id),
            "reset_code": locked_code,
            "new_password": "StrongAfterReset5!",
        },
    )
    locked_error = _extract_error_detail(locked_response)
    assert locked_response.status_code == 409
    assert locked_error["error_code"] == "password_reset_not_allowed_for_state"
    assert locked_error["message"] == "Password reset is not allowed for current account state."
    assert locked_error["reason"] == "password_reset_not_allowed_for_state"
    assert locked_error["current_state"] == "locked"
    assert locked_error["requested_state"] == "locked"


def test_lifecycle_challenge_initiation_is_non_enumerating_across_endpoints(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryEmailVerificationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
    ],
) -> None:
    client, _, _, _, _ = client_and_stores
    existing_email = "integration.enumeration.known@example.com"
    unknown_email = "integration.enumeration.unknown@example.com"
    _register_user(
        client=client,
        email=existing_email,
        phone_number="+254722550006",
        correlation_id="lifecycle-enumeration-reg-corr",
    )

    known_otp = _issue_email_challenge(
        client=client,
        email=existing_email,
        idempotency_key="lifecycle-enumeration-otp-known-idem",
        correlation_id="lifecycle-enumeration-otp-known-corr",
    )
    unknown_otp = _issue_email_challenge(
        client=client,
        email=unknown_email,
        idempotency_key="lifecycle-enumeration-otp-unknown-idem",
        correlation_id="lifecycle-enumeration-otp-unknown-corr",
    )
    assert known_otp["status"] == "challenge_issued"
    assert unknown_otp["status"] == "challenge_issued"
    assert set(known_otp.keys()) == set(unknown_otp.keys())
    assert "account_exists" not in known_otp
    assert "account_exists" not in unknown_otp

    known_reset = _initiate_password_reset(
        client=client,
        email=existing_email,
        idempotency_key="lifecycle-enumeration-reset-known-idem",
        correlation_id="lifecycle-enumeration-reset-known-corr",
    )
    unknown_reset = _initiate_password_reset(
        client=client,
        email=unknown_email,
        idempotency_key="lifecycle-enumeration-reset-unknown-idem",
        correlation_id="lifecycle-enumeration-reset-unknown-corr",
    )
    assert known_reset["status"] == "challenge_issued"
    assert unknown_reset["status"] == "challenge_issued"
    assert set(known_reset.keys()) == set(unknown_reset.keys())
    assert "account_exists" not in known_reset
    assert "account_exists" not in unknown_reset


def _register_user(
    *,
    client: TestClient,
    email: str,
    phone_number: str,
    correlation_id: str,
) -> dict[str, Any]:
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
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _issue_email_challenge(
    *,
    client: TestClient,
    email: str,
    idempotency_key: str,
    correlation_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "X-Correlation-ID": correlation_id,
            "Idempotency-Key": idempotency_key,
        },
        json={"purpose": "registration_verify", "channel": "email", "email": email},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _issue_phone_challenge(
    *,
    client: TestClient,
    phone_number: str,
    idempotency_key: str,
    correlation_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "X-Correlation-ID": correlation_id,
            "Idempotency-Key": idempotency_key,
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


def _verify_otp(
    *,
    client: TestClient,
    challenge_id: UUID,
    otp_code: str,
    correlation_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": correlation_id},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    payload = _response_json(response)
    assert response.status_code == 200
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
            "X-Correlation-ID": correlation_id,
            "Idempotency-Key": idempotency_key,
        },
        json={"purpose": "password_reset", "channel": "email", "email": email},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _confirm_password_reset(
    *,
    client: TestClient,
    challenge_id: UUID,
    reset_code: str,
    new_password: str,
    correlation_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": correlation_id},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": new_password,
        },
    )
    payload = _response_json(response)
    assert response.status_code == 200
    return payload


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
