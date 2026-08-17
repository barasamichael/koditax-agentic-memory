"""Delegation constraint tests for shared authorization middleware."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from typing import Annotated
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import Depends
from fastapi import FastAPI
from fastapi import APIRouter
from fastapi.testclient import TestClient

from shared.authz.rbac import Principal
from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from shared.authz.rbac import build_authorized_principal_dependency

ROUTER = APIRouter()
require_delegated_principal = build_authorized_principal_dependency(
    allowed_roles=frozenset({"IndividualTaxpayer", "TaxAgent", "Accountant"}),
    allowed_delegated_roles=frozenset({"TaxAgent", "Accountant"}),
    allow_delegation=True,
)


@ROUTER.post("/delegated/action")
def delegated_action_endpoint(
    principal: Annotated[Principal, Depends(require_delegated_principal)],
) -> dict[str, object]:
    return {
        "ok": True,
        "user_id": str(principal.user_id),
        "is_delegated": principal.delegation_context.is_delegated,
    }


def test_valid_active_delegation_allows_policy_approved_action() -> None:
    client = TestClient(_create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
    )
    payload = cast(dict[str, object], response.json())
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["is_delegated"] is True
    assert payload["user_id"] == str(delegate_user_id)


def test_missing_delegation_context_fields_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    delegate_user_id = uuid4()
    response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=None,
                delegate_user_id=None,
                delegation_id=None,
                granted_at=None,
            )
        },
    )
    _assert_error_reason(
        response=response, expected_status=403, expected_reason="delegation_context_missing"
    )


def test_malformed_delegation_context_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=uuid4(),
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=uuid4(),
                delegate_user_id=uuid4(),
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
    )
    _assert_error_reason(
        response=response, expected_status=403, expected_reason="delegation_context_invalid"
    )


def test_revoked_or_inactive_delegation_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    revoked_response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z"),
                revoked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        },
    )
    _assert_error_reason(
        response=revoked_response, expected_status=403, expected_reason="delegation_revoked"
    )

    inactive_response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) + timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
    )
    _assert_error_reason(
        response=inactive_response,
        expected_status=403,
        expected_reason="delegation_not_active",
    )


def test_delegation_tenant_mismatch_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                tenant_id="other_tenant",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="delegation_tenant_mismatch",
    )


def test_delegated_role_outside_allowed_scope_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="IndividualTaxpayer",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="delegation_role_forbidden",
    )


def test_repeated_same_invalid_delegation_class_is_deterministic() -> None:
    client = TestClient(_create_app())
    delegate_user_id = uuid4()
    headers = {
        AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
            user_id=delegate_user_id,
            role="TaxAgent",
            is_delegated=True,
            principal_user_id=None,
            delegate_user_id=None,
            delegation_id=None,
            granted_at=None,
        )
    }
    first = cast(dict[str, object], client.post("/delegated/action", headers=headers).json())[
        "detail"
    ]
    second = cast(dict[str, object], client.post("/delegated/action", headers=headers).json())[
        "detail"
    ]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())


def test_delegated_request_with_unsupported_scope_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/delegated/action",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(
                {
                    "schema_version": "2.0.0",
                    "user_id": str(delegate_user_id),
                    "tenant_id": "default_tenant",
                    "role": "TaxAgent",
                    "session_id": "11111111-2222-3333-4444-555555555555",
                    "delegation_context": {
                        "is_delegated": True,
                        "principal_user_id": str(principal_user_id),
                        "delegate_user_id": str(delegate_user_id),
                        "delegation_id": str(uuid4()),
                        "granted_at": (datetime.now(UTC) - timedelta(minutes=10))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "revoked_at": None,
                    },
                }
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="unsupported_auth_context_scope",
    )


def _create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ROUTER)
    return app


def _assert_error_reason(
    *,
    response: object,
    expected_status: int,
    expected_reason: str,
) -> None:
    status_code = cast(int, response.status_code)
    payload = cast(dict[str, object], cast(object, response).json())
    detail = cast(dict[str, object], payload["detail"])
    assert status_code == expected_status
    assert detail["error_code"] == expected_reason
    assert detail["reason"] == expected_reason
    assert "trace_id" in detail
    assert "correlation_id" in detail


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
