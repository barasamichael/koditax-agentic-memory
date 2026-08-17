"""Focused audit and internal-boundary tests for validation closeout."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi.testclient import TestClient

from services.validation.app.main import create_app
from services.validation.app.config import VALIDATION_INTERNAL_API_KEY_HEADER
from services.validation.app.config import VALIDATION_INTERNAL_API_KEY_ENV_VAR
from services.validation.app.audit_events import ValidationAuditEvent


def test_successful_validation_execution_emits_accepted_audit_event() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-accepted"},
            json={
                "return_id": "RET-AUD-001",
                "tax_domain": "income_tax",
                "mode": "pre_submission",
                "fields": {
                    "kra_pin": "A123456789B",
                    "period_start": "2024-01-01",
                    "period_end": "2024-12-31",
                    "amount_total": "100.00",
                },
            },
        )

    payload = _json(response)
    evidence = cast(dict[str, object], payload["audit_evidence"])
    events = _events(app=app, correlation_id="validation-audit-accepted")
    assert response.status_code == 200
    assert evidence["event_type"] == "validation_execution_accepted"
    assert len(events) == 1
    assert events[0]["event_type"] == "validation_execution_accepted"


def test_rejected_validation_execution_emits_rejected_audit_event() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-rejected"},
            json={
                "return_id": "RET-AUD-002",
                "tax_domain": "income_tax",
                "mode": "pre_submission",
                "fields": {
                    "kra_pin": "INVALID",
                    "period_start": "2024-12-31",
                    "period_end": "2024-01-01",
                    "amount_total": "100.123",
                },
            },
        )

    payload = _json(response)
    evidence = cast(dict[str, object], payload["audit_evidence"])
    result = cast(dict[str, object], payload["result"])
    events = _events(app=app, correlation_id="validation-audit-rejected")
    assert response.status_code == 200
    assert result["validation_status"] == "rejected"
    assert evidence["event_type"] == "validation_execution_rejected"
    assert len(events) == 1
    assert events[0]["event_type"] == "validation_execution_rejected"


def test_unsupported_domain_rejection_emits_request_rejected_audit_event() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-unsupported"},
            json={
                "return_id": "RET-AUD-003",
                "tax_domain": "vat",
                "mode": "draft",
                "fields": {},
            },
        )

    detail = cast(dict[str, object], _json(response)["detail"])
    context = cast(dict[str, object], detail["context"])
    evidence = cast(dict[str, object], context["audit_evidence"])
    events = _events(app=app, correlation_id="validation-audit-unsupported")
    assert response.status_code == 404
    assert evidence["event_type"] == "validation_request_rejected"
    assert len(events) == 1
    assert events[0]["event_type"] == "validation_request_rejected"
    assert events[0]["error_code"] == "unsupported_validation_scope"


def test_invalid_request_rejection_emits_request_rejected_audit_event() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-invalid"},
            json={
                "return_id": "RET-AUD-004",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": "bad-shape",
            },
        )

    detail = cast(dict[str, object], _json(response)["detail"])
    context = cast(dict[str, object], detail["context"])
    evidence = cast(dict[str, object], context["audit_evidence"])
    events = _events(app=app, correlation_id="validation-audit-invalid")
    assert response.status_code == 400
    assert evidence["event_type"] == "validation_request_rejected"
    assert len(events) == 1
    assert events[0]["error_code"] == "invalid_validation_request"


def test_persistence_unavailable_emits_failed_audit_event(monkeypatch: Any) -> None:
    monkeypatch.setenv("VALIDATION_RUNTIME_MODE", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid dsn")
    monkeypatch.setenv(VALIDATION_INTERNAL_API_KEY_ENV_VAR, "validation-internal-secret")
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={
                "X-Correlation-ID": "validation-audit-persistence",
                VALIDATION_INTERNAL_API_KEY_HEADER: "validation-internal-secret",
            },
            json={
                "return_id": "RET-AUD-005",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )

    detail = cast(dict[str, object], _json(response)["detail"])
    context = cast(dict[str, object], detail["context"])
    evidence = cast(dict[str, object], context["audit_evidence"])
    events = _events(app=app, correlation_id="validation-audit-persistence")
    assert response.status_code == 503
    assert evidence["event_type"] == "validation_execution_failed"
    assert len(events) == 1
    assert events[0]["event_type"] == "validation_execution_failed"


def test_repeated_identical_rejected_boundary_requests_are_deterministic(monkeypatch: Any) -> None:
    monkeypatch.setenv("VALIDATION_RUNTIME_MODE", "production")
    monkeypatch.setenv(VALIDATION_INTERNAL_API_KEY_ENV_VAR, "validation-internal-secret")
    app = create_app()

    with TestClient(app) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-boundary"},
            json={
                "return_id": "RET-AUD-006",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-boundary"},
            json={
                "return_id": "RET-AUD-006",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )

    first_detail = cast(dict[str, object], _json(first)["detail"])
    second_detail = cast(dict[str, object], _json(second)["detail"])
    assert first.status_code == 403
    assert second.status_code == 403
    assert first_detail == second_detail
    assert len(_events(app=app, correlation_id="validation-audit-boundary")) == 1


def _events(*, app: Any, correlation_id: str) -> list[ValidationAuditEvent]:
    return cast(
        list[ValidationAuditEvent],
        app.state.validation_audit_store.list(correlation_id=correlation_id),
    )


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
