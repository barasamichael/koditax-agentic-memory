"""Regression tests for deterministic account-deletion incident and dispute paths."""

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
from services.auth.app.account_deletion import InMemoryAccountDeletionRequestStore


@pytest.fixture()
def client_and_stores() -> (
    Iterator[tuple[TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore]]
):
    """Create deterministic auth app context for deletion incident path assertions."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    account_deletion_store = InMemoryAccountDeletionRequestStore()
    app.state.registration_store = registration_store
    app.state.account_deletion_request_store = account_deletion_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, account_deletion_store
    reset_default_registration_store()


def test_account_deletion_incident_legitimate_lifecycle_remains_valid(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion.incident.legitimate@example.com",
        phone_number="+254733260001",
        correlation_id="deletion-incident-legitimate-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-incident-legitimate-request-idem",
        correlation_id="deletion-incident-legitimate-request-corr",
    )
    _confirm_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_id=request_id,
        idempotency_key="deletion-incident-legitimate-confirm-idem",
        correlation_id="deletion-incident-legitimate-confirm-corr",
    )

    cancel_response = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-incident-legitimate-cancel-idem",
            "X-Correlation-ID": "deletion-incident-legitimate-cancel-corr",
        },
        json={"request_id": str(request_id)},
    )
    cancel_payload = _response_json(cancel_response)
    assert cancel_response.status_code == 200
    assert cancel_payload["status"] == "cancelled"
    assert cancel_payload["deletion_state"] == "cancelled"


def test_account_deletion_incident_takeover_attempt_is_blocked_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    owner_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion.incident.owner@example.com",
        phone_number="+254733260002",
        correlation_id="deletion-incident-owner-register-corr",
    )
    attacker_user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion.incident.attacker@example.com",
        phone_number="+254733260003",
        correlation_id="deletion-incident-attacker-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=owner_user_id,
        idempotency_key="deletion-incident-owner-request-idem",
        correlation_id="deletion-incident-owner-request-corr",
    )

    headers = {
        "Authorization": _auth_header(user_id=attacker_user_id),
        "Idempotency-Key": "deletion-incident-takeover-idem",
        "X-Correlation-ID": "deletion-incident-takeover-corr",
    }
    payload = {"request_id": str(request_id)}
    first = client.post("/v1/auth/account-deletion/cancel", headers=headers, json=payload)
    second = client.post("/v1/auth/account-deletion/cancel", headers=headers, json=payload)
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)

    assert first.status_code == 404
    assert second.status_code == 404
    assert first_error["error_code"] == "account_deletion_cancel_request_not_found"
    assert first_error["reason"] == "account_deletion_cancel_request_not_found"
    assert first_error["incident_code"] == "account_deletion_malicious_takeover_attempt"
    assert first_error["account_deletion_state"] == "not_owned"
    assert isinstance(first_error["audit_reference_id"], str)
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)
    incident_records = account_deletion_store.get_incident_records_for_user(
        user_id=attacker_user_id
    )
    assert len(incident_records) == 1
    assert incident_records[0].incident_code == "account_deletion_malicious_takeover_attempt"


def test_account_deletion_legal_hold_dispute_cannot_force_unsafe_deletion(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion.incident.legalhold@example.com",
        phone_number="+254733260004",
        correlation_id="deletion-incident-legalhold-register-corr",
    )
    account_deletion_store.set_test_precheck_context(
        user_id=user_id,
        tenant_id="default_tenant",
        legal_hold=True,
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-incident-legalhold-request-idem",
        correlation_id="deletion-incident-legalhold-request-corr",
    )

    confirm_response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-incident-legalhold-confirm-idem",
            "X-Correlation-ID": "deletion-incident-legalhold-confirm-corr",
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": "reauth:not-used",
            "otp_verification_id": str(uuid4()),
        },
    )
    confirm_error = _extract_error_detail(confirm_response)
    assert confirm_response.status_code == 409
    assert confirm_error["error_code"] == "account_deletion_confirm_invalid_state"
    assert confirm_error["reason"] == "account_deletion_confirm_invalid_state"
    assert confirm_error["incident_code"] == "account_deletion_legal_hold_dispute"
    assert confirm_error["account_deletion_state"] == "blocked"
    assert isinstance(confirm_error["audit_reference_id"], str)

    execute_response = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-incident-legalhold-execute-idem",
            "X-Correlation-ID": "deletion-incident-legalhold-execute-corr",
        },
        json={"request_id": str(request_id)},
    )
    execute_error = _extract_error_detail(execute_response)
    assert execute_response.status_code == 409
    assert execute_error["error_code"] == "account_deletion_execute_invalid_state"
    assert execute_error["reason"] == "account_deletion_execute_invalid_state"
    assert execute_error["incident_code"] == "account_deletion_legal_hold_dispute"
    assert execute_error["account_deletion_state"] == "blocked"
    assert isinstance(execute_error["audit_reference_id"], str)

    persisted_request = account_deletion_store.get_request_by_id(request_id=request_id)
    assert persisted_request is not None
    assert persisted_request.deletion_state == "blocked"


def test_account_deletion_erroneous_path_preserves_reversible_controls_when_allowed(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    user_id = _register_active_user(
        client=client,
        registration_store=registration_store,
        email="deletion.incident.erroneous@example.com",
        phone_number="+254733260005",
        correlation_id="deletion-incident-erroneous-register-corr",
    )
    request_id = _create_deletion_request(
        client=client,
        user_id=user_id,
        idempotency_key="deletion-incident-erroneous-request-idem",
        correlation_id="deletion-incident-erroneous-request-corr",
    )
    _confirm_deletion_request(
        client=client,
        account_deletion_store=account_deletion_store,
        user_id=user_id,
        request_id=request_id,
        idempotency_key="deletion-incident-erroneous-confirm-idem",
        correlation_id="deletion-incident-erroneous-confirm-corr",
    )

    cancel_response = client.post(
        "/v1/auth/account-deletion/cancel",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-incident-erroneous-cancel-idem",
            "X-Correlation-ID": "deletion-incident-erroneous-cancel-corr",
        },
        json={"request_id": str(request_id)},
    )
    assert cancel_response.status_code == 200

    execute_response = client.post(
        "/v1/auth/account-deletion/execute",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-incident-erroneous-execute-idem",
            "X-Correlation-ID": "deletion-incident-erroneous-execute-corr",
        },
        json={"request_id": str(request_id)},
    )
    execute_error = _extract_error_detail(execute_response)
    assert execute_response.status_code == 409
    assert execute_error["error_code"] == "account_deletion_execute_invalid_state"
    assert execute_error["reason"] == "account_deletion_execute_invalid_state"
    assert execute_error["account_deletion_state"] == "cancelled"
    assert isinstance(execute_error["audit_reference_id"], str)


def _register_active_user(
    *,
    client: TestClient,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone_number: str,
    correlation_id: str,
) -> UUID:
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
    user_id = UUID(cast(str, payload["user_id"]))
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-04-01T14:00:00Z",
    )
    return user_id


def _create_deletion_request(
    *,
    client: TestClient,
    user_id: UUID,
    idempotency_key: str,
    correlation_id: str,
) -> UUID:
    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        },
        json={"request_reason": "Deletion incident-path test request."},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return UUID(cast(str, payload["request_id"]))


def _confirm_deletion_request(
    *,
    client: TestClient,
    account_deletion_store: InMemoryAccountDeletionRequestStore,
    user_id: UUID,
    request_id: UUID,
    idempotency_key: str,
    correlation_id: str,
) -> None:
    reauth_proof = account_deletion_store.issue_test_reauth_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    otp_verification_id = account_deletion_store.issue_test_otp_verification_proof(
        user_id=user_id,
        tenant_id="default_tenant",
        request_id=request_id,
    )
    response = client.post(
        "/v1/auth/account-deletion/confirm",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        },
        json={
            "request_id": str(request_id),
            "reauth_proof": reauth_proof,
            "otp_verification_id": str(otp_verification_id),
        },
    )
    assert response.status_code == 200


def _auth_header(*, user_id: UUID) -> str:
    return f"Bearer user_id={user_id};tenant_id=default_tenant;role=IndividualTaxpayer"


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "account_deletion_state" in detail
    assert "audit_reference_id" in detail
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
