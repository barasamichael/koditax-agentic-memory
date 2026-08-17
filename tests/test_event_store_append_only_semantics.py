"""Append-only and persistence-failure semantics for event-store runtime."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import reset_audit_events
from services.event_store.app.main import get_event_store_repository
from services.event_store.app.config import load_database_url
from services.event_store.app.repository import EventStoreRepository
from services.event_store.app.repository import PERSISTENCE_UNAVAILABLE
from services.event_store.app.repository import EventStoreRepositoryError


def test_append_only_storage_blocks_mutation_and_deletion() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)},
        json={
            "event_type": "audit.append.only",
            "user_id": str(user_id),
            "correlation_id": "corr-event-store-append-only-001",
            "idempotency_key": _idempotency_key("idem-event-store-append-only-001"),
        },
    )
    assert response.status_code == 200
    event_id = UUID(response.json()["event_id"])
    database_url = load_database_url()
    assert database_url is not None and database_url.strip()

    with psycopg.connect(database_url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    "UPDATE audit_events SET event_type = %s WHERE id = %s",
                    ("x", event_id),
                )
                mutation_error = ""
            except psycopg.Error as error:
                mutation_error = str(error)
                connection.rollback()
            try:
                cursor.execute("DELETE FROM audit_events WHERE id = %s", (event_id,))
                deletion_error = ""
            except psycopg.Error as error:
                deletion_error = str(error)
                connection.rollback()

    assert "append-only" in mutation_error.lower()
    assert "append-only" in deletion_error.lower()


def test_persistence_failure_returns_canonical_error_envelope() -> None:
    app = create_app()
    app.dependency_overrides[get_event_store_repository] = _build_failing_repository
    client = TestClient(app)
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=uuid4())},
        json={
            "event_type": "audit.persistence.failure",
            "user_id": str(uuid4()),
            "correlation_id": "corr-event-store-append-only-002",
            "idempotency_key": _idempotency_key("idem-event-store-append-only-002"),
        },
    )
    detail = response.json()["detail"]
    assert response.status_code == 503
    assert detail["error_code"] == PERSISTENCE_UNAVAILABLE
    assert detail["reason"] == PERSISTENCE_UNAVAILABLE
    assert detail["reason_code"] == PERSISTENCE_UNAVAILABLE
    expected_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "correlation_id",
        "trace_id",
    }
    assert expected_fields.issubset(detail.keys())


def test_repeated_append_shape_is_deterministic() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    user_id = uuid4()
    headers = {AUTH_CONTEXT_HEADER_NAME: _auth_header(role="TaxAgent", user_id=user_id)}
    first = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.append.shape",
            "user_id": str(user_id),
            "correlation_id": "corr-event-store-append-only-003",
            "idempotency_key": _idempotency_key("idem-event-store-append-only-003-a"),
        },
    ).json()
    second = client.post(
        "/audit/append",
        headers=headers,
        json={
            "event_type": "audit.append.shape",
            "user_id": str(user_id),
            "correlation_id": "corr-event-store-append-only-003",
            "idempotency_key": _idempotency_key("idem-event-store-append-only-003-b"),
        },
    ).json()
    assert set(first.keys()) == set(second.keys())
    assert "event_id" in first
    assert "event_id" in second
    assert first["correlation_id"] == second["correlation_id"]


class _FailingRepository(EventStoreRepository):
    def __init__(self) -> None:
        pass

    def append_event(self, **kwargs: object):  # type: ignore[override]
        _ = kwargs
        raise EventStoreRepositoryError(
            reason_code=PERSISTENCE_UNAVAILABLE,
            message="Event-store persistence is unavailable.",
        )


def _build_failing_repository() -> EventStoreRepository:
    return _FailingRepository()


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
