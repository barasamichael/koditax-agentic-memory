"""Runtime tests for deterministic auth account-state machine enforcement."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import register_user
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.registration import parse_registration_request
from services.auth.app.registration import reset_default_registration_store
from services.auth.app.account_lifecycle import AccountStateError
from services.auth.app.account_lifecycle import require_account_action_allowed
from services.auth.app.account_lifecycle import ensure_state_transition_allowed
from services.auth.app.email_verification import InMemoryEmailVerificationStore


@pytest.fixture()
def client_and_stores() -> (
    Iterator[tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore]]
):
    """Create isolated auth app client with deterministic registration/email stores."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    email_verification_store = InMemoryEmailVerificationStore()
    app.state.registration_store = registration_store
    app.state.email_verification_store = email_verification_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, email_verification_store
    reset_default_registration_store()


def test_pending_verification_to_active_succeeds_on_valid_verification_path(
    client_and_stores: tuple[TestClient, InMemoryRegistrationStore, InMemoryEmailVerificationStore],
) -> None:
    client, registration_store, email_verification_store = client_and_stores
    email = "state-machine.user@example.com"
    _register_user(client=client, email=email)

    challenge_response = client.post(
        "/v1/auth/otp/challenges",
        headers={
            "X-Correlation-ID": "state-machine-challenge-corr",
            "Idempotency-Key": "state-machine-challenge-idem",
        },
        json={"purpose": "registration_verify", "channel": "email", "email": email},
    )
    challenge_payload = _response_json(challenge_response)
    challenge_id = UUID(cast(str, challenge_payload["challenge_id"]))
    otp_code = email_verification_store.get_otp_code_for_challenge(challenge_id=challenge_id)

    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "state-machine-verify-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    verify_payload = _response_json(verify_response)
    assert verify_response.status_code == 200
    assert verify_payload["status"] == "verified"

    registered_user = registration_store.get_user_by_email(email_normalized=email.lower())
    assert registered_user is not None
    assert registered_user.account_state == "active"
    assert registered_user.verification_state == "verified"


def test_allowed_lock_and_disable_transition_paths_are_deterministic() -> None:
    store = InMemoryRegistrationStore()
    created = register_user(
        request_record=parse_registration_request(
            {
                "email": "state-transition-path@example.com",
                "phone_number": "+254799330001",
                "kra_pin": "A123456789Z",
                "password": "StrongPassw0rd!",
                "role": "IndividualTaxpayer",
            }
        ),
        registration_store=store,
    )
    user_id = created.user_id
    active_user = store.mark_user_email_verified(
        user_id=user_id, verified_at="2026-03-28T10:00:00Z"
    )
    locked_user = store.lock_user(user_id=user_id)
    disabled_user = store.disable_user(user_id=user_id)

    assert active_user.account_state == "active"
    assert locked_user.account_state == "locked"
    assert disabled_user.account_state == "disabled"


def test_forbidden_direct_transition_is_rejected_with_canonical_fields() -> None:
    error = _capture_transition_error(
        current_state="pending_verification", requested_state="locked"
    )
    assert error["error_code"] == "account_state_transition_not_allowed"
    assert error["message"] == "Requested account-state transition is not allowed."
    assert error["reason"] == "account_state_transition_not_allowed"
    assert error["current_state"] == "pending_verification"
    assert error["requested_state"] == "locked"


def test_locked_or_disabled_account_actions_are_blocked_deterministically() -> None:
    locked_error = _capture_action_error(action="verify_email", current_state="locked")
    disabled_error = _capture_action_error(action="verify_phone", current_state="disabled")
    pending_error = _capture_action_error(
        action="auth_access", current_state="pending_verification"
    )

    assert locked_error["reason"] == "account_state_action_forbidden"
    assert locked_error["current_state"] == "locked"
    assert locked_error["requested_state"] == "locked"
    assert disabled_error["reason"] == "account_state_action_forbidden"
    assert disabled_error["current_state"] == "disabled"
    assert disabled_error["requested_state"] == "disabled"
    assert pending_error["reason"] == "account_verification_required"
    assert pending_error["requested_state"] == "active"


def test_repeated_illegal_transition_yields_identical_error_payload() -> None:
    first = _capture_transition_error(
        current_state="pending_verification", requested_state="locked"
    )
    second = _capture_transition_error(
        current_state="pending_verification", requested_state="locked"
    )
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def _capture_transition_error(*, current_state: str, requested_state: str) -> dict[str, str]:
    try:
        ensure_state_transition_allowed(
            current_state=cast(Any, current_state),
            requested_state=cast(Any, requested_state),
        )
    except AccountStateError as error:
        return {
            "error_code": error.error_code,
            "message": error.message,
            "reason": error.reason,
            "current_state": error.current_state,
            "requested_state": error.requested_state,
        }
    raise AssertionError("Expected AccountStateError was not raised.")


def _capture_action_error(*, action: str, current_state: str) -> dict[str, str]:
    try:
        require_account_action_allowed(
            action=cast(Any, action),
            current_state=cast(Any, current_state),
        )
    except AccountStateError as error:
        return {
            "error_code": error.error_code,
            "message": error.message,
            "reason": error.reason,
            "current_state": error.current_state,
            "requested_state": error.requested_state,
        }
    raise AssertionError("Expected AccountStateError was not raised.")


def _register_user(*, client: TestClient, email: str) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "state-machine-registration-corr"},
        json={
            "email": email,
            "phone_number": "+254799330002",
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
