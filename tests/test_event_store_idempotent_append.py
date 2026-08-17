"""Idempotent append semantics for event-store runtime."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import get_audit_events
from services.event_store.app.main import reset_audit_events
from services.event_store.app.repository import APPEND_CONFLICT


def test_first_append_then_equivalent_replay_is_deterministic() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    idempotency_key = f"idem-event-store-append-replay-{uuid4()}"
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    payload = {
        "event_type": "audit.idempotent.append",
        "user_id": str(user_id),
        "trace_id": "trace-event-store-idem-001",
        "correlation_id": "corr-event-store-idem-001",
        "idempotency_key": idempotency_key,
    }

    first_response = client.post("/audit/append", headers=headers, json=payload)
    second_response = client.post("/audit/append", headers=headers, json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_payload = first_response.json()
    second_payload = second_response.json()
    assert set(first_payload.keys()) == set(second_payload.keys())
    assert first_payload["event_id"] == second_payload["event_id"]
    assert first_payload["correlation_id"] == second_payload["correlation_id"]

    events = [event for event in get_audit_events() if event.user_id == user_id]
    assert len(events) == 1
    assert events[0].event_id == UUID(first_payload["event_id"])


def test_same_idempotency_key_with_payload_drift_returns_conflict_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    idempotency_key = f"idem-event-store-append-conflict-{uuid4()}"
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    first_payload = {
        "event_type": "audit.idempotent.append",
        "user_id": str(user_id),
        "trace_id": "trace-event-store-idem-002",
        "correlation_id": "corr-event-store-idem-002",
        "idempotency_key": idempotency_key,
    }
    drift_payload = {
        "event_type": "audit.idempotent.append.changed",
        "user_id": str(user_id),
        "trace_id": "trace-event-store-idem-002",
        "correlation_id": "corr-event-store-idem-002",
        "idempotency_key": idempotency_key,
    }
    first_response = client.post("/audit/append", headers=headers, json=first_payload)
    conflict_one = client.post("/audit/append", headers=headers, json=drift_payload)
    conflict_two = client.post("/audit/append", headers=headers, json=drift_payload)

    assert first_response.status_code == 200
    assert conflict_one.status_code == 409
    assert conflict_two.status_code == 409
    detail_one = conflict_one.json()["detail"]
    detail_two = conflict_two.json()["detail"]
    assert detail_one["error_code"] == APPEND_CONFLICT
    assert detail_one["reason"] == APPEND_CONFLICT
    assert detail_one["reason_code"] == APPEND_CONFLICT
    assert detail_one["error_code"] == detail_two["error_code"]
    assert detail_one["reason"] == detail_two["reason"]
    assert detail_one["reason_code"] == detail_two["reason_code"]
    assert set(detail_one.keys()) == set(detail_two.keys())


def test_missing_or_invalid_idempotency_key_is_fail_closed() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    missing_key = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.idempotent.append",
            "user_id": str(user_id),
            "trace_id": "trace-event-store-idem-003",
            "correlation_id": "corr-event-store-idem-003",
        },
    )
    blank_key = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.idempotent.append",
            "user_id": str(user_id),
            "trace_id": "trace-event-store-idem-003",
            "correlation_id": "corr-event-store-idem-003",
            "idempotency_key": "   ",
        },
    )
    too_long_key = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.idempotent.append",
            "user_id": str(user_id),
            "trace_id": "trace-event-store-idem-003",
            "correlation_id": "corr-event-store-idem-003",
            "idempotency_key": "x" * 129,
        },
    )
    assert missing_key.status_code == 400
    assert blank_key.status_code == 400
    assert too_long_key.status_code == 400
    missing_detail = missing_key.json()["detail"]
    blank_detail = blank_key.json()["detail"]
    too_long_detail = too_long_key.json()["detail"]
    assert missing_detail["error_code"] == "invalid_event_store_request"
    assert missing_detail["reason"] == "invalid_event_store_request"
    assert blank_detail["error_code"] == "invalid_event_store_request"
    assert blank_detail["reason"] == "invalid_event_store_request"
    assert too_long_detail["error_code"] == "invalid_event_store_request"
    assert too_long_detail["reason"] == "invalid_event_store_request"
    assert set(missing_detail.keys()) == set(blank_detail.keys())
    assert set(blank_detail.keys()) == set(too_long_detail.keys())


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
