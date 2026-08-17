"""Pilot-acceptance E2E tests for deterministic critical auth failure paths."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.login import InMemoryLoginStepUpStore
from services.auth.app.login import InMemoryLoginLockoutStore
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.password_reset import InMemoryPasswordResetStore
from services.auth.app.account_deletion import InMemoryAccountDeletionRequestStore
from services.auth.app.session_issuance import InMemorySessionIssuanceStore
from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore

_LOCKOUT_MAX_FAILED_ATTEMPTS = 3
_LOCKOUT_ATTEMPT_WINDOW_SECONDS = 60
_LOCKOUT_DURATION_SECONDS = 120


@pytest.fixture()
def e2e_failure_client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryPhoneVerificationStore,
            InMemoryPasswordResetStore,
            InMemoryAccountDeletionRequestStore,
        ]
    ]
):
    """Build deterministic E2E auth context for critical failure-path assertions."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    phone_store = InMemoryPhoneVerificationStore()
    email_store = InMemoryEmailVerificationStore()
    password_reset_store = InMemoryPasswordResetStore()
    account_deletion_store = InMemoryAccountDeletionRequestStore()
    session_store = InMemorySessionIssuanceStore()

    app.state.registration_store = registration_store
    app.state.phone_verification_store = phone_store
    app.state.email_verification_store = email_store
    app.state.password_reset_store = password_reset_store
    app.state.account_deletion_request_store = account_deletion_store
    app.state.session_issuance_store = session_store
    app.state.login_lockout_store = InMemoryLoginLockoutStore(
        max_failed_attempts=_LOCKOUT_MAX_FAILED_ATTEMPTS,
        failed_attempt_window_seconds=_LOCKOUT_ATTEMPT_WINDOW_SECONDS,
        lockout_window_seconds=_LOCKOUT_DURATION_SECONDS,
    )
    app.state.login_step_up_store = InMemoryLoginStepUpStore()

    with TestClient(app) as client:
        yield client, registration_store, phone_store, password_reset_store, account_deletion_store
    reset_default_registration_store()


def test_e2e_failure_invalid_and_expired_otp_paths_are_deterministic(
    e2e_failure_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, _, _ = e2e_failure_client_and_stores
    _register_user_only(
        client=client,
        registration_store=registration_store,
        email=f"pilot.failure.otp.invalid.{uuid4()}@example.com",
        phone_number="+254733980001",
    )
    invalid_challenge = _issue_registration_phone_challenge(
        client=client,
        phone_number="+254733980001",
        idempotency_key="e2e-failure-otp-invalid-idem",
    )
    invalid_challenge_id = UUID(cast(str, invalid_challenge["challenge_id"]))
    valid_otp = phone_store.get_otp_code_for_challenge(challenge_id=invalid_challenge_id)
    invalid_otp = "000000" if valid_otp != "000000" else "999999"

    first_invalid = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "e2e-failure-otp-invalid-corr"},
        json={"challenge_id": str(invalid_challenge_id), "otp_code": invalid_otp},
    )
    second_invalid = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "e2e-failure-otp-invalid-corr"},
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

    _register_user_only(
        client=client,
        registration_store=registration_store,
        email=f"pilot.failure.otp.expired.{uuid4()}@example.com",
        phone_number="+254733980002",
    )
    expired_challenge = _issue_registration_phone_challenge(
        client=client,
        phone_number="+254733980002",
        idempotency_key="e2e-failure-otp-expired-idem",
    )
    expired_challenge_id = UUID(cast(str, expired_challenge["challenge_id"]))
    phone_store.force_expire_challenge(challenge_id=expired_challenge_id)
    expired_otp = phone_store.get_otp_code_for_challenge(challenge_id=expired_challenge_id)

    first_expired = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "e2e-failure-otp-expired-corr"},
        json={"challenge_id": str(expired_challenge_id), "otp_code": expired_otp},
    )
    second_expired = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "e2e-failure-otp-expired-corr"},
        json={"challenge_id": str(expired_challenge_id), "otp_code": expired_otp},
    )
    first_expired_error = _extract_error_detail(first_expired)
    second_expired_error = _extract_error_detail(second_expired)
    assert first_expired.status_code == 409
    assert second_expired.status_code == 409
    assert first_expired_error["error_code"] == "otp_expired"
    assert first_expired_error["reason"] == "otp_expired"
    assert second_expired_error["error_code"] == "otp_expired"
    assert second_expired_error["reason"] == "otp_expired"
    assert set(first_expired_error.keys()) == set(second_expired_error.keys())


def test_e2e_failure_login_lockout_path_is_enforced_and_stable(
    e2e_failure_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, _, _ = e2e_failure_client_and_stores
    user = _register_and_verify_user(
        client=client,
        registration_store=registration_store,
        phone_store=phone_store,
        email=f"pilot.failure.lockout.{uuid4()}@example.com",
        phone_number="+254733980003",
    )
    headers = {
        "X-Correlation-ID": "e2e-failure-lockout-corr",
        "X-Forwarded-For": "203.0.113.240",
    }
    invalid_payload = {"login_id": user.phone_number_normalized, "password": "WrongPassw0rd!"}

    first = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    second = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    threshold_error = _extract_error_detail(threshold)
    assert first.status_code == 401
    assert second.status_code == 401
    assert threshold.status_code == 403
    assert threshold_error["error_code"] == "login_lockout_threshold_exceeded"
    assert threshold_error["reason"] == "login_lockout_threshold_exceeded"

    active_first = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_second = client.post("/v1/auth/login", headers=headers, json=invalid_payload)
    active_first_error = _extract_error_detail(active_first)
    active_second_error = _extract_error_detail(active_second)
    assert active_first.status_code == 403
    assert active_second.status_code == 403
    assert active_first_error["error_code"] == "login_lockout_active"
    assert active_first_error["reason"] == "login_lockout_active"
    assert active_second_error["error_code"] == "login_lockout_active"
    assert active_second_error["reason"] == "login_lockout_active"
    assert set(active_first_error.keys()) == set(active_second_error.keys())


def test_e2e_failure_password_reset_replay_and_expiry_are_deterministic(
    e2e_failure_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, password_reset_store, _ = e2e_failure_client_and_stores
    user = _register_and_verify_user(
        client=client,
        registration_store=registration_store,
        phone_store=phone_store,
        email=f"pilot.failure.reset.{uuid4()}@example.com",
        phone_number="+254733980004",
    )

    replay_challenge = _initiate_password_reset(
        client=client,
        email=user.email_normalized,
        idempotency_key="e2e-failure-reset-replay-initiate-idem",
        correlation_id="e2e-failure-reset-replay-initiate-corr",
    )
    replay_challenge_id = UUID(cast(str, replay_challenge["challenge_id"]))
    replay_code = password_reset_store.get_reset_code_for_challenge(
        challenge_id=replay_challenge_id
    )
    first_confirm = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-failure-reset-replay-first-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "N3wStrongPassw0rd!",
        },
    )
    assert first_confirm.status_code == 200

    replay_first = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-failure-reset-replay-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "AnotherStrongPassw0rd!",
        },
    )
    replay_second = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-failure-reset-replay-corr"},
        json={
            "challenge_id": str(replay_challenge_id),
            "reset_code": replay_code,
            "new_password": "AnotherStrongPassw0rd!",
        },
    )
    replay_first_error = _extract_error_detail(replay_first)
    replay_second_error = _extract_error_detail(replay_second)
    assert replay_first.status_code == 409
    assert replay_second.status_code == 409
    assert replay_first_error["error_code"] == "password_reset_token_already_used"
    assert replay_first_error["reason"] == "password_reset_token_already_used"
    assert canonical_json_dumps(replay_first_error) == canonical_json_dumps(replay_second_error)

    expired_challenge = _initiate_password_reset(
        client=client,
        email=user.email_normalized,
        idempotency_key="e2e-failure-reset-expired-initiate-idem",
        correlation_id="e2e-failure-reset-expired-initiate-corr",
    )
    expired_challenge_id = UUID(cast(str, expired_challenge["challenge_id"]))
    password_reset_store.force_expire_challenge(challenge_id=expired_challenge_id)
    expired_code = password_reset_store.get_reset_code_for_challenge(
        challenge_id=expired_challenge_id
    )

    expired_first = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-failure-reset-expired-corr"},
        json={
            "challenge_id": str(expired_challenge_id),
            "reset_code": expired_code,
            "new_password": "Y3tAnotherStrongPassw0rd!",
        },
    )
    expired_second = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-failure-reset-expired-corr"},
        json={
            "challenge_id": str(expired_challenge_id),
            "reset_code": expired_code,
            "new_password": "Y3tAnotherStrongPassw0rd!",
        },
    )
    expired_first_error = _extract_error_detail(expired_first)
    expired_second_error = _extract_error_detail(expired_second)
    assert expired_first.status_code == 409
    assert expired_second.status_code == 409
    assert expired_first_error["error_code"] == "password_reset_token_expired"
    assert expired_first_error["reason"] == "password_reset_token_expired"
    assert canonical_json_dumps(expired_first_error) == canonical_json_dumps(expired_second_error)


def test_e2e_failure_unauthorized_and_legal_hold_deletion_paths_are_fail_closed(
    e2e_failure_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    (
        client,
        registration_store,
        phone_store,
        _,
        account_deletion_store,
    ) = e2e_failure_client_and_stores

    unauthorized_first = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Idempotency-Key": "e2e-failure-deletion-unauthorized-idem",
            "X-Correlation-ID": "e2e-failure-deletion-unauthorized-corr",
        },
        json={"request_reason": "Unauthorized should fail."},
    )
    unauthorized_second = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Idempotency-Key": "e2e-failure-deletion-unauthorized-idem",
            "X-Correlation-ID": "e2e-failure-deletion-unauthorized-corr",
        },
        json={"request_reason": "Unauthorized should fail."},
    )
    unauthorized_first_error = _extract_error_detail(unauthorized_first)
    unauthorized_second_error = _extract_error_detail(unauthorized_second)
    assert unauthorized_first.status_code == 401
    assert unauthorized_second.status_code == 401
    assert unauthorized_first_error["error_code"] == "account_deletion_request_unauthorized"
    assert unauthorized_first_error["reason"] == "account_deletion_request_unauthorized"
    assert canonical_json_dumps(unauthorized_first_error) == canonical_json_dumps(
        unauthorized_second_error
    )

    user = _register_and_verify_user(
        client=client,
        registration_store=registration_store,
        phone_store=phone_store,
        email=f"pilot.failure.deletion.legalhold.{uuid4()}@example.com",
        phone_number="+254733980005",
    )
    account_deletion_store.set_test_precheck_context(
        user_id=user.user_id,
        tenant_id="default_tenant",
        legal_hold=True,
    )

    request_response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "Idempotency-Key": "e2e-failure-deletion-legalhold-request-idem",
            "X-Correlation-ID": "e2e-failure-deletion-legalhold-request-corr",
        },
        json={"request_reason": "Legal hold should block deletion."},
    )
    request_payload = _response_json(request_response)
    assert request_response.status_code == 201
    assert request_payload["deletion_state"] == "blocked"
    assert "deletion_blocked_legal_hold" in cast(list[str], request_payload["blockers"])
    request_id = UUID(cast(str, request_payload["request_id"]))

    confirm_first = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "Idempotency-Key": "e2e-failure-deletion-legalhold-confirm-idem",
            "X-Correlation-ID": "e2e-failure-deletion-legalhold-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": "reauth:unavailable",
            "otp_verification_id": str(uuid4()),
        },
    )
    confirm_second = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "Idempotency-Key": "e2e-failure-deletion-legalhold-confirm-idem",
            "X-Correlation-ID": "e2e-failure-deletion-legalhold-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": "reauth:unavailable",
            "otp_verification_id": str(uuid4()),
        },
    )
    confirm_first_error = _extract_error_detail(confirm_first)
    confirm_second_error = _extract_error_detail(confirm_second)
    assert confirm_first.status_code == 409
    assert confirm_second.status_code == 409
    assert confirm_first_error["error_code"] == "account_deletion_confirm_invalid_state"
    assert confirm_first_error["reason"] == "account_deletion_confirm_invalid_state"
    assert confirm_first_error["incident_code"] == "account_deletion_legal_hold_dispute"
    assert confirm_first_error["account_deletion_state"] == "blocked"
    assert isinstance(confirm_first_error["audit_reference_id"], str)
    assert confirm_second_error["incident_code"] == "account_deletion_legal_hold_dispute"
    assert confirm_second_error["account_deletion_state"] == "blocked"
    assert isinstance(confirm_second_error["audit_reference_id"], str)
    assert set(confirm_first_error.keys()) == set(confirm_second_error.keys())


def _register_user_only(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
) -> RegisteredUserRecord:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": f"e2e-failure-register-{uuid4()}"},
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
    user = registration_store.get_user_by_id(user_id=user_id)
    assert user is not None
    return user


def _register_and_verify_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    phone_store: InMemoryPhoneVerificationStore,
    email: str,
    phone_number: str,
) -> RegisteredUserRecord:
    user = _register_user_only(
        client=client,
        registration_store=registration_store,
        email=email,
        phone_number=phone_number,
    )
    issued = _issue_registration_phone_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key=f"e2e-failure-verify-{uuid4()}",
    )
    challenge_id = UUID(cast(str, issued["challenge_id"]))
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": f"e2e-failure-verify-{uuid4()}"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    assert verify_response.status_code == 200
    verified = registration_store.get_user_by_id(user_id=user.user_id)
    assert verified is not None
    assert verified.account_state == "active"
    return verified


def _issue_registration_phone_challenge(
    *,
    client: TestClient,
    phone_number: str,
    idempotency_key: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": f"e2e-failure-otp-issue-{uuid4()}",
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
        json={"purpose": "password_reset", "channel": "email", "email": email},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _auth_header(*, user_id: UUID) -> str:
    return f"Bearer user_id={user_id};tenant_id=default_tenant;role=IndividualTaxpayer"


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
