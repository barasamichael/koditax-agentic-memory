"""Deterministic integrity hash-chain verification tests for event-store."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events


def test_integrity_verification_passes_deterministically_for_unchanged_sequence() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    for index in range(3):
        response = client.post(
            "/audit/append",
            headers=headers,
            json={
                "event_type": "audit.integrity.sequence",
                "user_id": str(user_id),
                "trace_id": f"trace-integrity-sequence-{index}",
                "correlation_id": "corr-integrity-sequence-001",
                "idempotency_key": f"idem-integrity-sequence-{index}-{uuid4()}",
            },
        )
        assert response.status_code == 200

    first = client.get(
        f"/audit/integrity/verify?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )
    second = client.get(
        f"/audit/integrity/verify?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    payload_one = first.json()
    payload_two = second.json()
    assert set(payload_one.keys()) == set(payload_two.keys())
    assert payload_one["algorithm"] == "sha256"
    assert payload_one["verified_event_count"] >= 3
    assert payload_one["verified_event_count"] == payload_two["verified_event_count"]
    assert payload_one["verified_through_event_id"] == payload_two["verified_through_event_id"]


def test_replay_returns_integrity_metadata_consistently() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    correlation_id = "corr-integrity-replay-001"
    for index in range(2):
        response = client.post(
            "/audit/append",
            headers=headers,
            json={
                "event_type": "audit.integrity.replay",
                "user_id": str(user_id),
                "trace_id": f"trace-integrity-replay-{index}",
                "correlation_id": correlation_id,
                "idempotency_key": f"idem-integrity-replay-{index}-{uuid4()}",
            },
        )
        assert response.status_code == 200

    replay = client.get(
        f"/audit/replay/{correlation_id}?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )
    assert replay.status_code == 200
    payload = replay.json()
    assert payload["correlation_id"] == correlation_id
    assert all(isinstance(item["event_checksum"], str) for item in payload["events"])
    assert all("previous_event_checksum" in item for item in payload["events"])


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
