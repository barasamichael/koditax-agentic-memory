"""Test dummy end-to-end tool ping flow through gateway and event-store."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from typing import TypedDict
from dataclasses import dataclass
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.errors import codes
from shared.errors.envelope import ErrorEnvelope
from services.gateway.app.main import create_app as create_gateway_app
from services.gateway.app.main import get_audit_client
from services.gateway.app.main import AuditClientProtocol
from services.gateway.app.main import AuditEventAppendRequest
from services.gateway.app.main import AuditEventAppendResponse
from services.gateway.app.main import AUTH_CONTEXT_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.event_store.app.main import create_app as create_event_store_app
from services.event_store.app.main import get_audit_events
from services.event_store.app.main import reset_audit_events


class ToolPingResponseBody(TypedDict):
    """Represent the gateway /tools/ping response payload."""

    ok: bool
    event_id: str
    correlation_id: str


class ErrorBody(TypedDict):
    """Represent FastAPI HTTPException payload shape for Option A envelope."""

    detail: ErrorEnvelope


@dataclass(frozen=True)
class IntegrationContext:
    """Represent initialized app clients used in integration tests."""

    gateway_client: TestClient
    fake_audit_client: InProcessAuditClient


class InProcessAuditClient(AuditClientProtocol):
    """Call event-store app in-process for gateway integration tests.

    :param event_store_app: Event-store FastAPI application.
    """

    def __init__(self, event_store_app: FastAPI) -> None:
        self._event_store_app = event_store_app
        self.last_response: AuditEventAppendResponse | None = None
        self.last_response_header_correlation_id: str | None = None

    async def append_audit_event(
        self,
        payload: AuditEventAppendRequest,
        auth_context_header: str,
    ) -> AuditEventAppendResponse:
        """Append an audit event through in-process ASGI transport.

        :param payload: Audit append payload from gateway.
        :param auth_context_header: Auth context header forwarded by gateway.
        :return: Event-store append response payload.
        """

        headers = {
            AUTH_CONTEXT_HEADER_NAME: auth_context_header,
            CORRELATION_ID_HEADER_NAME: payload.correlation_id,
        }
        transport = httpx.ASGITransport(app=self._event_store_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://event-store") as client:
            response = await client.post(
                "/audit/append",
                json=payload.model_dump(mode="json"),
                headers=headers,
            )
        response.raise_for_status()
        self.last_response_header_correlation_id = response.headers.get(CORRELATION_ID_HEADER_NAME)
        parsed = AuditEventAppendResponse.model_validate(response.json())
        self.last_response = parsed
        return parsed


@pytest.fixture()
def integration_context() -> Iterator[IntegrationContext]:
    """Create in-process gateway and event-store applications for testing.

    :return: Iterator of integration test context.
    """

    reset_audit_events()
    event_store_app = create_event_store_app()
    gateway_app = create_gateway_app()
    fake_audit_client = InProcessAuditClient(event_store_app=event_store_app)
    gateway_app.dependency_overrides[get_audit_client] = lambda: fake_audit_client

    with TestClient(gateway_app) as gateway_client:
        yield IntegrationContext(
            gateway_client=gateway_client,
            fake_audit_client=fake_audit_client,
        )

    gateway_app.dependency_overrides.clear()
    reset_audit_events()


def test_tools_ping_success_appends_audit_event(
    integration_context: IntegrationContext,
) -> None:
    """Verify /tools/ping returns success and appends one audit event.

    :param integration_context: Initialized integration test context.
    :return: None.
    """

    user_id = uuid4()
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=user_id),
            "Idempotency-Key": "idem-001",
        },
    )

    payload = cast(ToolPingResponseBody, _response_json(response))
    assert response.status_code == 200
    assert payload["ok"] is True
    UUID(payload["event_id"])

    audit_events = get_audit_events()
    assert len(audit_events) == 1
    stored_event = audit_events[0]
    assert stored_event.event_type == "tool.ping"
    assert stored_event.idempotency_key == "idem-001"
    assert str(stored_event.user_id) == str(user_id)
    assert stored_event.correlation_id == payload["correlation_id"]


def test_tools_ping_propagates_provided_correlation_id(
    integration_context: IntegrationContext,
) -> None:
    """Verify provided correlation ID remains unchanged end-to-end.

    :param integration_context: Initialized integration test context.
    :return: None.
    """

    user_id = uuid4()
    provided_correlation_id = "corr-provided-123"
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=user_id),
            "Idempotency-Key": "idem-002",
            CORRELATION_ID_HEADER_NAME: provided_correlation_id,
        },
    )

    payload = cast(ToolPingResponseBody, _response_json(response))
    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER_NAME] == provided_correlation_id
    assert payload["correlation_id"] == provided_correlation_id

    fake_response = integration_context.fake_audit_client.last_response
    assert fake_response is not None
    assert fake_response.correlation_id == provided_correlation_id
    assert (
        integration_context.fake_audit_client.last_response_header_correlation_id
        == provided_correlation_id
    )

    stored_event = get_audit_events()[0]
    assert stored_event.correlation_id == provided_correlation_id


def test_tools_ping_generates_correlation_id_when_missing(
    integration_context: IntegrationContext,
) -> None:
    """Verify missing correlation ID is generated and propagated end-to-end.

    :param integration_context: Initialized integration test context.
    :return: None.
    """

    user_id = uuid4()
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=user_id),
            "Idempotency-Key": "idem-003",
        },
    )

    payload = cast(ToolPingResponseBody, _response_json(response))
    generated_correlation_id = response.headers[CORRELATION_ID_HEADER_NAME]

    assert response.status_code == 200
    assert payload["correlation_id"] == generated_correlation_id
    UUID(generated_correlation_id)

    fake_response = integration_context.fake_audit_client.last_response
    assert fake_response is not None
    assert fake_response.correlation_id == generated_correlation_id
    assert (
        integration_context.fake_audit_client.last_response_header_correlation_id
        == generated_correlation_id
    )

    stored_event = get_audit_events()[0]
    assert stored_event.correlation_id == generated_correlation_id


def test_tools_ping_requires_authorization_header(
    integration_context: IntegrationContext,
) -> None:
    """Verify missing auth context returns 401 with standard envelope.

    :param integration_context: Initialized integration test context.
    :return: None.
    """

    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={"Idempotency-Key": "idem-004"},
    )

    envelope = _extract_error_envelope(response)
    assert response.status_code == 401
    assert envelope["error_code"] == "auth_context_missing"
    assert len(get_audit_events()) == 0


def test_tools_ping_requires_idempotency_key(
    integration_context: IntegrationContext,
) -> None:
    """Verify missing Idempotency-Key returns 400 with standard envelope.

    :param integration_context: Initialized integration test context.
    :return: None.
    """

    user_id = uuid4()
    response = integration_context.gateway_client.post(
        "/tools/ping",
        headers={AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(user_id=user_id)},
    )

    envelope = _extract_error_envelope(response)
    assert response.status_code == 400
    assert envelope["error_code"] == codes.MISSING_IDEMPOTENCY_KEY
    assert len(get_audit_events()) == 0


def _build_auth_context_header(*, user_id: UUID) -> str:
    """Build canonical auth context header payload for gateway boundary tests."""

    return (
        "{"
        '"schema_version":"1.0.0",'
        f'"user_id":"{user_id}",'
        '"tenant_id":"default_tenant",'
        '"role":"IndividualTaxpayer",'
        '"session_id":"11111111-2222-3333-4444-555555555555",'
        '"delegation_context":{'
        '"is_delegated":false,'
        '"principal_user_id":null,'
        '"delegate_user_id":null,'
        '"delegation_id":null,'
        '"granted_at":null,'
        '"revoked_at":null'
        "}"
        "}"
    )


def _response_json(response: object) -> dict[str, object]:
    """Parse a TestClient response payload into a JSON object.

    :param response: TestClient response object.
    :return: Parsed JSON object payload.
    """

    # TestClient response payload is an untyped runtime boundary in tests.
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _extract_error_envelope(response: object) -> ErrorEnvelope:
    """Extract Option A error envelope from a FastAPI HTTPException payload.

    :param response: TestClient response object.
    :return: Parsed standard error envelope.
    """

    payload = _response_json(response)
    error_payload = cast(ErrorBody, payload)
    envelope = error_payload["detail"]

    assert "error_code" in envelope
    assert "message" in envelope
    assert "correlation_id" in envelope
    return envelope
