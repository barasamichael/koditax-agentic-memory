"""Pilot-acceptance E2E tests for governed Phase 8 auth success journeys."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.auth.app.main import create_app
from services.auth.app.main import list_auth_audit_events
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


@pytest.fixture()
def e2e_client_and_stores() -> (
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
    """Build deterministic E2E auth context with governed in-memory dependencies."""

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
    app.state.login_lockout_store = InMemoryLoginLockoutStore()
    app.state.login_step_up_store = InMemoryLoginStepUpStore()

    with TestClient(app) as client:
        yield client, registration_store, phone_store, password_reset_store, account_deletion_store
    reset_default_registration_store()


def test_e2e_registration_verification_completion_and_audit_linkage(
    e2e_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, _, _ = e2e_client_and_stores
    phone_number = "+254733970001"
    email = f"pilot.acceptance.register.{uuid4()}@example.com"

    register_response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": "e2e-acceptance-register-corr"},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    register_payload = _response_json(register_response)
    assert register_response.status_code == 201
    user_id = UUID(cast(str, register_payload["user_id"]))

    issued = _issue_registration_phone_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key="e2e-acceptance-register-verify-idem",
    )
    challenge_id = UUID(cast(str, issued["challenge_id"]))
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": "e2e-acceptance-register-verify-corr"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    verify_payload = _response_json(verify_response)
    assert verify_response.status_code == 200
    assert verify_payload["status"] == "verified"

    persisted = registration_store.get_user_by_id(user_id=user_id)
    assert persisted is not None
    assert persisted.account_state == "active"
    assert persisted.verification_state == "verified"

    audit_events = list_auth_audit_events(app_instance=cast(FastAPI, client.app))
    emitted_types = [event.event_type for event in audit_events]
    assert "auth_registration_requested" in emitted_types
    assert "auth_otp_challenge_issued" in emitted_types
    assert "auth_otp_challenge_verified" in emitted_types


def test_e2e_login_mfa_logout_and_session_revocation_flow(
    e2e_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, _, _ = e2e_client_and_stores
    user = _register_and_verify_user(
        client=client,
        registration_store=registration_store,
        phone_store=phone_store,
        email=f"pilot.acceptance.login.{uuid4()}@example.com",
        phone_number="+254733970002",
    )

    pending = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "e2e-acceptance-login-pending-corr",
            "X-Forwarded-For": "203.0.113.230",
        },
        json={"login_id": user.phone_number_normalized, "password": "StrongPassw0rd!"},
    )
    pending_payload = _response_json(pending)
    assert pending.status_code == 200
    assert pending_payload["status"] == "pending_step_up"
    assert pending_payload["step_up_required"] is True
    assert "access_token" not in pending_payload
    assert "refresh_token" not in pending_payload

    challenge_id = UUID(cast(str, pending_payload["step_up_challenge_id"]))
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    authenticated = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "e2e-acceptance-login-final-corr",
            "X-Forwarded-For": "203.0.113.230",
        },
        json={
            "login_id": user.phone_number_normalized,
            "password": "StrongPassw0rd!",
            "step_up_challenge_id": str(challenge_id),
            "step_up_otp_code": otp_code,
        },
    )
    authenticated_payload = _response_json(authenticated)
    assert authenticated.status_code == 200
    assert authenticated_payload["status"] == "authenticated"
    refresh_token = cast(str, authenticated_payload["refresh_token"])
    session_id = cast(dict[str, Any], authenticated_payload["session"])["session_id"]

    logout = client.post(
        "/v1/auth/logout",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "X-Correlation-ID": "e2e-acceptance-logout-corr",
        },
        json={"revoke_scope": "single_session", "target_session_id": session_id},
    )
    logout_payload = _response_json(logout)
    assert logout.status_code == 200
    assert logout_payload["status"] == "revoked"
    assert logout_payload["revoke_scope"] == "single_session"
    assert logout_payload["revoked_session_count"] == 1

    refresh_after_logout = client.post(
        "/v1/auth/refresh",
        headers={"X-Correlation-ID": "e2e-acceptance-refresh-after-logout-corr"},
        json={"refresh_token": refresh_token},
    )
    revoked_error = _extract_error_detail(refresh_after_logout)
    assert refresh_after_logout.status_code == 401
    assert revoked_error["error_code"] == "refresh_token_session_revoked"
    assert revoked_error["reason"] == "refresh_token_session_revoked"


def test_e2e_password_reset_completion_and_replay_rejection(
    e2e_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, password_reset_store, _ = e2e_client_and_stores
    user = _register_and_verify_user(
        client=client,
        registration_store=registration_store,
        phone_store=phone_store,
        email=f"pilot.acceptance.reset.{uuid4()}@example.com",
        phone_number="+254733970003",
    )

    initiated = client.post(
        "/v1/auth/password-reset/initiate",
        headers={
            "Idempotency-Key": "e2e-acceptance-reset-initiate-idem",
            "X-Correlation-ID": "e2e-acceptance-reset-initiate-corr",
        },
        json={"purpose": "password_reset", "channel": "email", "email": user.email_normalized},
    )
    initiated_payload = _response_json(initiated)
    assert initiated.status_code == 201
    challenge_id = UUID(cast(str, initiated_payload["challenge_id"]))
    reset_code = password_reset_store.get_reset_code_for_challenge(challenge_id=challenge_id)

    confirm_response = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-acceptance-reset-confirm-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "N3wStrongPassw0rd!",
        },
    )
    confirm_payload = _response_json(confirm_response)
    assert confirm_response.status_code == 200
    assert confirm_payload["status"] == "password_updated"

    replay_first = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-acceptance-reset-replay-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
            "new_password": "AnotherStrongPassw0rd!",
        },
    )
    replay_second = client.post(
        "/v1/auth/password-reset/confirm",
        headers={"X-Correlation-ID": "e2e-acceptance-reset-replay-corr"},
        json={
            "challenge_id": str(challenge_id),
            "reset_code": reset_code,
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

    login_with_new_password = client.post(
        "/v1/auth/login",
        headers={
            "X-Correlation-ID": "e2e-acceptance-reset-login-corr",
            "X-Forwarded-For": "203.0.113.231",
        },
        json={"login_id": user.phone_number_normalized, "password": "N3wStrongPassw0rd!"},
    )
    login_payload = _response_json(login_with_new_password)
    assert login_with_new_password.status_code == 200
    assert login_payload["status"] == "pending_step_up"


def test_e2e_account_deletion_request_confirm_and_cooldown_guard(
    e2e_client_and_stores: tuple[
        TestClient,
        InMemoryRegistrationStore,
        InMemoryPhoneVerificationStore,
        InMemoryPasswordResetStore,
        InMemoryAccountDeletionRequestStore,
    ],
) -> None:
    client, registration_store, phone_store, _, account_deletion_store = e2e_client_and_stores
    user = _register_and_verify_user(
        client=client,
        registration_store=registration_store,
        phone_store=phone_store,
        email=f"pilot.acceptance.deletion.{uuid4()}@example.com",
        phone_number="+254733970004",
    )

    request_response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "Idempotency-Key": "e2e-acceptance-deletion-request-idem",
            "X-Correlation-ID": "e2e-acceptance-deletion-request-corr",
        },
        json={"request_reason": "Pilot acceptance account deletion path."},
    )
    request_payload = _response_json(request_response)
    assert request_response.status_code == 201
    assert request_payload["deletion_state"] == "requested"
    request_id = UUID(cast(str, request_payload["request_id"]))

    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user.user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user.user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    confirm_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "Idempotency-Key": "e2e-acceptance-deletion-confirm-idem",
            "X-Correlation-ID": "e2e-acceptance-deletion-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )
    confirm_payload = _response_json(confirm_response)
    assert confirm_response.status_code == 200
    assert confirm_payload["deletion_state"] == "cooldown_active"

    execute_before_cooldown = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user.user_id),
            "Idempotency-Key": "e2e-acceptance-deletion-execute-before-cooldown-idem",
            "X-Correlation-ID": "e2e-acceptance-deletion-execute-before-cooldown-corr",
        },
        json={"request_id": str(request_id)},
    )
    execute_error = _extract_error_detail(execute_before_cooldown)
    assert execute_before_cooldown.status_code == 409
    assert execute_error["error_code"] == "account_deletion_execute_not_allowed"
    assert execute_error["reason"] == "account_deletion_execute_not_allowed"
    assert execute_error["account_deletion_state"] == "confirmed"
    assert isinstance(execute_error["audit_reference_id"], str)

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user.user_id)
    assert len(audit_events) >= 2
    assert audit_events[0].event_type == "account_deletion_request_created"
    assert audit_events[1].event_type == "account_deletion_request_confirmed"


def _register_and_verify_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    phone_store: InMemoryPhoneVerificationStore,
    email: str,
    phone_number: str,
) -> RegisteredUserRecord:
    register_response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": f"e2e-register-{uuid4()}"},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    register_payload = _response_json(register_response)
    assert register_response.status_code == 201
    user_id = UUID(cast(str, register_payload["user_id"]))

    issued = _issue_registration_phone_challenge(
        client=client,
        phone_number=phone_number,
        idempotency_key=f"e2e-register-verify-{uuid4()}",
    )
    challenge_id = UUID(cast(str, issued["challenge_id"]))
    otp_code = phone_store.get_otp_code_for_challenge(challenge_id=challenge_id)
    verify_response = client.post(
        "/v1/auth/otp/verify",
        headers={"X-Correlation-ID": f"e2e-register-verify-{uuid4()}"},
        json={"challenge_id": str(challenge_id), "otp_code": otp_code},
    )
    assert verify_response.status_code == 200

    user = registration_store.get_user_by_id(user_id=user_id)
    assert user is not None
    assert user.account_state == "active"
    return user


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
            "X-Correlation-ID": f"e2e-otp-issue-{uuid4()}",
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
