"""Deterministic replay/query tests for event-store runtime."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events


def test_query_by_user_returns_scoped_events_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    other_user_id = uuid4()
    _append_event(client=client, user_id=user_id, correlation_id="corr-query-user-001", suffix="a")
    _append_event(client=client, user_id=user_id, correlation_id="corr-query-user-002", suffix="b")
    _append_event(
        client=client,
        user_id=other_user_id,
        correlation_id="corr-query-user-003",
        suffix="c",
    )

    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    response_one = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )
    response_two = client.get(
        f"/audit/events?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    payload_one = response_one.json()
    payload_two = response_two.json()
    assert set(payload_one.keys()) == set(payload_two.keys())
    assert [item["event_id"] for item in payload_one["events"]] == [
        item["event_id"] for item in payload_two["events"]
    ]
    assert all(item["user_id"] == str(user_id) for item in payload_one["events"])
    assert all("event_checksum" in item for item in payload_one["events"])
    assert all("previous_event_checksum" in item for item in payload_one["events"])


def test_replay_by_correlation_is_deterministic_and_ordered() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    correlation_id = "corr-replay-deterministic-001"
    _append_event(client=client, user_id=user_id, correlation_id=correlation_id, suffix="a")
    _append_event(client=client, user_id=user_id, correlation_id=correlation_id, suffix="b")
    _append_event(
        client=client,
        user_id=user_id,
        correlation_id="corr-replay-other-001",
        suffix="c",
    )

    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    response_one = client.get(
        f"/audit/replay/{correlation_id}?tenant_id=default_tenant&limit=50",
        headers=headers,
    )
    response_two = client.get(
        f"/audit/replay/{correlation_id}?tenant_id=default_tenant&limit=50",
        headers=headers,
    )

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    payload_one = response_one.json()
    payload_two = response_two.json()
    assert set(payload_one.keys()) == set(payload_two.keys())
    assert payload_one["correlation_id"] == correlation_id
    assert all(item["correlation_id"] == correlation_id for item in payload_one["events"])
    assert [item["event_id"] for item in payload_one["events"]] == [
        item["event_id"] for item in payload_two["events"]
    ]
    assert all("event_checksum" in item for item in payload_one["events"])
    assert all("previous_event_checksum" in item for item in payload_one["events"])
    assert [item["created_at"] for item in payload_one["events"]] == sorted(
        item["created_at"] for item in payload_one["events"]
    )


def _append_event(*, client: TestClient, user_id: UUID, correlation_id: str, suffix: str) -> None:
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
        json={
            "event_type": "audit.query.replay",
            "user_id": str(user_id),
            "trace_id": f"trace-query-replay-{suffix}",
            "correlation_id": correlation_id,
            "idempotency_key": f"idem-query-replay-{suffix}-{uuid4()}",
        },
    )
    assert response.status_code == 200


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
