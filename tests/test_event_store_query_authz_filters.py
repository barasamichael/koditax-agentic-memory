"""Authz and filter rejection tests for event-store query/replay endpoints."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events
from services.event_store.app.repository import QUERY_CURSOR_INVALID
from services.event_store.app.repository import QUERY_SCOPE_FORBIDDEN


def test_cross_tenant_query_scope_is_rejected_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    response = client.get(
        "/audit/events?tenant_id=other_tenant&limit=10",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
    )
    detail = response.json()["detail"]
    assert response.status_code == 403
    assert detail["error_code"] == QUERY_SCOPE_FORBIDDEN
    assert detail["reason"] == QUERY_SCOPE_FORBIDDEN
    assert detail["reason_code"] == QUERY_SCOPE_FORBIDDEN


def test_cross_user_query_scope_is_rejected_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    other_user_id = uuid4()
    response = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={other_user_id}&limit=10",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
    )
    detail = response.json()["detail"]
    assert response.status_code == 403
    assert detail["error_code"] == QUERY_SCOPE_FORBIDDEN
    assert detail["reason"] == QUERY_SCOPE_FORBIDDEN
    assert detail["reason_code"] == QUERY_SCOPE_FORBIDDEN


def test_invalid_pagination_cursor_is_rejected_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    first = client.get(
        "/audit/events?tenant_id=default_tenant&cursor=not_base64&limit=10",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
    )
    second = client.get(
        "/audit/events?tenant_id=default_tenant&cursor=not_base64&limit=10",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
    )
    detail_one = first.json()["detail"]
    detail_two = second.json()["detail"]
    assert first.status_code == 400
    assert second.status_code == 400
    assert detail_one["error_code"] == QUERY_CURSOR_INVALID
    assert detail_one["reason"] == QUERY_CURSOR_INVALID
    assert detail_one["reason_code"] == QUERY_CURSOR_INVALID
    assert detail_one["error_code"] == detail_two["error_code"]
    assert set(detail_one.keys()) == set(detail_two.keys())


def _auth_header(*, role: str, user_id: UUID, tenant_id: str = "default_tenant") -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": str(user_id),
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
