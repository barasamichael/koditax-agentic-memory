"""Tamper-detection regression tests for event-store integrity verification."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events
from services.event_store.app.config import load_database_url
from services.event_store.app.repository import INTEGRITY_CHECK_FAILED


def test_integrity_verification_fails_deterministically_when_checksum_is_tampered() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    append = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.integrity.tamper",
            "user_id": str(user_id),
            "trace_id": "trace-integrity-tamper-001",
            "correlation_id": "corr-integrity-tamper-001",
            "idempotency_key": f"idem-integrity-tamper-{uuid4()}",
        },
    )
    assert append.status_code == 200
    event_id = append.json()["event_id"]

    _tamper_event_checksum(event_id=UUID(event_id))

    first = client.get(
        f"/audit/integrity/verify?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )
    second = client.get(
        f"/audit/integrity/verify?tenant_id=default_tenant&user_id={user_id}&limit=50",
        headers=headers,
    )

    assert first.status_code == 409
    assert second.status_code == 409
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == INTEGRITY_CHECK_FAILED
    assert first_detail["reason"] == INTEGRITY_CHECK_FAILED
    assert first_detail["reason_code"] == INTEGRITY_CHECK_FAILED
    assert second_detail["error_code"] == INTEGRITY_CHECK_FAILED
    assert second_detail["reason"] == INTEGRITY_CHECK_FAILED
    assert second_detail["reason_code"] == INTEGRITY_CHECK_FAILED
    assert set(first_detail.keys()) == set(second_detail.keys())


def _tamper_event_checksum(*, event_id: UUID) -> None:
    database_url = load_database_url()
    assert database_url is not None
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
            cursor.execute(
                "UPDATE audit_events SET event_hash = %s WHERE id = %s",
                ("0" * 64, event_id),
            )
            cursor.execute("SET session_replication_role = DEFAULT")
        connection.commit()


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
