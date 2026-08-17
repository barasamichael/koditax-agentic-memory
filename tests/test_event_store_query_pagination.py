"""Deterministic pagination tests for event-store query endpoints."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events


def test_query_pagination_has_stable_cursor_and_ordering() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    for index in range(5):
        response = client.post(
            "/audit/append",
            headers=headers,
            json={
                "event_type": "audit.query.pagination",
                "user_id": str(user_id),
                "trace_id": f"trace-query-pagination-{index}",
                "correlation_id": "corr-query-pagination-001",
                "idempotency_key": f"idem-query-pagination-{index}-{uuid4()}",
            },
        )
        assert response.status_code == 200

    first_page = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={user_id}&limit=2",
        headers=headers,
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert len(first_payload["events"]) == 2
    assert first_payload["next_cursor"] is not None

    second_page = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={user_id}&limit=2&cursor={first_payload['next_cursor']}",
        headers=headers,
    )
    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert len(second_payload["events"]) == 2
    assert second_payload["next_cursor"] is not None

    combined_ids = [item["event_id"] for item in first_payload["events"]] + [
        item["event_id"] for item in second_payload["events"]
    ]
    assert len(set(combined_ids)) == len(combined_ids)


def test_repeated_identical_query_page_is_deterministic() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    for index in range(3):
        response = client.post(
            "/audit/append",
            headers=headers,
            json={
                "event_type": "audit.query.pagination.repeat",
                "user_id": str(user_id),
                "trace_id": f"trace-query-pagination-repeat-{index}",
                "correlation_id": "corr-query-pagination-repeat-001",
                "idempotency_key": f"idem-query-pagination-repeat-{index}-{uuid4()}",
            },
        )
        assert response.status_code == 200

    first = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={user_id}&limit=2",
        headers=headers,
    )
    second = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={user_id}&limit=2",
        headers=headers,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    payload_one = first.json()
    payload_two = second.json()
    assert set(payload_one.keys()) == set(payload_two.keys())
    assert payload_one["next_cursor"] == payload_two["next_cursor"]
    assert [item["event_id"] for item in payload_one["events"]] == [
        item["event_id"] for item in payload_two["events"]
    ]


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
