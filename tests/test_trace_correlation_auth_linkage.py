"""Trace/correlation linkage tests across auth-protected gateway and audit flow."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from dataclasses import dataclass
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.gateway.app.main import create_app as create_gateway_app
from services.gateway.app.main import get_audit_client
from services.gateway.app.main import AuditClientProtocol
from services.gateway.app.main import AuditEventAppendRequest
from services.gateway.app.main import AuditEventAppendResponse
from services.gateway.app.main import AUTH_CONTEXT_HEADER_NAME
from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.event_store.app.main import create_app as create_event_store_app
from services.event_store.app.main import get_audit_events
from services.event_store.app.main import reset_audit_events


@dataclass(frozen=True)
class InProcessAuditClient(AuditClientProtocol):
    """Event-store in-process adapter for gateway integration tests."""

    event_store_app: FastAPI

    async def append_audit_event(
        self,
        payload: AuditEventAppendRequest,
        auth_context_header: str,
    ) -> AuditEventAppendResponse:
        headers = {
            AUTH_CONTEXT_HEADER_NAME: auth_context_header,
            CORRELATION_ID_HEADER_NAME: payload.correlation_id,
            TRACE_ID_HEADER_NAME: payload.trace_id,
        }
        transport = httpx.ASGITransport(app=self.event_store_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://event-store") as client:
            response = await client.post(
                "/audit/append",
                json=payload.model_dump(mode="json"),
                headers=headers,
            )
        response.raise_for_status()
        return AuditEventAppendResponse.model_validate(response.json())


@dataclass(frozen=True)
class IntegrationContext:
    gateway_client: TestClient


@pytest.fixture()
def integration_context() -> Iterator[IntegrationContext]:
    reset_audit_events()
    event_store_app = create_event_store_app()
    gateway_app = create_gateway_app()
    gateway_app.dependency_overrides[get_audit_client] = lambda: InProcessAuditClient(
        event_store_app=event_store_app
    )
    with TestClient(gateway_app) as gateway_client:
        yield IntegrationContext(gateway_client=gateway_client)
    gateway_app.dependency_overrides.clear()
    reset_audit_events()


def test_linkage_preserved_gateway_to_audit_with_provided_ids(
    integration_context: IntegrationContext,
) -> None:
    user_id = uuid4()
    correlation_id = "corr-auth-linkage-001"
    trace_id = "trace-auth-linkage-001"
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=user_id),
            CORRELATION_ID_HEADER_NAME: correlation_id,
            TRACE_ID_HEADER_NAME: trace_id,
            "Idempotency-Key": _idempotency_key("idem-auth-linkage-001"),
        },
    )

    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER_NAME] == correlation_id
    assert response.headers[TRACE_ID_HEADER_NAME] == trace_id

    audit_event = get_audit_events()[0]
    assert audit_event.correlation_id == correlation_id
    assert audit_event.trace_id == trace_id


def test_authz_rejection_still_carries_trace_and_correlation_ids(
    integration_context: IntegrationContext,
) -> None:
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-auth-linkage-002",
            TRACE_ID_HEADER_NAME: "trace-auth-linkage-002",
            "Idempotency-Key": _idempotency_key("idem-auth-linkage-002"),
        },
    )
    payload = cast(dict[str, object], response.json())
    detail = cast(dict[str, object], payload["detail"])
    assert response.status_code == 401
    assert detail["reason"] == "auth_context_missing"
    assert detail["correlation_id"] == "corr-auth-linkage-002"
    assert detail["trace_id"] == "trace-auth-linkage-002"


def test_malformed_inbound_correlation_header_is_handled_deterministically(
    integration_context: IntegrationContext,
) -> None:
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=uuid4()),
            CORRELATION_ID_HEADER_NAME: "{bad-correlation}",
            TRACE_ID_HEADER_NAME: "{bad-trace}",
            "Idempotency-Key": _idempotency_key("idem-auth-linkage-003"),
        },
    )

    assert response.status_code == 200
    generated_correlation = response.headers[CORRELATION_ID_HEADER_NAME]
    generated_trace = response.headers[TRACE_ID_HEADER_NAME]
    UUID(generated_correlation)
    UUID(generated_trace)

    audit_event = get_audit_events()[0]
    assert audit_event.correlation_id == generated_correlation
    assert audit_event.trace_id == generated_trace


def test_repeated_invalid_request_path_has_stable_linkage_semantics(
    integration_context: IntegrationContext,
) -> None:
    headers = {
        AUTH_CONTEXT_HEADER_NAME: "{bad-json",
        CORRELATION_ID_HEADER_NAME: "{bad-correlation}",
        "Idempotency-Key": _idempotency_key("idem-auth-linkage-004"),
    }
    first = cast(
        dict[str, object],
        integration_context.gateway_client.post("/tools/ping", headers=headers).json(),
    )["detail"]
    second = cast(
        dict[str, object],
        integration_context.gateway_client.post("/tools/ping", headers=headers).json(),
    )["detail"]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    first_details = cast(dict[str, object], first_error["details"])
    second_details = cast(dict[str, object], second_error["details"])
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())
    assert first_details["trace_context_reason"] == "trace_context_invalid"
    assert second_details["trace_context_reason"] == "trace_context_invalid"


def test_unsupported_scope_is_blocked_before_downstream_audit_call(
    integration_context: IntegrationContext,
) -> None:
    unsupported_context = json.loads(_build_auth_context_header(user_id=uuid4()))
    unsupported_context["schema_version"] = "2.0.0"
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: json.dumps(unsupported_context),
            CORRELATION_ID_HEADER_NAME: "corr-auth-linkage-005",
            TRACE_ID_HEADER_NAME: "trace-auth-linkage-005",
            "Idempotency-Key": _idempotency_key("idem-auth-linkage-005"),
        },
    )
    payload = cast(dict[str, object], response.json())
    detail = cast(dict[str, object], payload["detail"])
    assert response.status_code == 403
    assert detail["reason"] == "unsupported_auth_context_scope"
    assert detail["correlation_id"] == "corr-auth-linkage-005"
    assert detail["trace_id"] == "trace-auth-linkage-005"
    assert len(get_audit_events()) == 0


def _build_auth_context_header(*, user_id: UUID) -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": str(user_id),
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
    )


def _idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"
