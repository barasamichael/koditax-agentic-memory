"""Archival retention-hook tests for deterministic event-store transitions."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events
from services.event_store.app.repository import ARCHIVAL_FORBIDDEN
from services.event_store.app.repository import ARCHIVAL_INELIGIBLE
from services.event_store.app.repository import EventStoreRepository


def test_archival_transition_marks_eligible_event_deterministically() -> None:
    reset_audit_events()
    repository = EventStoreRepository()
    user_id = uuid4()
    old_timestamp = datetime.now(UTC) - timedelta(days=4000)
    persisted = repository.append_event(
        event_type="audit.archival.eligible",
        user_id=user_id,
        role_at_time="TaxAgent",
        trace_id="trace-archival-eligible-001",
        correlation_id="corr-archival-eligible-001",
        idempotency_key=f"idem-archival-eligible-001-{uuid4()}",
        is_delegated=False,
        principal_user_id=None,
        delegate_user_id=None,
        delegation_id=None,
        event_timestamp=old_timestamp,
    )

    client = TestClient(create_app())
    request_payload = {
        "event_id": str(persisted.event_id),
        "reason_code": "retention_expired",
        "archived_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    first = client.post("/audit/archival/mark", headers=headers, json=request_payload)
    second = client.post("/audit/archival/mark", headers=headers, json=request_payload)

    first_payload = first.json()
    second_payload = second.json()
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["status"] == "archived"
    assert second_payload["status"] == "already_archived"
    assert set(first_payload.keys()) == set(second_payload.keys())
    assert first_payload["event_id"] == second_payload["event_id"]
    assert first_payload["archival_reason_code"] == second_payload["archival_reason_code"]


def test_ineligible_archival_attempt_is_rejected_with_canonical_error() -> None:
    reset_audit_events()
    repository = EventStoreRepository()
    user_id = uuid4()
    persisted = repository.append_event(
        event_type="audit.archival.ineligible",
        user_id=user_id,
        role_at_time="TaxAgent",
        trace_id="trace-archival-ineligible-001",
        correlation_id="corr-archival-ineligible-001",
        idempotency_key=f"idem-archival-ineligible-001-{uuid4()}",
        is_delegated=False,
        principal_user_id=None,
        delegate_user_id=None,
        delegation_id=None,
        event_timestamp=datetime.now(UTC),
    )

    client = TestClient(create_app())
    response = client.post(
        "/audit/archival/mark",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
        json={
            "event_id": str(persisted.event_id),
            "reason_code": "retention_expired",
            "archived_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )
    detail = response.json()["detail"]
    assert response.status_code == 409
    assert detail["error_code"] == ARCHIVAL_INELIGIBLE
    assert detail["reason"] == ARCHIVAL_INELIGIBLE
    assert detail["reason_code"] == ARCHIVAL_INELIGIBLE


def test_cross_user_archival_attempt_is_forbidden_deterministically() -> None:
    reset_audit_events()
    repository = EventStoreRepository()
    owner_user_id = uuid4()
    other_user_id = uuid4()
    persisted = repository.append_event(
        event_type="audit.archival.forbidden",
        user_id=owner_user_id,
        role_at_time="TaxAgent",
        trace_id="trace-archival-forbidden-001",
        correlation_id="corr-archival-forbidden-001",
        idempotency_key=f"idem-archival-forbidden-001-{uuid4()}",
        is_delegated=False,
        principal_user_id=None,
        delegate_user_id=None,
        delegation_id=None,
        event_timestamp=datetime.now(UTC) - timedelta(days=4000),
    )

    client = TestClient(create_app())
    response = client.post(
        "/audit/archival/mark",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=other_user_id)},
        json={
            "event_id": str(persisted.event_id),
            "reason_code": "retention_expired",
            "archived_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )
    detail = response.json()["detail"]
    assert response.status_code == 403
    assert detail["error_code"] == ARCHIVAL_FORBIDDEN
    assert detail["reason"] == ARCHIVAL_FORBIDDEN
    assert detail["reason_code"] == ARCHIVAL_FORBIDDEN


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
