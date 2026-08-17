"""Runtime tests for deterministic phone-number change workflow with step-up verification."""

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
from services.auth.app.phone_change import InMemoryPhoneChangeStore
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore


@pytest.fixture()
def client_and_stores() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryRegistrationStore,
            InMemoryPhoneVerificationStore,
            InMemoryPhoneChangeStore,
        ]
    ]
):
    """Create isolated auth app client with deterministic phone-change stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    phone_verification_store = InMemoryPhoneVerificationStore()
    phone_change_store = InMemoryPhoneChangeStore()
    app.state.registration_store = registration_store
    app.state.phone_verification_store = phone_verification_store
    app.state.phone_change_store = phone_change_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, phone_verification_store, phone_change_store
    reset_default_registration_store()


def test_phone_change_positive_flow_updates_phone_and_login_identifier(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, phone_verification_store, phone_change_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-positive@example.com",
        phone_number="+254733510001",
        correlation_id="phone-change-positive-register-corr",
    )

    request_payload = _create_phone_change_request(
        client=client,
        user_id=user_id,
        idempotency_key="phone-change-positive-request-idem",
        correlation_id="phone-change-positive-request-corr",
        new_phone_number="+254733510101",
        current_password="StrongPassw0rd!",
    )
    request_id = UUID(cast(str, request_payload["request_id"]))
    challenge_id = UUID(cast(str, request_payload["step_up_challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    confirm_response = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-positive-confirm-idem",
            "X-Correlation-ID": "phone-change-positive-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    confirm_payload = _response_json(confirm_response)

    assert confirm_response.status_code == 200
    assert confirm_payload["status"] == "phone_updated"
    assert confirm_payload["phone_change_state"] == "confirmed"
    assert confirm_payload["request_id"] == str(request_id)
    assert confirm_payload["updated_phone_number"] == "+254733510101"

    updated_user = registration_store.get_user_by_id(user_id=user_id)
    assert updated_user is not None
    assert updated_user.phone_number_normalized == "+254733510101"
    assert registration_store.get_user_by_phone(phone_number_normalized="+254733510001") is None

    old_login = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "phone-change-old-login-corr",
            "X-Forwarded-For": "203.0.113.150",
        },
        json={"login_id": "+254733510001", "password": "StrongPassw0rd!"},
    )
    old_login_error = _extract_error_detail(old_login)
    assert old_login.status_code == 401
    assert old_login_error["error_code"] == "login_invalid_credentials"
    assert old_login_error["reason"] == "login_invalid_credentials"

    new_login = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "phone-change-new-login-corr",
            "X-Forwarded-For": "203.0.113.151",
        },
        json={"login_id": "+254733510101", "password": "StrongPassw0rd!"},
    )
    new_login_payload = _response_json(new_login)
    assert new_login.status_code == 200
    assert new_login_payload["status"] == "pending_step_up"

    persisted_request = phone_change_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.phone_change_state == "confirmed"

    audit_events = phone_change_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 2
    assert audit_events[0].event_type == "phone_change_requested"
    assert audit_events[1].event_type == "phone_change_confirmed"
    assert audit_events[0].event_id == audit_events[0].audit_evidence_id
    assert audit_events[1].event_id == audit_events[1].audit_evidence_id


def test_phone_change_missing_step_up_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, _, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-stepup-required@example.com",
        phone_number="+254733510002",
        correlation_id="phone-change-stepup-required-register-corr",
    )

    response = client.post(
        "/v1/auth/phone-change/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-stepup-required-idem",
            "X-Correlation-ID": "phone-change-stepup-required-corr",
        },
        json={
            "new_phone_number": "+254733510102",
            "current_password": "   ",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 403
    assert error["error_code"] == "phone_change_step_up_required"
    assert error["reason"] == "phone_change_step_up_required"


def test_phone_change_invalid_step_up_proof_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, _, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-stepup-invalid@example.com",
        phone_number="+254733510003",
        correlation_id="phone-change-stepup-invalid-register-corr",
    )

    response = client.post(
        "/v1/auth/phone-change/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-stepup-invalid-idem",
            "X-Correlation-ID": "phone-change-stepup-invalid-corr",
        },
        json={
            "new_phone_number": "+254733510103",
            "current_password": "WrongPassw0rd!",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "phone_change_step_up_invalid"
    assert error["reason"] == "phone_change_step_up_invalid"


def test_phone_change_malformed_phone_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, _, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-invalid-format@example.com",
        phone_number="+254733510004",
        correlation_id="phone-change-invalid-format-register-corr",
    )

    response = client.post(
        "/v1/auth/phone-change/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-invalid-format-idem",
            "X-Correlation-ID": "phone-change-invalid-format-corr",
        },
        json={
            "new_phone_number": "ABC-123",
            "current_password": "StrongPassw0rd!",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "phone_change_target_phone_invalid"
    assert error["reason"] == "phone_change_target_phone_invalid"


def test_phone_change_conflict_existing_account_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, _, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-owner@example.com",
        phone_number="+254733510005",
        correlation_id="phone-change-owner-register-corr",
    )
    _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-conflict@example.com",
        phone_number="+254733510105",
        correlation_id="phone-change-conflict-register-corr",
    )

    response = client.post(
        "/v1/auth/phone-change/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-conflict-idem",
            "X-Correlation-ID": "phone-change-conflict-corr",
        },
        json={
            "new_phone_number": "+254733510105",
            "current_password": "StrongPassw0rd!",
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "phone_change_target_phone_already_registered"
    assert error["reason"] == "phone_change_target_phone_already_registered"


def test_phone_change_wrong_user_confirmation_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, phone_verification_store, _ = client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-owner-confirm@example.com",
        phone_number="+254733510006",
        correlation_id="phone-change-owner-confirm-register-corr",
    )
    attacker_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-attacker@example.com",
        phone_number="+254733510007",
        correlation_id="phone-change-attacker-register-corr",
    )

    request_payload = _create_phone_change_request(
        client=client,
        user_id=owner_user_id,
        idempotency_key="phone-change-wrong-user-request-idem",
        correlation_id="phone-change-wrong-user-request-corr",
        new_phone_number="+254733510107",
        current_password="StrongPassw0rd!",
    )
    request_id = UUID(cast(str, request_payload["request_id"]))
    challenge_id = UUID(cast(str, request_payload["step_up_challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    response = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=attacker_user_id),
            "Idempotency-Key": "phone-change-wrong-user-confirm-idem",
            "X-Correlation-ID": "phone-change-wrong-user-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )

    error = _extract_error_detail(response)
    assert response.status_code == 403
    assert error["error_code"] == "phone_change_unauthorized"
    assert error["reason"] == "phone_change_unauthorized"


def test_phone_change_request_and_confirm_idempotency_replay_and_conflict(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, phone_verification_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-idempotency@example.com",
        phone_number="+254733510008",
        correlation_id="phone-change-idempotency-register-corr",
    )

    request_headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "phone-change-idempotency-request-idem",
        "X-Correlation-ID": "phone-change-idempotency-request-corr",
    }
    request_body = {
        "new_phone_number": "+254733510108",
        "current_password": "StrongPassw0rd!",
    }

    first_request = client.post(
        "/v1/auth/phone-change/requests",
        headers=request_headers,
        json=request_body,
    )
    second_request = client.post(
        "/v1/auth/phone-change/requests",
        headers=request_headers,
        json=request_body,
    )
    first_request_payload = _response_json(first_request)
    second_request_payload = _response_json(second_request)
    assert first_request.status_code == 201
    assert second_request.status_code == 201
    assert canonical_json_dumps(second_request_payload) == canonical_json_dumps(
        first_request_payload
    )

    conflicting_request = client.post(
        "/v1/auth/phone-change/requests",
        headers=request_headers,
        json={
            "new_phone_number": "+254733510109",
            "current_password": "StrongPassw0rd!",
        },
    )
    conflicting_request_error = _extract_error_detail(conflicting_request)
    assert conflicting_request.status_code == 409
    assert conflicting_request_error["error_code"] == "idempotency_key_conflict"
    assert conflicting_request_error["reason"] == "idempotency_key_conflict"

    request_id = UUID(cast(str, first_request_payload["request_id"]))
    challenge_id = UUID(cast(str, first_request_payload["step_up_challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    confirm_headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "phone-change-idempotency-confirm-idem",
        "X-Correlation-ID": "phone-change-idempotency-confirm-corr",
    }
    confirm_body = {
        "request_id": str(request_id),
        "step_up_challenge_id": str(challenge_id),
        "step_up_otp_code": otp_code,
    }

    first_confirm = client.post(
        "/v1/auth/phone-change/confirm",
        headers=confirm_headers,
        json=confirm_body,
    )
    second_confirm = client.post(
        "/v1/auth/phone-change/confirm",
        headers=confirm_headers,
        json=confirm_body,
    )
    first_confirm_payload = _response_json(first_confirm)
    second_confirm_payload = _response_json(second_confirm)
    assert first_confirm.status_code == 200
    assert second_confirm.status_code == 200
    assert canonical_json_dumps(second_confirm_payload) == canonical_json_dumps(
        first_confirm_payload
    )

    conflicting_confirm = client.post(
        "/v1/auth/phone-change/confirm",
        headers=confirm_headers,
        json={
            "request_id": str(uuid4()),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    conflicting_confirm_error = _extract_error_detail(conflicting_confirm)
    assert conflicting_confirm.status_code == 409
    assert conflicting_confirm_error["error_code"] == "idempotency_key_conflict"
    assert conflicting_confirm_error["reason"] == "idempotency_key_conflict"


def test_phone_change_supersedes_previous_pending_request(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, phone_verification_store, phone_change_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-supersede@example.com",
        phone_number="+254733510019",
        correlation_id="phone-change-supersede-register-corr",
    )

    first_request_payload = _create_phone_change_request(
        client=client,
        user_id=user_id,
        idempotency_key="phone-change-supersede-first-idem",
        correlation_id="phone-change-supersede-first-corr",
        new_phone_number="+254733510119",
        current_password="StrongPassw0rd!",
    )
    first_request_id = UUID(cast(str, first_request_payload["request_id"]))
    first_challenge_id = UUID(cast(str, first_request_payload["step_up_challenge_id"]))
    first_otp_code = phone_verification_store.get_otp_code_for_challenge(
        challenge_id=first_challenge_id
    )

    second_request_payload = _create_phone_change_request(
        client=client,
        user_id=user_id,
        idempotency_key="phone-change-supersede-second-idem",
        correlation_id="phone-change-supersede-second-corr",
        new_phone_number="+254733510129",
        current_password="StrongPassw0rd!",
    )
    second_request_id = UUID(cast(str, second_request_payload["request_id"]))

    superseded_request = phone_change_store.get_request_by_id(request_id=first_request_id)
    assert superseded_request is not None
    assert superseded_request.phone_change_state == "superseded"

    superseded_confirm = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-supersede-first-confirm-idem",
            "X-Correlation-ID": "phone-change-supersede-first-confirm-corr",
        },
        json={
            "request_id": str(first_request_id),
            "step_up_challenge_id": str(first_challenge_id),
            "step_up_otp_code": first_otp_code,
        },
    )
    superseded_error = _extract_error_detail(superseded_confirm)
    assert superseded_confirm.status_code == 409
    assert superseded_error["error_code"] == "phone_change_request_invalid"
    assert superseded_error["reason"] == "phone_change_request_invalid"

    active_second_request = phone_change_store.get_request_by_id(request_id=second_request_id)
    assert active_second_request is not None
    assert active_second_request.phone_change_state == "pending_confirmation"


def test_phone_change_expired_step_up_proof_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, phone_verification_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-expired-step-up@example.com",
        phone_number="+254733510013",
        correlation_id="phone-change-expired-step-up-register-corr",
    )
    request_payload = _create_phone_change_request(
        client=client,
        user_id=user_id,
        idempotency_key="phone-change-expired-step-up-request-idem",
        correlation_id="phone-change-expired-step-up-request-corr",
        new_phone_number="+254733510113",
        current_password="StrongPassw0rd!",
    )
    request_id = UUID(cast(str, request_payload["request_id"]))
    challenge_id = UUID(cast(str, request_payload["step_up_challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    phone_verification_store.force_expire_challenge(challenge_id=challenge_id)

    response = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-expired-step-up-confirm-idem",
            "X-Correlation-ID": "phone-change-expired-step-up-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "phone_change_step_up_expired"
    assert error["reason"] == "phone_change_step_up_expired"


def test_phone_change_mismatched_challenge_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, _, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-mismatch-step-up@example.com",
        phone_number="+254733510014",
        correlation_id="phone-change-mismatch-step-up-register-corr",
    )
    first_request_payload = _create_phone_change_request(
        client=client,
        user_id=user_id,
        idempotency_key="phone-change-mismatch-step-up-first-idem",
        correlation_id="phone-change-mismatch-step-up-first-corr",
        new_phone_number="+254733510114",
        current_password="StrongPassw0rd!",
    )
    request_id = UUID(cast(str, first_request_payload["request_id"]))
    challenge_id = uuid4()
    otp_code = "123456"

    response = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-mismatch-step-up-confirm-idem",
            "X-Correlation-ID": "phone-change-mismatch-step-up-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "phone_change_step_up_invalid"
    assert error["reason"] == "phone_change_step_up_invalid"


def test_phone_change_replayed_confirmation_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPhoneChangeStore,
    ],
) -> None:
    client, registration_store, phone_verification_store, _ = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="phone-change-replay-confirm@example.com",
        phone_number="+254733510015",
        correlation_id="phone-change-replay-confirm-register-corr",
    )
    request_payload = _create_phone_change_request(
        client=client,
        user_id=user_id,
        idempotency_key="phone-change-replay-confirm-request-idem",
        correlation_id="phone-change-replay-confirm-request-corr",
        new_phone_number="+254733510116",
        current_password="StrongPassw0rd!",
    )
    request_id = UUID(cast(str, request_payload["request_id"]))
    challenge_id = UUID(cast(str, request_payload["step_up_challenge_id"]))
    otp_code = phone_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    first_confirm = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-replay-confirm-first-idem",
            "X-Correlation-ID": "phone-change-replay-confirm-first-corr",
        },
        json={
            "request_id": str(request_id),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    assert first_confirm.status_code == 200

    replay_confirm = client.post(
        "/v1/auth/phone-change/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "phone-change-replay-confirm-second-idem",
            "X-Correlation-ID": "phone-change-replay-confirm-second-corr",
        },
        json={
            "request_id": str(request_id),
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    error = _extract_error_detail(replay_confirm)
    assert replay_confirm.status_code == 409
    assert error["error_code"] == "phone_change_request_already_confirmed"
    assert error["reason"] == "phone_change_request_already_confirmed"


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
    correlation_id: str,
) -> UUID:
    registration_response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": correlation_id},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": f"A{uuid4().int % 1_000_000_000:09d}Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    registration_payload = _response_json(registration_response)
    assert registration_response.status_code == 201
    user_id = UUID(cast(str, registration_payload["user_id"]))
    registration_store.mark_user_phone_verified(
        user_id=user_id,
        verified_at="2026-03-31T12:00:00Z",
    )
    return user_id


def _create_phone_change_request(
    *,
    client: TestClient,
    user_id: UUID,
    idempotency_key: str,
    correlation_id: str,
    new_phone_number: str,
    current_password: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/phone-change/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        },
        json={
            "new_phone_number": new_phone_number,
            "current_password": current_password,
        },
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
