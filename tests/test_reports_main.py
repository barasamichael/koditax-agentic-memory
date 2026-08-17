"""Runtime checks for reports app factory and operational endpoints."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.repository import ReportsRepository
from services.reports.app.logging_policy import get_default_report_structured_log_store
from services.reports.app.logging_policy import reset_default_report_structured_log_store


def test_reports_app_factory_returns_fastapi_app() -> None:
    reset_default_report_structured_log_store()
    app = create_app()
    assert app.title == "reports"
    assert isinstance(app.version, str)
    assert app.version


def test_reports_healthz_returns_deterministic_payload() -> None:
    reset_default_report_structured_log_store()
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Correlation-ID": "reports-health-corr"})

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload == {
        "status": "ok",
        "service": "reports",
        "version": payload["version"],
        "correlation_id": "reports-health-corr",
    }
    events = get_default_report_structured_log_store().snapshot()
    assert events
    assert events[-1]["event_type"] == "reports_request_succeeded"


def test_reports_readyz_returns_deterministic_payload() -> None:
    reset_default_report_structured_log_store()
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/readyz", headers={"X-Correlation-ID": "reports-ready-corr"})

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload == {
        "status": "ready",
        "service": "reports",
        "version": payload["version"],
        "correlation_id": "reports-ready-corr",
    }


def test_reports_health_domain_generation_executes_supported_governed_output() -> None:
    reset_default_report_structured_log_store()
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/health-contribution/artifacts",
            json={
                "computation_id": "bf80513f-f7dd-5257-9f4d-656eebc2c2f5",
                "form_id": "85bfa98d-e3e9-5829-aad6-047e7dc97f8c",
                "report_type": "health_contribution_summary",
                "tax_year": 2024,
                "historical_version_id": "HCH-VER-20241001-A",
                "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
            },
            headers={"X-Correlation-ID": "reports-health-domain-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["status"] == "generated"
    assert payload["report_type"] == "health_contribution_summary"
    lineage_reference = cast(dict[str, object], payload["lineage_reference"])
    assert lineage_reference["tax_type"] == "health_contribution"


def test_reports_unknown_tax_domain_fails_closed_with_invalid_domain_reason() -> None:
    reset_default_report_structured_log_store()
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/mystery-tax/artifacts",
            json={},
            headers={"X-Correlation-ID": "reports-invalid-domain-corr"},
        )

    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])
    assert response.status_code == 400
    assert detail["error_code"] == "invalid_tax_domain"
    assert detail["reason"] == "invalid_tax_domain"
    assert detail["reason_code"] == "invalid_tax_domain"


def test_reports_generation_fails_closed_when_governed_validation_rejects() -> None:
    reset_default_report_structured_log_store()
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/health-contribution/artifacts",
            json={
                "computation_id": "bf80513f-f7dd-5257-9f4d-656eebc2c2f5",
                "form_id": "85bfa98d-e3e9-5829-aad6-047e7dc97f8c",
                "report_type": "income_tax_summary",
                "tax_year": 2024,
                "historical_version_id": "HCH-VER-20241001-A",
                "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
            },
            headers={"X-Correlation-ID": "reports-governed-validation-block"},
        )

    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])
    context = cast(dict[str, object], detail["context"])
    governed_validation = cast(dict[str, object], context["governed_validation"])
    assert response.status_code == 409
    assert detail["error_code"] == "invalid_report_request"
    assert detail["reason"] == "invalid_report_request"
    assert governed_validation["validation_status"] == "rejected"
    issues = cast(list[dict[str, object]], governed_validation["issues"])
    assert issues[0]["code"] == "reports_validation_report_type_inconsistent"


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
