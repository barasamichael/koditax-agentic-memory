"""Canonical deterministic authz rejection contract tests."""

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
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME

ROUTER = APIRouter()
require_canonical_principal = build_authorized_principal_dependency(
    allowed_roles=frozenset({"TaxAgent"}),
    allow_delegation=False,
)


@ROUTER.get("/contract/protected")
def protected_endpoint(
    principal: Annotated[Principal, Depends(require_canonical_principal)],
) -> dict[str, object]:
    return {
        "ok": True,
        "user_id": str(principal.user_id),
        "role": principal.role,
    }


def test_authorized_request_is_unchanged() -> None:
    client = TestClient(_create_app())
    response = client.get(
        "/contract/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=uuid4(), role="TaxAgent")
        },
    )
    payload = cast(dict[str, object], response.json())
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["role"] == "TaxAgent"


def test_canonical_reasons_return_canonical_envelope() -> None:
    client = TestClient(_create_app())
    delegate_user_id = uuid4()
    principal_user_id = uuid4()
    cases = [
        ({}, "auth_context_missing", 401),
        ({AUTH_CONTEXT_HEADER_NAME: "{not-json"}, "auth_context_malformed", 401),
        (
            {
                AUTH_CONTEXT_HEADER_NAME: json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "user_id": str(uuid4()),
                        "tenant_id": "default_tenant",
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
            },
            "auth_context_invalid_claim",
            401,
        ),
        (
            {
                AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                    user_id=uuid4(),
                    role="TaxAgent",
                    schema_version="2.0.0",
                )
            },
            "unsupported_auth_context_scope",
            403,
        ),
        (
            {
                AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                    user_id=uuid4(),
                    role="IndividualTaxpayer",
                )
            },
            "authorization_role_forbidden",
            403,
        ),
        (
            {
                AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                    user_id=uuid4(),
                    role="TaxAgent",
                    tenant_id="other_tenant",
                )
            },
            "authorization_tenant_forbidden",
            403,
        ),
        (
            {
                AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                    user_id=delegate_user_id,
                    role="TaxAgent",
                    is_delegated=True,
                    principal_user_id=principal_user_id,
                    delegate_user_id=delegate_user_id,
                    delegation_id=uuid4(),
                    granted_at=(datetime.now(UTC) - timedelta(minutes=5))
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
            },
            "authorization_delegation_forbidden",
            403,
        ),
    ]

    for headers, expected_reason, expected_status in cases:
        response = client.get("/contract/protected", headers=headers)
        _assert_canonical_error_envelope(
            response=response,
            expected_status=expected_status,
            expected_reason=expected_reason,
        )


def test_trace_context_reason_codes_are_reported_deterministically() -> None:
    client = TestClient(_create_app())

    missing_context_response = client.get("/contract/protected")
    missing_detail = _assert_canonical_error_envelope(
        response=missing_context_response,
        expected_status=401,
        expected_reason="auth_context_missing",
    )
    assert (
        cast(dict[str, object], missing_detail["details"])["trace_context_reason"]
        == "trace_context_missing"
    )

    invalid_context_response = client.get(
        "/contract/protected",
        headers={
            AUTH_CONTEXT_HEADER_NAME: "{not-json",
            CORRELATION_ID_HEADER_NAME: "{bad-correlation-id}",
        },
    )
    invalid_detail = _assert_canonical_error_envelope(
        response=invalid_context_response,
        expected_status=401,
        expected_reason="auth_context_malformed",
    )
    assert (
        cast(dict[str, object], invalid_detail["details"])["trace_context_reason"]
        == "trace_context_invalid"
    )


def test_repeated_invalid_input_is_deterministic_and_redacted() -> None:
    client = TestClient(_create_app())
    malformed_header = "{super-secret-token-value}"
    headers = {
        AUTH_CONTEXT_HEADER_NAME: malformed_header,
        CORRELATION_ID_HEADER_NAME: "corr-authz-error-contract",
    }
    first = cast(dict[str, object], client.get("/contract/protected", headers=headers).json())[
        "detail"
    ]
    second = cast(dict[str, object], client.get("/contract/protected", headers=headers).json())[
        "detail"
    ]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())
    assert malformed_header not in json.dumps(first_error)


def test_repeated_unsupported_scope_failure_is_deterministic() -> None:
    client = TestClient(_create_app())
    headers = {
        AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
            user_id=uuid4(),
            role="TaxAgent",
            schema_version="2.0.0",
        )
    }
    first = cast(dict[str, object], client.get("/contract/protected", headers=headers).json())[
        "detail"
    ]
    second = cast(dict[str, object], client.get("/contract/protected", headers=headers).json())[
        "detail"
    ]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    assert first_error["error_code"] == "unsupported_auth_context_scope"
    assert second_error["error_code"] == "unsupported_auth_context_scope"
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())


def _create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ROUTER)
    return app


def _assert_canonical_error_envelope(
    *,
    response: object,
    expected_status: int,
    expected_reason: str,
) -> dict[str, object]:
    status_code = cast(int, response.status_code)
    payload = cast(dict[str, object], cast(object, response).json())
    detail = cast(dict[str, object], payload["detail"])
    assert status_code == expected_status
    assert detail["error_code"] == expected_reason
    assert detail["reason"] == expected_reason
    assert isinstance(detail["message"], str) and detail["message"]
    assert isinstance(detail["trace_id"], str) and detail["trace_id"]
    assert isinstance(detail["correlation_id"], str) and detail["correlation_id"]
    if "details" in detail:
        assert isinstance(detail["details"], dict)
    return detail


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
    granted_at: str | None = None,
    revoked_at: str | None = None,
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
                "granted_at": granted_at,
                "revoked_at": revoked_at,
            },
        }
    )
