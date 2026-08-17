"""Runtime tests for deterministic self-service account deletion request creation."""

from __future__ import annotations

from uuid import UUID
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
    """Create isolated auth app client with deterministic account deletion request store."""

    reset_default_registration_store()
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    account_deletion_store = InMemoryAccountDeletionRequestStore()
    app.state.registration_store = registration_store
    app.state.account_deletion_request_store = account_deletion_store
    with TestClient(app) as test_client:
        yield test_client, registration_store, account_deletion_store
    reset_default_registration_store()


def test_account_deletion_request_positive_for_eligible_authenticated_user(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    registered = _register_user(
        client=client,
        email="deletion.positive@example.com",
        phone_number="+254733110001",
        correlation_id="deletion-positive-register-corr",
    )
    user_id = UUID(cast(str, registered["user_id"]))
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-28T10:00:00Z",
    )

    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-positive-idem",
            "X-Correlation-ID": "deletion-positive-request-corr",
        },
        json={"request_reason": "User requested self-service account closure."},
    )

    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["status"] == "accepted"
    assert payload["deletion_state"] == "requested"
    request_id = UUID(cast(str, payload["request_id"]))

    active_request = account_deletion_store.get_active_request_for_user(user_id=user_id)
    assert active_request is not None
    assert active_request.request_id == request_id
    assert active_request.deletion_state == "requested"

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 1
    assert audit_events[0].action == "account_deletion_request_created"
    assert audit_events[0].action_status == "created"


def test_account_deletion_request_idempotent_replay_and_active_conflict_are_deterministic(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    registered = _register_user(
        client=client,
        email="deletion.idempotent@example.com",
        phone_number="+254733110002",
        correlation_id="deletion-idempotent-register-corr",
    )
    user_id = UUID(cast(str, registered["user_id"]))
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-28T10:01:00Z",
    )

    headers = {
        "Authorization": _auth_header(user_id=user_id),
        "Idempotency-Key": "deletion-idempotent-request-key",
        "X-Correlation-ID": "deletion-idempotent-request-corr",
    }
    body = {"request_reason": "User requested deletion for account cleanup."}
    first = client.post("/v1/auth/account-deletion/requests", headers=headers, json=body)
    second = client.post("/v1/auth/account-deletion/requests", headers=headers, json=body)

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)

    audit_events = account_deletion_store.get_audit_events_for_user(user_id=user_id)
    assert len(audit_events) == 1

    conflict = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-active-conflict-key",
            "X-Correlation-ID": "deletion-active-conflict-corr",
        },
        json={"request_reason": "User requested deletion for account cleanup."},
    )
    conflict_error = _extract_error_detail(conflict)
    assert conflict.status_code == 409
    assert conflict_error["error_code"] == "account_deletion_request_already_active"
    assert conflict_error["message"] == "An active account deletion request already exists."
    assert conflict_error["reason"] == "account_deletion_request_already_active"


def test_account_deletion_request_unauthorized_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, _, _ = client_and_stores
    headers = {
        "Idempotency-Key": "deletion-unauthorized-idem",
        "X-Correlation-ID": "deletion-unauthorized-corr",
    }
    body = {"request_reason": "Unauthorized caller test case."}
    first = client.post("/v1/auth/account-deletion/requests", headers=headers, json=body)
    second = client.post("/v1/auth/account-deletion/requests", headers=headers, json=body)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 401
    assert second.status_code == 401
    assert first_error["error_code"] == "account_deletion_request_unauthorized"
    assert (
        first_error["message"]
        == "Authentication is required for account deletion request creation."
    )
    assert first_error["reason"] == "account_deletion_request_unauthorized"
    assert canonical_json_dumps(second_error) == canonical_json_dumps(first_error)


def test_account_deletion_request_ineligible_state_is_rejected_deterministically(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, _, _ = client_and_stores
    registered = _register_user(
        client=client,
        email="deletion-ineligible@example.com",
        phone_number="+254733110003",
        correlation_id="deletion-ineligible-register-corr",
    )
    user_id = UUID(cast(str, registered["user_id"]))

    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-ineligible-idem",
            "X-Correlation-ID": "deletion-ineligible-request-corr",
        },
        json={"request_reason": "User requested deletion before activation."},
    )
    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "account_deletion_request_ineligible_state"
    assert error["message"] == "Account deletion request is not allowed for current account state."
    assert error["reason"] == "account_deletion_request_ineligible_state"
    assert error["current_state"] == "pending_verification"
    assert error["requested_state"] == "requested"


def test_account_deletion_request_has_no_execution_side_effects(
    client_and_stores: tuple[
        TestClient, InMemoryRegistrationStore, InMemoryAccountDeletionRequestStore
    ],
) -> None:
    client, registration_store, account_deletion_store = client_and_stores
    registered = _register_user(
        client=client,
        email="deletion-guardrail@example.com",
        phone_number="+254733110004",
        correlation_id="deletion-guardrail-register-corr",
    )
    user_id = UUID(cast(str, registered["user_id"]))
    registration_store.mark_user_email_verified(
        user_id=user_id,
        verified_at="2026-03-28T10:02:00Z",
    )

    response = client.post(
        "/v1/auth/account-deletion/requests",
        headers={
            "Authorization": _auth_header(user_id=user_id),
            "Idempotency-Key": "deletion-guardrail-idem",
            "X-Correlation-ID": "deletion-guardrail-request-corr",
        },
        json={"request_reason": "Guardrail check for no execution side effects."},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["deletion_state"] == "requested"

    persisted_user = registration_store.get_user_by_id(user_id=user_id)
    assert persisted_user is not None
    assert persisted_user.account_state == "active"

    active_request = account_deletion_store.get_active_request_for_user(user_id=user_id)
    assert active_request is not None
    assert active_request.deletion_state == "requested"


def _auth_header(*, user_id: UUID) -> str:
    return f"Bearer user_id={user_id};tenant_id=default_tenant;role=IndividualTaxpayer"


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
            "kra_pin": _kra_pin_for_phone(phone_number),
            "password": "StrongPassw0rd!",
            "role": "IndividualTaxpayer",
        },
    )
    payload = _response_json(response)
    assert response.status_code == 201
    return payload


def _kra_pin_for_phone(phone_number: str) -> str:
    digits_only = "".join(ch for ch in phone_number if ch.isdigit())
    serial = digits_only[-9:].rjust(9, "0")
    return f"A{serial}B"


def _extract_error_detail(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
