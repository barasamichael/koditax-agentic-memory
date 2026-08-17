"""Deterministic tenant/role authorization middleware tests."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from fastapi import APIRouter
from fastapi.testclient import TestClient

from shared.authz.rbac import Principal
from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from shared.authz.rbac import build_authorized_principal_dependency

ROUTER = APIRouter()
require_test_principal = build_authorized_principal_dependency(
    allowed_roles=frozenset({"TaxAgent"}),
    allow_delegation=False,
)


@ROUTER.get("/protected")
def protected_endpoint(
    principal: Annotated[Principal, Depends(require_test_principal)],
) -> dict[str, str]:
    return {
        "user_id": str(principal.user_id),
        "role": principal.role,
        "tenant_id": principal.tenant_id,
    }


def test_allowed_role_and_tenant_are_authorized() -> None:
    client = TestClient(_create_app())
    user_id = uuid4()
    response = client.get(
        "/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=user_id, role="TaxAgent")
        },
    )
    payload = cast(dict[str, object], response.json())
    assert response.status_code == 200
    assert payload["user_id"] == str(user_id)
    assert payload["role"] == "TaxAgent"
    assert payload["tenant_id"] == "default_tenant"


def test_missing_auth_context_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.get("/protected")
    _assert_error_reason(
        response=response, expected_status=401, expected_reason="auth_context_missing"
    )


def test_malformed_auth_context_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.get("/protected", headers={AUTH_CONTEXT_HEADER_NAME: "{bad-json"})
    _assert_error_reason(
        response=response,
        expected_status=401,
        expected_reason="auth_context_malformed",
    )


def test_unsupported_auth_context_scope_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.get(
        "/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=uuid4(),
                role="TaxAgent",
                schema_version="2.0.0",
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="unsupported_auth_context_scope",
    )


def test_disallowed_role_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.get(
        "/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=uuid4(), role="IndividualTaxpayer"
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_role_forbidden",
    )


def test_tenant_mismatch_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.get(
        "/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=uuid4(),
                role="TaxAgent",
                tenant_id="other_tenant",
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_tenant_forbidden",
    )


def test_malformed_delegation_context_is_rejected_deterministically() -> None:
    client = TestClient(_create_app())
    response = client.get(
        "/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(
                {
                    "schema_version": "1.0.0",
                    "user_id": str(uuid4()),
                    "tenant_id": "default_tenant",
                    "role": "TaxAgent",
                    "session_id": "11111111-2222-3333-4444-555555555555",
                    "delegation_context": {"is_delegated": True},
                }
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=401,
        expected_reason="auth_context_invalid_claim",
    )


def test_delegated_access_forbidden_when_policy_disallows_it() -> None:
    client = TestClient(_create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.get(
        "/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
            )
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_delegation_forbidden",
    )


def test_repeated_same_forbidden_input_has_stable_error_shape() -> None:
    client = TestClient(_create_app())
    headers = {
        AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
            user_id=uuid4(), role="IndividualTaxpayer"
        ),
    }
    first = cast(dict[str, object], client.get("/protected", headers=headers).json())["detail"]
    second = cast(dict[str, object], client.get("/protected", headers=headers).json())["detail"]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())


def test_missing_auth_context_does_not_reach_protected_handler() -> None:
    call_count = {"count": 0}
    app = FastAPI()

    @app.get("/protected")
    def protected_endpoint_with_counter(
        principal: Annotated[Principal, Depends(require_test_principal)],
    ) -> dict[str, str]:
        call_count["count"] += 1
        return {
            "user_id": str(principal.user_id),
            "role": principal.role,
            "tenant_id": principal.tenant_id,
        }

    client = TestClient(app)
    response = client.get("/protected")

    _assert_error_reason(
        response=response, expected_status=401, expected_reason="auth_context_missing"
    )
    assert call_count["count"] == 0


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
    schema_version: str = "1.0.0",
    tenant_id: str = "default_tenant",
    is_delegated: bool = False,
    principal_user_id: UUID | None = None,
    delegate_user_id: UUID | None = None,
    delegation_id: UUID | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": schema_version,
            "user_id": str(user_id),
            "tenant_id": tenant_id,
            "role": role,
            "session_id": "11111111-2222-3333-4444-555555555555",
            "delegation_context": {
                "is_delegated": is_delegated,
                "principal_user_id": str(principal_user_id) if principal_user_id else None,
                "delegate_user_id": str(delegate_user_id) if delegate_user_id else None,
                "delegation_id": str(delegation_id) if delegation_id else None,
                "granted_at": None,
                "revoked_at": None,
            },
        }
    )
