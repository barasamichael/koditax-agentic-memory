"""Event-store authorization boundary tests for canonical auth context enforcement."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.event_store.app.main import create_app
from services.event_store.app.main import get_audit_events
from services.event_store.app.main import reset_audit_events


def test_event_store_allows_supported_role_and_tenant() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    response = client.post(
        "/audit/append",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(role="TaxAgent"),
            CORRELATION_ID_HEADER_NAME: "corr-event-store-authz-custom",
            TRACE_ID_HEADER_NAME: "trace-event-store-authz-custom",
        },
        json={
            "event_type": "audit.test",
            "user_id": str(uuid4()),
            "trace_id": "trace-event-store-authz-custom",
            "correlation_id": "corr-event-store-authz-001",
            "idempotency_key": _idempotency_key("idem-event-store-authz-001"),
        },
    )
    payload = cast(dict[str, object], response.json())
    assert response.status_code == 200
    assert "event_id" in payload
    assert len(get_audit_events()) == 1
    audit_record = get_audit_events()[0]
    assert response.headers[CORRELATION_ID_HEADER_NAME] == "corr-event-store-authz-custom"
    assert response.headers[TRACE_ID_HEADER_NAME] == "trace-event-store-authz-custom"
    assert audit_record.correlation_id == "corr-event-store-authz-custom"
    assert audit_record.trace_id == "trace-event-store-authz-custom"
    assert audit_record.is_delegated is False
    assert audit_record.principal_user_id is None
    assert audit_record.delegate_user_id is None
    assert audit_record.delegation_id is None
    assert audit_record.action_type == "audit.test"


def test_event_store_allows_active_delegation_and_emits_audit_linkage() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    delegation_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=delegation_id,
                granted_at=(datetime.now(UTC) - timedelta(minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
        json={
            "event_type": "audit.delegated.action",
            "user_id": str(delegate_user_id),
            "correlation_id": "corr-event-store-authz-001b",
            "idempotency_key": _idempotency_key("idem-event-store-authz-001b"),
        },
    )
    assert response.status_code == 200
    assert len(get_audit_events()) == 1
    audit_record = get_audit_events()[0]
    assert audit_record.trace_id
    assert audit_record.correlation_id
    assert audit_record.is_delegated is True
    assert audit_record.principal_user_id == principal_user_id
    assert audit_record.delegate_user_id == delegate_user_id
    assert audit_record.delegation_id == delegation_id
    assert audit_record.action_type == "audit.delegated.action"


def test_event_store_rejects_missing_auth_context_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    response = client.post(
        "/audit/append",
        json={
            "event_type": "audit.test",
            "user_id": str(uuid4()),
            "correlation_id": "corr-event-store-authz-002",
            "idempotency_key": "idem-event-store-authz-002",
        },
    )
    _assert_error_reason(
        response=response, expected_status=401, expected_reason="auth_context_missing"
    )
    assert len(get_audit_events()) == 0


def test_event_store_rejects_disallowed_role_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(role="Administrator")},
        json={
            "event_type": "audit.test",
            "user_id": str(uuid4()),
            "correlation_id": "corr-event-store-authz-003",
            "idempotency_key": "idem-event-store-authz-003",
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_role_forbidden",
    )
    assert len(get_audit_events()) == 0


def test_event_store_rejects_tenant_mismatch_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    response = client.post(
        "/audit/append",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                role="TaxAgent",
                tenant_id="other_tenant",
            )
        },
        json={
            "event_type": "audit.test",
            "user_id": str(uuid4()),
            "correlation_id": "corr-event-store-authz-004",
            "idempotency_key": "idem-event-store-authz-004",
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="authorization_tenant_forbidden",
    )
    assert len(get_audit_events()) == 0


def test_event_store_rejects_revoked_delegation_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
                revoked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        },
        json={
            "event_type": "audit.test",
            "user_id": str(delegate_user_id),
            "correlation_id": "corr-event-store-authz-004b",
            "idempotency_key": "idem-event-store-authz-004b",
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="delegation_revoked",
    )
    assert len(get_audit_events()) == 0


def test_event_store_rejects_delegated_tenant_mismatch_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="TaxAgent",
                tenant_id="other_tenant",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
        json={
            "event_type": "audit.test",
            "user_id": str(delegate_user_id),
            "correlation_id": "corr-event-store-authz-004c",
            "idempotency_key": "idem-event-store-authz-004c",
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="delegation_tenant_mismatch",
    )
    assert len(get_audit_events()) == 0


def test_event_store_rejects_delegation_role_forbidden_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    principal_user_id = uuid4()
    delegate_user_id = uuid4()
    response = client.post(
        "/audit/append",
        headers={
            AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(
                user_id=delegate_user_id,
                role="IndividualTaxpayer",
                is_delegated=True,
                principal_user_id=principal_user_id,
                delegate_user_id=delegate_user_id,
                delegation_id=uuid4(),
                granted_at=(datetime.now(UTC) - timedelta(minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        },
        json={
            "event_type": "audit.test",
            "user_id": str(delegate_user_id),
            "correlation_id": "corr-event-store-authz-004d",
            "idempotency_key": "idem-event-store-authz-004d",
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="delegation_role_forbidden",
    )
    assert len(get_audit_events()) == 0


def test_event_store_rejects_unsupported_auth_context_scope_deterministically() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    header_payload = json.loads(_build_auth_context_header(role="TaxAgent"))
    header_payload["schema_version"] = "2.0.0"
    response = client.post(
        "/audit/append",
        headers={AUTH_CONTEXT_HEADER_NAME: json.dumps(header_payload)},
        json={
            "event_type": "audit.test",
            "user_id": str(uuid4()),
            "correlation_id": "corr-event-store-authz-004e",
            "idempotency_key": "idem-event-store-authz-004e",
        },
    )
    _assert_error_reason(
        response=response,
        expected_status=403,
        expected_reason="unsupported_auth_context_scope",
    )
    assert len(get_audit_events()) == 0


def test_event_store_repeated_forbidden_input_is_deterministic() -> None:
    reset_audit_events()
    client = TestClient(create_app())
    headers = {
        AUTH_CONTEXT_HEADER_NAME: _build_auth_context_header(role="Administrator"),
    }
    payload = {
        "event_type": "audit.test",
        "user_id": str(uuid4()),
        "correlation_id": "corr-event-store-authz-005",
        "idempotency_key": "idem-event-store-authz-005",
    }
    first = cast(
        dict[str, object], client.post("/audit/append", headers=headers, json=payload).json()
    )["detail"]
    second = cast(
        dict[str, object], client.post("/audit/append", headers=headers, json=payload).json()
    )["detail"]
    first_error = cast(dict[str, object], first)
    second_error = cast(dict[str, object], second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]
    assert set(first_error.keys()) == set(second_error.keys())


def _assert_error_reason(
    *,
    response: httpx.Response,
    expected_status: int,
    expected_reason: str,
) -> None:
    status_code = response.status_code
    payload = cast(dict[str, object], response.json())
    detail = cast(dict[str, object], payload["detail"])
    assert status_code == expected_status
    assert detail["error_code"] == expected_reason
    assert detail["reason"] == expected_reason
    assert "trace_id" in detail
    assert "correlation_id" in detail


def _build_auth_context_header(
    *,
    user_id: UUID | None = None,
    role: str,
    tenant_id: str = "default_tenant",
    is_delegated: bool = False,
    principal_user_id: UUID | None = None,
    delegate_user_id: UUID | None = None,
    delegation_id: UUID | None = None,
    granted_at: str | None = None,
    revoked_at: str | None = None,
) -> str:
    subject_user_id = user_id or uuid4()
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "user_id": str(subject_user_id),
            "tenant_id": tenant_id,
            "role": role,
            "session_id": "11111111-2222-3333-4444-555555555555",
            "delegation_context": {
                "is_delegated": is_delegated,
                "principal_user_id": str(principal_user_id) if principal_user_id else None,
                "delegate_user_id": str(delegate_user_id) if delegate_user_id else None,
                "delegation_id": str(delegation_id) if delegation_id else None,
                "granted_at": granted_at,
                "revoked_at": revoked_at,
            },
        }
    )


def _idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"
