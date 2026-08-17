"""Fail-closed unsupported-scope and invalid-lineage tests for reports runtime."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.repository import ReportsRepository


def test_reports_recognized_tax_domain_scope_rejected_with_canonical_envelope() -> None:
    app = _fresh_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/reports/vat/artifacts",
            headers={"X-Correlation-ID": "reports-unsupported-scope-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 501
    assert detail["error_code"] == "unimplemented_tax_domain_report_generation"
    assert detail["reason"] == "unimplemented_tax_domain_report_generation"
    assert detail["reason_code"] == "unimplemented_tax_domain_report_generation"


def test_reports_generation_invalid_lineage_rejected_with_canonical_envelope() -> None:
    app = _fresh_app()
    payload = {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "11111111-1111-1111-1111-111111111111",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-invalid-lineage-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "invalid_lineage_reference"
    assert detail["reason"] == "invalid_lineage_reference"
    assert detail["reason_code"] == "invalid_lineage_reference"


def test_reports_generation_unsupported_version_context_rejected_canonically() -> None:
    app = _fresh_app()
    payload = {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20990101-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-unsupported-version-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "invalid_lineage_reference"
    assert detail["reason"] == "invalid_lineage_reference"
    assert detail["reason_code"] == "invalid_lineage_reference"


def _fresh_app() -> FastAPI:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = _repository(app)
    repository.reset()
    return app


def _repository(app: FastAPI) -> ReportsRepository:
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    return repository


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _error_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_object = cast(dict[str, object], detail)
    assert {"error_code", "message", "reason", "reason_code"}.issubset(detail_object.keys())
    return detail_object
