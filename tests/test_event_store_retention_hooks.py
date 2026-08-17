"""Retention metadata hook tests for event-store append behavior."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events
from services.event_store.app.config import EVENT_RETENTION_DAYS_ENV_VAR
from services.event_store.app.repository import EventStoreRepository
from services.event_store.app.repository import RETENTION_POLICY_INVALID


def test_retention_metadata_assigned_deterministically() -> None:
    reset_audit_events()
    user_id = uuid4()
    base_time = datetime.now(UTC) - timedelta(days=10)
    repository = EventStoreRepository()
    persisted = repository.append_event(
        event_type="audit.retention.assigned",
        user_id=user_id,
        role_at_time="TaxAgent",
        trace_id="trace-retention-assigned-001",
        correlation_id="corr-retention-assigned-001",
        idempotency_key=f"idem-retention-assigned-001-{uuid4()}",
        is_delegated=False,
        principal_user_id=None,
        delegate_user_id=None,
        delegation_id=None,
        event_timestamp=base_time,
    )
    events = repository.list_events_since(created_at_floor=datetime.fromtimestamp(0, tz=UTC))
    matching = [item for item in events if item.event_id == persisted.event_id]
    assert len(matching) == 1
    event = matching[0]
    assert event.retention_policy_code
    assert event.retention_days > 0
    expected_expiry = (
        (base_time + timedelta(days=event.retention_days)).isoformat().replace("+00:00", "Z")
    )
    assert event.retention_expires_at == expected_expiry


def test_retention_policy_misconfiguration_fails_fast_with_canonical_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_audit_events()
    monkeypatch.setenv(EVENT_RETENTION_DAYS_ENV_VAR, "0")
    client = TestClient(create_app())
    user_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
        json={
            "event_type": "audit.retention.invalid-policy",
            "user_id": str(user_id),
            "correlation_id": "corr-retention-invalid-001",
            "idempotency_key": f"idem-retention-invalid-001-{uuid4()}",
        },
    )
    payload = response.json()["detail"]
    assert response.status_code == 500
    assert payload["error_code"] == RETENTION_POLICY_INVALID
    assert payload["reason"] == RETENTION_POLICY_INVALID
    assert payload["reason_code"] == RETENTION_POLICY_INVALID
    assert {"error_code", "message", "reason", "reason_code"}.issubset(payload.keys())


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
