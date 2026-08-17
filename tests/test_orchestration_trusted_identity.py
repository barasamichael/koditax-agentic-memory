"""Trusted identity checks for protected orchestration conversation routes."""

from __future__ import annotations

import json
from uuid import uuid4
from typing import cast
from datetime import UTC
from datetime import datetime

import httpx
import pytest
from fastapi import Request
from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.testclient import TestClient

from shared.authz.rbac import Principal
from services.gateway.app import main as gateway_main
from shared.authz.delegation import DelegationContext
from services.orchestration.app.main import create_app
from services.orchestration.app.main import PromptIngestionRequest
from services.orchestration.app.main import (
    _resolve_trusted_conversation_owner,  # pyright: ignore[reportPrivateUsage]
)


def test_decide_requires_auth_context_before_any_followup_lookup() -> None:
    response = TestClient(create_app()).post(
        "/v1/orchestration/prompt/decide",
        json=_prompt_payload(),
        headers={"X-Test-Anonymous": "1"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "auth_context_missing"


def test_administrator_is_denied_raw_orchestration_state_access() -> None:
    response = TestClient(create_app()).post(
        "/v1/orchestration/prompt/decide",
        json=_prompt_payload(),
        headers={"X-Auth-Context": _auth_context(role="Administrator")},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "authorization_role_forbidden"


def test_requested_tenant_must_match_trusted_principal_tenant() -> None:
    response = TestClient(create_app()).post(
        "/v1/orchestration/prompt/decide",
        json=_prompt_payload(tenant_id="other_tenant"),
        headers={"X-Auth-Context": _auth_context(tenant_id="pilot_tenant_alpha")},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "authorization_tenant_forbidden"


def test_execute_rejects_removed_client_user_id_field() -> None:
    principal_id = str(uuid4())
    body = {
        **_prompt_payload(),
        "user_id": str(uuid4()),
        "idempotency_key": "trusted-owner-test",
        "intent_class": "income_tax_compute",
        "tax_domain_hint": "income_tax",
        "decision_id": "a" * 64,
        "selected_route": None,
    }
    response = TestClient(create_app()).post(
        "/v1/orchestration/prompt/execute",
        json=body,
        headers={
            "X-Auth-Context": _auth_context(user_id=principal_id),
            "X-Test-Allow-Client-User-Id": "1",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "invalid_orchestration_request"


def test_active_delegated_agent_derives_the_taxpayer_owner() -> None:
    taxpayer_id = uuid4()
    agent_id = uuid4()
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/test"})
    payload = PromptIngestionRequest.model_validate(_prompt_payload())
    owner = _resolve_trusted_conversation_owner(
        request=request,
        payload=payload,
        principal=Principal(
            user_id=agent_id,
            role="TaxAgent",
            tenant_id="pilot_tenant_alpha",
            delegation_context=DelegationContext(
                is_delegated=True,
                principal_user_id=taxpayer_id,
                delegate_user_id=agent_id,
                delegation_id=uuid4(),
                granted_at=datetime.now(UTC),
                revoked_at=None,
            ),
        ),
    )

    assert owner["effective_taxpayer_user_id"] == str(taxpayer_id)
    assert owner["delegation_id"] is not None


def test_undelegated_agent_cannot_derive_a_taxpayer_owner() -> None:
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/test"})
    with pytest.raises(HTTPException) as error:
        _resolve_trusted_conversation_owner(
            request=request,
            payload=PromptIngestionRequest.model_validate(_prompt_payload()),
            principal=Principal(
                user_id=uuid4(),
                role="TaxAgent",
                tenant_id="pilot_tenant_alpha",
            ),
        )

    assert error.value.status_code == 403


def test_gateway_forwards_the_validated_auth_context_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def _forward(*, request: Request, stream: bool) -> Response:
        captured.update(dict(request.headers))
        assert stream is False
        return Response(status_code=204)

    monkeypatch.setattr(gateway_main, "_forward_orchestration_request", _forward)  # type: ignore[attr-defined]
    auth_context = _auth_context()
    response = TestClient(gateway_main.create_app()).post(
        "/v1/orchestration/prompt/decide",
        json=_prompt_payload(),
        headers={"X-Auth-Context": auth_context},
    )

    assert response.status_code == 204
    assert captured["x-auth-context"] == auth_context


def test_gateway_relays_stream_events_with_the_validated_auth_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class _EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b'event: final\ndata: {"status":"executed"}\n\n'

        async def aclose(self) -> None:
            pass

    class _StreamingClient:
        def __init__(self, **_: object) -> None:
            pass

        def build_request(self, **kwargs: object) -> httpx.Request:
            headers = cast(dict[str, str], kwargs.get("headers"))
            captured.update(headers)
            return httpx.Request("POST", "http://orchestration.test/stream", headers=headers)

        async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
            assert stream is True
            return httpx.Response(
                status_code=200,
                stream=_EventStream(),
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(gateway_main.httpx, "AsyncClient", _StreamingClient)
    auth_context = _auth_context()
    response = TestClient(gateway_main.create_app()).post(
        "/v1/orchestration/prompt/execute/stream",
        json=_prompt_payload(),
        headers={"X-Auth-Context": auth_context},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == 'event: final\ndata: {"status":"executed"}\n\n'
    assert captured["X-Auth-Context"] == auth_context


def _prompt_payload(*, tenant_id: str = "pilot_tenant_alpha") -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "conversation_id": "trusted-owner-conversation",
        "channel": "chat",
        "prompt": {"text": "compute income tax", "format": "plain_text"},
    }


def _auth_context(
    *,
    user_id: str | None = None,
    tenant_id: str = "pilot_tenant_alpha",
    role: str = "IndividualTaxpayer",
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": user_id or str(uuid4()),
            "tenant_id": tenant_id,
            "role": role,
            "session_id": str(uuid4()),
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
