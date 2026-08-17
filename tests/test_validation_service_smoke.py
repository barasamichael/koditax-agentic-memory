"""Smoke tests for validation runtime boundary."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.validation.app.main import create_app


def test_validation_app_boots_and_operational_routes_are_available() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)

    with TestClient(app) as client:
        health = client.get("/healthz", headers={"X-Correlation-ID": "val-health"})
        ready = client.get("/readyz", headers={"X-Correlation-ID": "val-ready"})

    health_payload = _json(health)
    ready_payload = _json(ready)
    assert health.status_code == 200
    assert ready.status_code == 200
    assert health_payload["service"] == "validation"
    assert ready_payload["service"] == "validation"
    assert health_payload["status"] == "ok"
    assert ready_payload["status"] == "ready"
    assert "runtime_mode" in health_payload
    assert "persistence_mode" in ready_payload


def test_validation_returns_deterministic_success_for_valid_payload() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-001",
        "tax_domain": "income_tax",
        "mode": "pre_submission",
        "fields": {
            "kra_pin": "A123456789B",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "amount_total": "100.00",
        },
    }
    with TestClient(app) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-smoke-income-tax"},
            json=payload,
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-smoke-income-tax"},
            json=payload,
        )

    first_payload = _json(first)
    second_payload = _json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    assert first_payload["audit_evidence"] == second_payload["audit_evidence"]
    assert cast(dict[str, object], first_payload["result"])["validation_status"] == "accepted"
    assert "rule_results" in cast(dict[str, object], first_payload["result"])


def test_validation_returns_deterministic_success_for_valid_health_contribution_payload() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-HC-SMOKE-001",
        "tax_domain": "health_contribution",
        "mode": "pre_submission",
        "fields": {
            "regime_identifier": "sha_shif",
            "resolved_domain_path": "sha_shif_non_salaried",
            "historical_version_id": "HCH-VER-20241001-A",
            "primary_effective_date": "2024-10-31",
            "contribution_basis_kes": "40000.00",
            "total_contribution_kes": "300.00",
        },
    }
    with TestClient(app) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-smoke-health"},
            json=payload,
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-smoke-health"},
            json=payload,
        )

    first_payload = _json(first)
    second_payload = _json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    assert first_payload["audit_evidence"] == second_payload["audit_evidence"]
    assert cast(dict[str, object], first_payload["result"])["validation_status"] == "accepted"


def test_validation_rejects_invalid_payload_with_canonical_error_shape() -> None:
    app = create_app()
    payload = {"return_id": "RET-001", "tax_domain": "income_tax", "mode": "unknown"}
    with TestClient(app) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-smoke-invalid"},
            json=payload,
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-smoke-invalid"},
            json=payload,
        )

    first_detail = cast(dict[str, object], _json(first)["detail"])
    second_detail = cast(dict[str, object], _json(second)["detail"])
    assert first.status_code == 400
    assert second.status_code == 400
    assert first_detail["error_code"] == second_detail["error_code"]
    assert first_detail["reason"] == second_detail["reason"]
    assert first_detail["reason_code"] == second_detail["reason_code"]
    assert (
        cast(dict[str, object], first_detail["context"])["audit_evidence"]
        == cast(dict[str, object], second_detail["context"])["audit_evidence"]
    )


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
