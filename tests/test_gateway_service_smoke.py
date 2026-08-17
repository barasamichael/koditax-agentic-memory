"""Smoke tests for gateway runtime boundary."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from dataclasses import dataclass
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.gateway.app.main import create_app
from services.gateway.app.main import get_audit_client
from services.gateway.app.main import AuditClientProtocol
from services.gateway.app.main import AuditEventAppendRequest
from services.gateway.app.main import AuditEventAppendResponse
from services.gateway.app.main import AUTH_CONTEXT_HEADER_NAME


@dataclass(frozen=True)
class FakeAuditClient(AuditClientProtocol):
    async def append_audit_event(
        self,
        payload: AuditEventAppendRequest,
        auth_context_header: str,
    ) -> AuditEventAppendResponse:
        _ = auth_context_header
        return AuditEventAppendResponse(event_id=uuid4(), correlation_id=payload.correlation_id)


@pytest.fixture()
def gateway_client() -> Iterator[TestClient]:
    app = create_app()
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_audit_client] = lambda: FakeAuditClient()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_gateway_app_boots_and_ping_route_is_available(gateway_client: TestClient) -> None:
    response = gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _valid_auth_context_header(),
            "Idempotency-Key": "idem-gateway-smoke-001",
            "X-Correlation-ID": "gateway-smoke-corr",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    UUID(payload["event_id"])
    assert payload["correlation_id"] == "gateway-smoke-corr"


def test_gateway_auth_rejection_is_deterministic(gateway_client: TestClient) -> None:
    headers = {"Idempotency-Key": "idem-gateway-smoke-002", "X-Correlation-ID": "gateway-auth-fail"}
    first = gateway_client.post("/tools/ping", headers=headers)
    second = gateway_client.post("/tools/ping", headers=headers)
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]

    assert first.status_code == 401
    assert second.status_code == 401
    assert first_detail["error_code"] == second_detail["error_code"]
    assert first_detail["reason"] == second_detail["reason"]
    assert set(first_detail.keys()) == set(second_detail.keys())


def test_gateway_authz_forbidden_is_deterministic(gateway_client: TestClient) -> None:
    headers = {
        AUTH_CONTEXT_HEADER_NAME: _valid_auth_context_header(tenant_id="other_tenant"),
        "Idempotency-Key": "idem-gateway-smoke-003",
        "X-Correlation-ID": "gateway-authz-forbidden",
    }
    first = gateway_client.post("/tools/ping", headers=headers)
    second = gateway_client.post("/tools/ping", headers=headers)
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]

    assert first.status_code == 403
    assert second.status_code == 403
    assert first_detail["error_code"] == second_detail["error_code"]
    assert first_detail["reason"] == second_detail["reason"]
    assert set(first_detail.keys()) == set(second_detail.keys())


def test_gateway_health_tax_domain_path_fails_closed_canonically(
    gateway_client: TestClient,
) -> None:
    headers = {"X-Correlation-ID": "gateway-health-path-fail"}
    first = gateway_client.post("/v1/gateway/health-contribution/prompt/execute", headers=headers)
    second = gateway_client.post("/v1/gateway/health-contribution/prompt/execute", headers=headers)
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]

    assert first.status_code == 501
    assert second.status_code == 501
    assert first_detail == second_detail
    assert first_detail["error_code"] == "unsupported_tax_domain_path"
    assert first_detail["reason"] == "active_orchestration_led_boundary"
    assert first_detail["details"] == {
        "requested_path": "/v1/gateway/health-contribution/prompt/execute",
        "tax_domain": "health-contribution",
        "supported_execution_boundary": "orchestration",
    }


def test_gateway_invalid_tax_domain_rejects_deterministically(gateway_client: TestClient) -> None:
    headers = {"X-Correlation-ID": "gateway-invalid-domain-fail"}
    first = gateway_client.get("/v1/gateway/wealth-tax/compute", headers=headers)
    second = gateway_client.get("/v1/gateway/wealth-tax/compute", headers=headers)
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]

    assert first.status_code == 400
    assert second.status_code == 400
    assert first_detail == second_detail
    assert first_detail["error_code"] == "invalid_tax_domain"
    assert first_detail["reason"] == "invalid_tax_domain"
    assert first_detail["details"] == {
        "requested_path": "/v1/gateway/wealth-tax/compute",
        "tax_domain": "wealth-tax",
    }


def _valid_auth_context_header(*, tenant_id: str = "default_tenant") -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "tenant_id": tenant_id,
            "role": "IndividualTaxpayer",
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
