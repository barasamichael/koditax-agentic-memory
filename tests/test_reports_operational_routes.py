"""Operational route and canonical error behavior checks for reports runtime."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps


def test_reports_unknown_route_uses_canonical_error_envelope() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/not-a-real-route", headers={"X-Correlation-ID": "missing-corr"})

    payload = _response_json(response)
    detail = _extract_detail(payload)
    assert response.status_code == 404
    assert detail["error_code"] == "report_not_found"
    assert detail["reason"] == "report_not_found"
    assert detail["reason_code"] == "report_not_found"
    assert detail["correlation_id"] == "missing-corr"
    assert isinstance(detail["trace_id"], str)
    assert detail["trace_id"]


def test_reports_scaffold_income_tax_path_is_not_implemented_with_canonical_reason() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/reports/income-tax/artifacts",
            headers={"X-Correlation-ID": "income-tax-scaffold-corr"},
        )

    detail = _extract_detail(_response_json(response))
    assert response.status_code == 501
    assert detail["error_code"] == "report_generation_not_supported"
    assert detail["reason"] == "report_generation_not_supported"
    assert detail["reason_code"] == "report_generation_not_supported"


def test_reports_scaffold_rejects_unsupported_scope_fail_closed() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/reports/vat/artifacts",
            headers={"X-Correlation-ID": "reports-unsupported-corr"},
        )

    detail = _extract_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "unsupported_report_scope"
    assert detail["reason"] == "unsupported_report_scope"
    assert detail["reason_code"] == "unsupported_report_scope"


def test_reports_error_envelope_deterministic_on_repeated_requests() -> None:
    app = create_app()

    with TestClient(app) as client:
        first = client.get(
            "/v1/reports/income-tax/exports/123/metadata",
            headers={"X-Correlation-ID": "reports-repeat-corr"},
        )
        second = client.get(
            "/v1/reports/income-tax/exports/123/metadata",
            headers={"X-Correlation-ID": "reports-repeat-corr"},
        )

    first_detail = _extract_detail(_response_json(first))
    second_detail = _extract_detail(_response_json(second))
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_reports_correlation_id_header_is_propagated() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Correlation-ID": "reports-header-corr"})

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "reports-header-corr"


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _extract_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    required_keys = {"error_code", "message", "reason", "reason_code"}
    detail_object = cast(dict[str, object], detail)
    assert required_keys.issubset(detail_object.keys())
    return detail_object
