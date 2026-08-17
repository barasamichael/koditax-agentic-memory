"""Persistence-focused tests for event-store append-only storage."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from datetime import UTC
from datetime import datetime

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import get_audit_events
from services.event_store.app.main import reset_audit_events
from services.event_store.app.config import load_database_url
from services.event_store.app.repository import EventStoreRepository


def test_append_event_persists_and_is_visible_in_snapshot() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=uuid4())},
        json={
            "event_type": "audit.persistence.test",
            "user_id": str(uuid4()),
            "correlation_id": "corr-event-store-persistence-001",
            "idempotency_key": _idempotency_key("idem-event-store-persistence-001"),
        },
    )
    assert response.status_code == 200
    payload = response.json()
    event_id = UUID(payload["event_id"])
    events = get_audit_events()
    assert len(events) == 1
    assert events[0].event_id == event_id
    assert events[0].event_type == "audit.persistence.test"
    assert events[0].correlation_id == "corr-event-store-persistence-001"
    assert events[0].trace_id
    assert events[0].created_at.endswith("Z")


def test_persisted_event_survives_repository_reinstantiation() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
        json={
            "event_type": "audit.persistence.restart",
            "user_id": str(user_id),
            "correlation_id": "corr-event-store-persistence-002",
            "idempotency_key": _idempotency_key("idem-event-store-persistence-002"),
        },
    )
    assert response.status_code == 200
    event_id = UUID(response.json()["event_id"])

    repository = EventStoreRepository(database_url=load_database_url())
    reloaded = repository.list_events_since(created_at_floor=datetime.fromtimestamp(0, tz=UTC))
    assert any(event.event_id == event_id for event in reloaded)


def test_persisted_event_verification_path_has_deterministic_read_ordering() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    first_response = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.persistence.ordering",
            "user_id": str(user_id),
            "correlation_id": "corr-event-store-persistence-003",
            "idempotency_key": _idempotency_key("idem-event-store-persistence-003-a"),
        },
    )
    second_response = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.persistence.ordering",
            "user_id": str(user_id),
            "correlation_id": "corr-event-store-persistence-003",
            "idempotency_key": _idempotency_key("idem-event-store-persistence-003-b"),
        },
    )
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_event_id = UUID(first_response.json()["event_id"])
    second_event_id = UUID(second_response.json()["event_id"])

    repository = EventStoreRepository(database_url=load_database_url())
    reloaded = repository.list_events_since(created_at_floor=datetime.fromtimestamp(0, tz=UTC))
    ordered_ids = [event.event_id for event in reloaded if event.user_id == user_id]
    assert first_event_id in ordered_ids
    assert second_event_id in ordered_ids
    assert ordered_ids.index(first_event_id) < ordered_ids.index(second_event_id)


def _auth_header(*, role: str, user_id: UUID) -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": str(user_id),
            "tenant_id": "default_tenant",
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


def _idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"
