"""Canonical error taxonomy checks for orchestration runtime."""

from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.orchestration.app.audit_events import OrchestrationAuditEventStore
from services.orchestration.app.audit_events import OrchestrationAuditStoreError
from services.orchestration.app.audit_events import set_default_orchestration_audit_event_store
from services.orchestration.app.audit_events import reset_default_orchestration_audit_event_store
from services.orchestration.app.orchestration_errors import build_orchestration_error_envelope
from tests.orchestration_auth_support import orchestration_auth_headers


class _FailingAuditStore:
    def append(self, event: object) -> object:
        _ = event
        raise OrchestrationAuditStoreError(
            reason_code="audit_persistence_unavailable",
            message="Orchestration audit persistence is unavailable.",
        )

    def list(self, *, correlation_id: str | None = None) -> list[object]:
        _ = correlation_id
        return []

    def clear(self) -> None:
        return None


def test_error_envelope_builder_returns_canonical_fields() -> None:
    envelope = build_orchestration_error_envelope(
        correlation_id="corr-error-001",
        trace_id="trace-error-001",
        error_code="unsupported_prompt_scope",
        message="Prompt scope is unsupported.",
        reason="unsupported_domain",
        reason_code="unsupported_domain",
        context={"intent_class": "unsupported"},
    )

    assert envelope["error_code"] == "unsupported_prompt_scope"
    assert envelope["reason"] == "unsupported_domain"
    assert envelope["reason_code"] == "unsupported_domain"
    assert envelope["correlation_id"] == "corr-error-001"
    assert envelope["trace_id"] == "trace-error-001"


def test_invalid_request_payload_uses_canonical_error_taxonomy() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/v1/orchestration/prompt/ingest",
        headers={"X-Correlation-ID": "corr-error-invalid-001"},
        json={"tenant_id": "pilot_tenant_alpha"},
    )

    assert response.status_code == 400
    detail = cast(dict[str, object], response.json()["detail"])
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason"] == "invalid_orchestration_request"
    assert detail["reason_code"] == "invalid_orchestration_request"
    assert detail["correlation_id"] == "corr-error-invalid-001"


def test_unsupported_scope_remains_fail_closed_canonically() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={
            "X-Correlation-ID": "corr-error-unsupported-001",
            **orchestration_auth_headers(tenant_id="pilot_tenant_alpha"),
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-error-unsupported-001",
            "channel": "chat",
            "prompt": {"text": "Compute VAT for Q3.", "format": "plain_text"},
            "user_id": "user_error_001",
            "idempotency_key": "idem-error-unsupported-001-v2",
            "intent_class": "compute_income_tax",
            "tax_domain_hint": "income_tax",
            "decision_id": "a" * 64,
            "selected_route": {
                "route_id": "income_tax_compute_route_v1",
                "target_service": "tax_core",
                "target_operation": "execute_computation",
            },
        },
    )

    assert response.status_code == 400
    detail = cast(dict[str, object], response.json()["detail"])
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason"] in {"prompt_context_mismatch", "invalid_orchestration_request"}
    assert detail["reason_code"] in {"prompt_context_mismatch", "invalid_orchestration_request"}


def test_audit_persistence_failure_maps_to_canonical_runtime_error() -> None:
    set_default_orchestration_audit_event_store(
        cast(OrchestrationAuditEventStore, _FailingAuditStore())
    )
    try:
        client = TestClient(create_app())
        response = client.post(
            "/v1/orchestration/prompt/ingest",
            headers={"X-Correlation-ID": "corr-error-audit-001"},
            json={
                "tenant_id": "pilot_tenant_alpha",
                "conversation_id": "conv-error-audit-001",
                "channel": "chat",
                "prompt": {"text": "hello", "format": "plain_text"},
            },
        )
    finally:
        reset_default_orchestration_audit_event_store()

    assert response.status_code == 503
    detail = cast(dict[str, object], response.json()["detail"])
    assert detail["error_code"] == "audit_persistence_failure"
    assert detail["reason"] == "audit_persistence_unavailable"
    assert detail["reason_code"] == "audit_persistence_unavailable"
