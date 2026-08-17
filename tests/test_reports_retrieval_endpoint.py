"""Deterministic tests for reports retrieval endpoint owner/tenant enforcement."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.reports.app.repository import ReportsRepository


def test_reports_retrieval_owner_and_tenant_success() -> None:
    app = _fresh_app()
    report_id = _generate_report(
        app=app,
        owner_user_id="owner-a",
        tenant_id="tenant-a",
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-success-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["report_id"] == report_id
    assert payload["report_version_id"]
    assert isinstance(payload["lineage_reference"], dict)
    download_capability_value = payload["download_capability"]
    assert isinstance(download_capability_value, dict)
    download_capability = _as_object(cast(dict[str, object], download_capability_value))
    assert download_capability["report_id"] == report_id
    assert download_capability["capability_id"]
    assert download_capability["download_url"]
    assert download_capability["expires_at"]
    assert "owner_user_id" not in payload
    assert "tenant_id" not in payload


def test_reports_retrieval_wrong_owner_same_tenant_forbidden() -> None:
    app = _fresh_app()
    report_id = _generate_report(
        app=app,
        owner_user_id="owner-a",
        tenant_id="tenant-a",
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-owner-denied-corr",
                "X-User-ID": "owner-b",
                "X-Tenant-ID": "tenant-a",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 403
    assert detail["error_code"] == "report_access_forbidden"
    assert detail["reason"] == "report_access_forbidden"
    assert detail["reason_code"] == "report_access_forbidden"


def test_reports_retrieval_cross_tenant_forbidden() -> None:
    app = _fresh_app()
    report_id = _generate_report(
        app=app,
        owner_user_id="owner-a",
        tenant_id="tenant-a",
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-tenant-denied-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-b",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 403
    assert detail["error_code"] == "report_access_forbidden"
    assert detail["reason"] == "report_access_forbidden"
    assert detail["reason_code"] == "report_access_forbidden"


def test_reports_retrieval_unknown_report_id_returns_not_found() -> None:
    app = _fresh_app()
    unknown_report_id = "11111111-1111-4111-8111-111111111111"

    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{unknown_report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-not-found-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{unknown_report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-not-found-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 404
    assert first_detail["error_code"] == "report_not_found"
    assert first_detail["reason"] == "report_not_found"
    assert first_detail["reason_code"] == "report_not_found"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_reports_retrieval_forbidden_response_is_deterministic() -> None:
    app = _fresh_app()
    report_id = _generate_report(
        app=app,
        owner_user_id="owner-a",
        tenant_id="tenant-a",
    )

    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-forbidden-corr",
                "X-User-ID": "owner-b",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-retrieve-forbidden-corr",
                "X-User-ID": "owner-b",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 403
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


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


def _generate_report(*, app: FastAPI, owner_user_id: str, tenant_id: str) -> str:
    payload = {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={
                "X-Correlation-ID": "reports-generate-for-retrieval-corr",
                "X-User-ID": owner_user_id,
                "X-Tenant-ID": tenant_id,
            },
        )
    response_payload = _response_json(response)
    assert response.status_code == 201
    return str(response_payload["report_id"])


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


def _as_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)
