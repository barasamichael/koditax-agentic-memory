"""Deterministic RBAC governance tests for auth role-change operations."""

from __future__ import annotations

import json
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
from services.auth.app.main import reset_auth_audit_events
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import get_default_registration_store
from services.auth.app.registration import reset_default_registration_store


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """Create isolated auth app client with deterministic registration/audit state."""

    reset_default_registration_store()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    reset_auth_audit_events(app_instance=app)
    reset_default_registration_store()


def test_admin_role_change_succeeds_and_emits_immutable_audit_record(client: TestClient) -> None:
    admin_user_id = _register_user(
        client=client,
        email="admin.role.change@example.com",
        role="Administrator",
    )
    target_user_id = _register_user(
        client=client, email="target.role.change@example.com", role="IndividualTaxpayer"
    )

    response = client.post(
        "/v1/auth/roles/change",
        headers={
            "X-Auth-Context": _build_auth_context_header(
                user_id=admin_user_id,
                role="Administrator",
            ),
            "X-Correlation-ID": "role-change-success-corr",
        },
        json={
            "target_user_id": str(target_user_id),
            "new_role": "TaxAgent",
            "reason": "support_escalation",
        },
    )
    payload = _response_json(response)

    assert response.status_code == 200
    assert payload["status"] == "role_updated"
    assert payload["target_user_id"] == str(target_user_id)
    assert payload["previous_role"] == "IndividualTaxpayer"
    assert payload["new_role"] == "TaxAgent"
    assert payload["changed_by_user_id"] == str(admin_user_id)
    assert isinstance(payload["changed_at"], str)

    updated = get_default_registration_store().get_user_by_id(user_id=target_user_id)
    assert updated is not None
    assert updated.role == "TaxAgent"

    events = list_auth_audit_events(app_instance=cast(FastAPI, client.app))
    role_events = [event for event in events if event.event_type == "auth_role_change_succeeded"]
    assert role_events
    latest = role_events[-1]
    assert latest.action_status == "succeeded"
    assert latest.reason_code is None
    assert latest.details["actor_user_id"] == str(admin_user_id)
    assert latest.details["target_user_id"] == str(target_user_id)
    assert latest.details["previous_role"] == "IndividualTaxpayer"
    assert latest.details["new_role"] == "TaxAgent"


def test_unauthorized_non_admin_role_change_is_rejected_deterministically(
    client: TestClient,
) -> None:
    actor_user_id = _register_user(
        client=client,
        email="tax.agent.actor@example.com",
        role="TaxAgent",
    )
    target_user_id = _register_user(
        client=client, email="tax.agent.target@example.com", role="IndividualTaxpayer"
    )

    response = client.post(
        "/v1/auth/roles/change",
        headers={
            "X-Auth-Context": _build_auth_context_header(
                user_id=actor_user_id,
                role="TaxAgent",
            ),
            "X-Correlation-ID": "role-change-non-admin-corr",
        },
        json={"target_user_id": str(target_user_id), "new_role": "Accountant"},
    )
    error = _extract_error(response)
    assert response.status_code == 403
    assert error["error_code"] == "authorization_role_forbidden"
    assert error["reason"] == "authorization_role_forbidden"


def test_self_escalation_is_rejected_and_audited_deterministically(client: TestClient) -> None:
    admin_user_id = _register_user(
        client=client,
        email="self.escalation.admin@example.com",
        role="Administrator",
    )

    response = client.post(
        "/v1/auth/roles/change",
        headers={
            "X-Auth-Context": _build_auth_context_header(
                user_id=admin_user_id,
                role="Administrator",
            ),
            "X-Correlation-ID": "role-change-self-corr",
        },
        json={"target_user_id": str(admin_user_id), "new_role": "TaxAgent"},
    )
    error = _extract_error(response)
    assert response.status_code == 403
    assert error["error_code"] == "role_change_self_escalation_forbidden"
    assert error["reason"] == "role_change_self_escalation_forbidden"

    rejected = [
        event
        for event in list_auth_audit_events(app_instance=cast(FastAPI, client.app))
        if event.event_type == "auth_role_change_rejected"
    ]
    assert rejected
    assert rejected[-1].reason_code == "role_change_self_escalation_forbidden"


def test_invalid_transition_and_cross_tenant_are_rejected_deterministically(
    client: TestClient,
) -> None:
    admin_user_id = _register_user(
        client=client,
        email="admin.invalid.transition@example.com",
        role="Administrator",
    )
    target_user_id = _register_user(
        client=client,
        email="target.invalid.transition@example.com",
        role="Accountant",
    )

    same_role_response = client.post(
        "/v1/auth/roles/change",
        headers={
            "X-Auth-Context": _build_auth_context_header(
                user_id=admin_user_id,
                role="Administrator",
            ),
            "X-Correlation-ID": "role-change-invalid-transition-corr",
        },
        json={"target_user_id": str(target_user_id), "new_role": "Accountant"},
    )
    same_role_error = _extract_error(same_role_response)
    assert same_role_response.status_code == 409
    assert same_role_error["error_code"] == "role_change_invalid_transition"
    assert same_role_error["reason"] == "role_change_invalid_transition"

    tenant_mismatch_response = client.post(
        "/v1/auth/roles/change",
        headers={
            "X-Auth-Context": _build_auth_context_header(
                user_id=admin_user_id,
                role="Administrator",
                tenant_id="other_tenant",
            ),
            "X-Correlation-ID": "role-change-tenant-corr",
        },
        json={"target_user_id": str(target_user_id), "new_role": "TaxAgent"},
    )
    tenant_mismatch_error = _extract_error(tenant_mismatch_response)
    assert tenant_mismatch_response.status_code == 403
    assert tenant_mismatch_error["error_code"] == "authorization_tenant_forbidden"
    assert tenant_mismatch_error["reason"] == "authorization_tenant_forbidden"


def test_repeated_same_invalid_request_has_stable_reason_and_shape(client: TestClient) -> None:
    admin_user_id = _register_user(
        client=client,
        email="repeat.admin@example.com",
        role="Administrator",
    )
    target_user_id = _register_user(
        client=client,
        email="repeat.target@example.com",
        role="IndividualTaxpayer",
    )

    headers = {
        "X-Auth-Context": _build_auth_context_header(
            user_id=admin_user_id,
            role="Administrator",
            is_delegated=True,
            principal_user_id=uuid4(),
            delegate_user_id=admin_user_id,
            delegation_id=uuid4(),
            granted_at="2026-04-10T10:00:00Z",
        ),
        "X-Correlation-ID": "role-change-repeat-corr",
    }
    payload = {"target_user_id": str(target_user_id), "new_role": "TaxAgent"}

    first = _extract_error(client.post("/v1/auth/roles/change", headers=headers, json=payload))
    second = _extract_error(client.post("/v1/auth/roles/change", headers=headers, json=payload))
    assert first["error_code"] == "authorization_delegation_forbidden"
    assert second["error_code"] == "authorization_delegation_forbidden"
    assert first["reason"] == second["reason"]
    assert set(first.keys()) == set(second.keys())
    assert canonical_json_dumps(first) == canonical_json_dumps(second)


def _register_user(*, client: TestClient, email: str, role: str) -> UUID:
    checksum = sum(ord(character) for character in f"{email}:{role}")
    phone_number = f"+2547{checksum % 100_000_000:08d}"
    response = client.post(
        "/v1/auth/register",
        headers={"X-Correlation-ID": f"register-{email}"},
        json={
            "email": email,
            "phone_number": phone_number,
            "kra_pin": "A123456789Z",
            "password": "StrongPassw0rd!",
            "role": role,
        },
    )
    assert response.status_code == 201
    payload = _response_json(response)
    return UUID(cast(str, payload["user_id"]))


def _build_auth_context_header(
    *,
    user_id: UUID,
    role: str,
    tenant_id: str = "default_tenant",
    is_delegated: bool = False,
    principal_user_id: UUID | None = None,
    delegate_user_id: UUID | None = None,
    delegation_id: UUID | None = None,
    granted_at: str | None = None,
    revoked_at: str | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": str(user_id),
            "tenant_id": tenant_id,
            "role": role,
            "session_id": "11111111-2222-3333-4444-555555555555",
            "delegation_context": {
                "is_delegated": is_delegated,
                "principal_user_id": str(principal_user_id) if principal_user_id else None,
                "delegate_user_id": str(delegate_user_id) if delegate_user_id else None,
                "delegation_id": str(delegation_id) if delegation_id else None,
                "granted_at": granted_at,
                "revoked_at": revoked_at,
            },
        }
    )


def _extract_error(response: object) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _response_json(response: object) -> dict[str, Any]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
