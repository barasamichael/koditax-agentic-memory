"""Determinism regression tests for reports runtime core endpoints."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.reports.app.repository import ReportsRepository


def test_reports_generation_repeated_request_is_deterministic() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()

    with TestClient(app) as client:
        first = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-determinism-generate-corr"},
        )
        second = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-determinism-generate-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_reports_retrieval_repeated_request_is_deterministic() -> None:
    app = _fresh_app()
    report_id = _generate_report(app=app, correlation_id="reports-determinism-seed-corr")

    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-determinism-retrieve-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-determinism-retrieve-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_reports_forbidden_and_not_found_error_shapes_are_deterministic() -> None:
    app = _fresh_app()
    report_id = _generate_report(app=app, correlation_id="reports-determinism-forbidden-seed-corr")

    with TestClient(app) as client:
        forbidden_first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-determinism-forbidden-corr",
                "X-User-ID": "owner-b",
                "X-Tenant-ID": "tenant-a",
            },
        )
        forbidden_second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-determinism-forbidden-corr",
                "X-User-ID": "owner-b",
                "X-Tenant-ID": "tenant-a",
            },
        )
        not_found_first = client.get(
            "/v1/reports/income-tax/artifacts/11111111-1111-4111-8111-111111111111/metadata",
            headers={
                "X-Correlation-ID": "reports-determinism-not-found-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        not_found_second = client.get(
            "/v1/reports/income-tax/artifacts/11111111-1111-4111-8111-111111111111/metadata",
            headers={
                "X-Correlation-ID": "reports-determinism-not-found-corr",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    forbidden_first_detail = _error_detail(_response_json(forbidden_first))
    forbidden_second_detail = _error_detail(_response_json(forbidden_second))
    assert forbidden_first.status_code == 403
    assert forbidden_second.status_code == 403
    assert canonical_json_dumps(forbidden_first_detail) == canonical_json_dumps(
        forbidden_second_detail
    )

    not_found_first_detail = _error_detail(_response_json(not_found_first))
    not_found_second_detail = _error_detail(_response_json(not_found_second))
    assert not_found_first.status_code == 404
    assert not_found_second.status_code == 404
    assert canonical_json_dumps(not_found_first_detail) == canonical_json_dumps(
        not_found_second_detail
    )


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


def _valid_generation_payload() -> dict[str, object]:
    return {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }


def _generate_report(*, app: FastAPI, correlation_id: str) -> str:
    payload = _valid_generation_payload()
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={
                "X-Correlation-ID": correlation_id,
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
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
