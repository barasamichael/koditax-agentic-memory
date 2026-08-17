"""Tax-core authorization boundary tests for canonical auth context enforcement."""

from __future__ import annotations

import json
from uuid import uuid4
from typing import cast

from fastapi.testclient import TestClient
from httpx import Response

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.tax_core.app.main import create_app


def test_tax_core_allows_supported_role_and_tenant() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/computations/execute",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(role="IndividualTaxpayer"),
            "Idempotency-Key": "idem-tax-authz-001",
        },
        json={},
    )
    # Boundary is authz-only for this test.
    # 400 indicates authz passed and body validation took over.
    assert response.status_code == 400


def test_tax_core_rejects_missing_auth_context_deterministically() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/computations/execute",
        headers={"Idempotency-Key": "idem-tax-authz-002"},
        json={},
    )
    _assert_error_reason(
        response=response, expected_status=401, expected_reason="auth_context_missing"
    )


def test_tax_core_rejects_disallowed_role_deterministically() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/computations/execute",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(role="Administrator"),
            "Idempotency-Key": "idem-tax-authz-003",
        },
        json={},
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_role_forbidden",
    )


def test_tax_core_rejects_tenant_mismatch_deterministically() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/computations/execute",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                role="IndividualTaxpayer",
                tenant_id="other_tenant",
            ),
            "Idempotency-Key": "idem-tax-authz-004",
        },
        json={},
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_tenant_forbidden",
    )


def test_tax_core_repeated_forbidden_input_is_deterministic() -> None:
    client = TestClient(create_app())
    headers = {
        AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(role="Administrator"),
        "Idempotency-Key": "idem-tax-authz-005",
    }
    first = cast(
        dict[str, object], client.post("/computations/execute", headers=headers, json={}).json()
    )["detail"]
    second = cast(
        dict[str, object], client.post("/computations/execute", headers=headers, json={}).json()
    )["detail"]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())


def _assert_error_reason(
    *,
    response: Response,
    expected_status: int,
    expected_reason: str,
) -> None:
    status_code = response.status_code
    payload = cast(dict[str, object], response.json())
    detail = cast(dict[str, object], payload["detail"])
    assert status_code == expected_status
    assert detail["error_code"] == expected_reason
    assert detail["reason"] == expected_reason


def _build_auth_context_header(*, role: str, tenant_id: str = "default_tenant") -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": str(uuid4()),
            "tenant_id": tenant_id,
            "role": role,
            "session_id": "11111111-2222-3333-4444-555555555555",
            "delegation_context": {
                "is_delegated": False,
                "principal_user_id": None,
                "delegate_user_id": None,
                "delegation_id": None,
                "granted_at": None,
                "revoked_at": None,
            },
        }
    )
