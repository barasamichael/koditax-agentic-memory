"""Gateway auth-context boundary enforcement tests."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from typing import TypedDict
from dataclasses import dataclass
from collections.abc import Iterator

from httpx import Response
import pytest
from fastapi.testclient import TestClient

from services.gateway.app.main import create_app as create_gateway_app
from services.gateway.app.main import get_audit_client
from services.gateway.app.main import AuditClientProtocol
from services.gateway.app.main import AuditEventAppendRequest
from services.gateway.app.main import AuditEventAppendResponse
from services.gateway.app.main import AUTH_CONTEXT_HEADER_NAME
from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME


class ToolPingResponseBody(TypedDict):
    """Represent /tools/ping success payload."""

    ok: bool
    event_id: str
    correlation_id: str


class ErrorEnvelopeBody(TypedDict):
    """Represent deterministic gateway auth error payload."""

    error_code: str
    message: str
    reason: str
    trace_id: str
    correlation_id: str
    details: dict[str, object]


@dataclass(frozen=True)
class FakeAuditClient(AuditClientProtocol):
    """In-process audit client for call-count assertions."""

    calls: list[AuditEventAppendRequest]
    auth_context_headers: list[str]

    async def append_audit_event(
        self,
        payload: AuditEventAppendRequest,
        auth_context_header: str,
    ) -> AuditEventAppendResponse:
        self.calls.append(payload)
        self.auth_context_headers.append(auth_context_header)
        return AuditEventAppendResponse(
            event_id=uuid4(),
            correlation_id=payload.correlation_id,
        )


@dataclass(frozen=True)
class IntegrationContext:
    """Represent initialized gateway test context."""

    gateway_client: TestClient
    fake_audit_client: FakeAuditClient


@pytest.fixture()
def integration_context() -> Iterator[IntegrationContext]:
    app = create_gateway_app()
    fake_audit_client = FakeAuditClient(calls=[], auth_context_headers=[])
    app.dependency_overrides[get_audit_client] = lambda: fake_audit_client
    with TestClient(app) as gateway_client:
        yield IntegrationContext(
            gateway_client=gateway_client,
            fake_audit_client=fake_audit_client,
        )
    app.dependency_overrides.clear()


def test_valid_auth_context_passes_and_routes_with_trusted_principal(
    integration_context: IntegrationContext,
) -> None:
    payload = _valid_auth_context_payload()
    headers = {
        AUTH_CONTEXT_HEADER_NAME: json.dumps(payload),
        "Idempotency-Key": "idem-gateway-auth-001",
    }
    response = integration_context.gateway_client.post("/tools/ping", headers=headers)

    body = cast(ToolPingResponseBody, response.json())
    assert response.status_code == 200
    assert body["ok"] is True
    UUID(body["event_id"])
    assert len(integration_context.fake_audit_client.calls) == 1
    assert integration_context.fake_audit_client.auth_context_headers == [json.dumps(payload)]
    append_payload = integration_context.fake_audit_client.calls[0]
    assert append_payload.trace_id
    assert append_payload.correlation_id == body["correlation_id"]
    assert append_payload.trace_id == response.headers[TRACE_ID_HEADER_NAME]


def test_missing_auth_context_header_is_rejected_deterministically(
    integration_context: IntegrationContext,
) -> None:
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={"Idempotency-Key": "idem-gateway-auth-002"},
    )

    _assert_gateway_error(
        response=response,
        expected_status=401,
        expected_reason="auth_context_missing",
    )
    assert len(integration_context.fake_audit_client.calls) == 0


def test_malformed_auth_context_header_is_rejected_deterministically(
    integration_context: IntegrationContext,
) -> None:
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: "{not-json",
            "Idempotency-Key": "idem-gateway-auth-003",
        },
    )

    _assert_gateway_error(
        response=response,
        expected_status=401,
        expected_reason="auth_context_malformed",
    )
    assert len(integration_context.fake_audit_client.calls) == 0


def test_missing_required_claim_is_rejected_deterministically(
    integration_context: IntegrationContext,
) -> None:
    payload = _valid_auth_context_payload()
    payload.pop("role")
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(payload),
            "Idempotency-Key": "idem-gateway-auth-004",
        },
    )

    _assert_gateway_error(
        response=response,
        expected_status=401,
        expected_reason="auth_context_invalid_claim",
    )
    assert len(integration_context.fake_audit_client.calls) == 0


def test_invalid_claim_values_are_rejected_deterministically(
    integration_context: IntegrationContext,
) -> None:
    for invalid_payload in (
        {
            **_valid_auth_context_payload(),
            "role": "NotAllowedRole",
        },
        {
            **_valid_auth_context_payload(),
            "session_id": "not-a-uuid",
        },
        {
            **_valid_auth_context_payload(),
            "delegation_context": {"is_delegated": True},
        },
    ):
        response = integration_context.gateway_client.post(
            "/tools/ping",
            headers={
                AUTH_CONTEXT_HEADER_NAME: json.dumps(invalid_payload),
                "Idempotency-Key": "idem-gateway-auth-005",
            },
        )

        _assert_gateway_error(
            response=response,
            expected_status=401,
            expected_reason="auth_context_invalid_claim",
        )
    assert len(integration_context.fake_audit_client.calls) == 0


def test_tenant_mismatch_is_rejected_with_canonical_reason(
    integration_context: IntegrationContext,
) -> None:
    payload = {
        **_valid_auth_context_payload(),
        "tenant_id": "other_tenant",
    }
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(payload),
            "Idempotency-Key": "idem-gateway-auth-005b",
        },
    )
    _assert_gateway_error(
        response=response,
        expected_status=403,
        expected_reason="authorization_tenant_forbidden",
    )
    assert len(integration_context.fake_audit_client.calls) == 0


def test_unsupported_auth_context_scope_is_rejected_deterministically(
    integration_context: IntegrationContext,
) -> None:
    payload = {
        **_valid_auth_context_payload(),
        "schema_version": "2.0.0",
    }
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(payload),
            "Idempotency-Key": "idem-gateway-auth-005c",
        },
    )
    _assert_gateway_error(
        response=response,
        expected_status=403,
        expected_reason="unsupported_auth_context_scope",
    )
    assert len(integration_context.fake_audit_client.calls) == 0


def test_repeated_invalid_input_has_stable_error_envelope_shape(
    integration_context: IntegrationContext,
) -> None:
    headers = {
        AUTH_CONTEXT_HEADER_NAME: "{bad-json",
        "Idempotency-Key": "idem-gateway-auth-006",
        CORRELATION_ID_HEADER_NAME: "corr-gateway-auth-determinism",
    }
    first_response = integration_context.gateway_client.post("/tools/ping", headers=headers)
    second_response = integration_context.gateway_client.post("/tools/ping", headers=headers)
    first = _error_envelope(first_response)
    second = _error_envelope(second_response)

    assert first["error_code"] == second["error_code"]
    assert first["reason"] == second["reason"]
    assert first["trace_id"] == second["trace_id"]
    assert first["correlation_id"] == second["correlation_id"]
    assert set(first.keys()) == set(second.keys())
    assert "{bad-json" not in json.dumps(first)
    assert "{bad-json" not in json.dumps(second)


def test_malformed_inbound_correlation_header_is_handled_deterministically(
    integration_context: IntegrationContext,
) -> None:
    payload = _valid_auth_context_payload()
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(payload),
            CORRELATION_ID_HEADER_NAME: "{bad-correlation-id}",
            "Idempotency-Key": "idem-gateway-auth-007",
        },
    )
    assert response.status_code == 200
    correlation_id = response.headers[CORRELATION_ID_HEADER_NAME]
    trace_id = response.headers[TRACE_ID_HEADER_NAME]
    UUID(correlation_id)
    UUID(trace_id)
    append_payload = integration_context.fake_audit_client.calls[-1]
    assert append_payload.correlation_id == correlation_id
    assert append_payload.trace_id == trace_id


def _assert_gateway_error(
    *,
    response: Response,
    expected_status: int,
    expected_reason: str,
) -> None:
    envelope = _error_envelope(response)
    assert response.status_code == expected_status
    assert envelope["error_code"] == expected_reason
    assert envelope["reason"] == expected_reason
    assert envelope["message"]
    assert envelope["trace_id"]
    assert envelope["correlation_id"]
    assert envelope["trace_id"] == envelope["correlation_id"]
    assert envelope["details"] is not None


def _error_envelope(response: Response) -> ErrorEnvelopeBody:
    payload = cast(dict[str, object], response.json())
    detail = cast(dict[str, object], payload["detail"])
    return cast(ErrorEnvelopeBody, detail)


def _valid_auth_context_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tenant_id": "default_tenant",
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
