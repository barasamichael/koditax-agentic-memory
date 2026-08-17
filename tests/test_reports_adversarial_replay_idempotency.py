"""Adversarial, replay, and idempotency regression tests for reports flows."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.reports.app.repository import ReportsRepository


def test_adversarial_generation_payload_shape_rejected_deterministically() -> None:
    app = _fresh_app()
    adversarial_payload: object = ["not", "a", "json", "object"]
    with TestClient(app) as client:
        first = client.post(
            "/v1/reports/income-tax/artifacts",
            json=adversarial_payload,
            headers={"X-Correlation-ID": "reports-adversarial-payload-shape"},
        )
        second = client.post(
            "/v1/reports/income-tax/artifacts",
            json=adversarial_payload,
            headers={"X-Correlation-ID": "reports-adversarial-payload-shape"},
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 400
    assert first_detail["error_code"] == "invalid_report_request"
    assert first_detail["reason"] == "invalid_report_request"
    assert first_detail["reason_code"] == "invalid_report_request"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_adversarial_report_id_path_rejected_canonically_and_deterministically() -> None:
    app = _fresh_app()
    malicious_report_id = "not-a-uuid-id"
    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{malicious_report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-adversarial-id",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{malicious_report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-adversarial-id",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 400
    assert first_detail["error_code"] == "invalid_report_request"
    assert first_detail["reason"] == "invalid_report_request"
    assert first_detail["reason_code"] == "invalid_report_request"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_replay_download_request_after_expiry_is_rejected_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORTS_REFERENCE_TIME", "2026-01-01T00:20:00+00:00")
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")
    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-replay-after-expiry",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-replay-after-expiry",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 410
    assert first_detail["error_code"] == "report_artifact_expired"
    assert first_detail["reason"] == "report_artifact_expired"
    assert first_detail["reason_code"] == "report_artifact_expired"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_replay_forbidden_download_access_is_stable() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")
    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-replay-forbidden",
                "X-User-ID": "attacker-user",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-replay-forbidden",
                "X-User-ID": "attacker-user",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 403
    assert first_detail["error_code"] == "report_access_forbidden"
    assert first_detail["reason"] == "report_access_forbidden"
    assert first_detail["reason_code"] == "report_access_forbidden"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def _fresh_app() -> FastAPI:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    repository.reset()
    return app


def _seed_report(*, app: FastAPI, owner_user_id: str, tenant_id: str) -> str:
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
                "X-Correlation-ID": "reports-adversarial-seed",
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
