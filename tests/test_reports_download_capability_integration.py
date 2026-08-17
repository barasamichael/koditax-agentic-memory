"""Integration tests for reports-to-storage download capability issuance flow."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.audit import ReportsAuditEmitter
from services.reports.app.metrics import REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL
from services.reports.app.metrics import get_default_reports_metrics_emitter
from services.reports.app.metrics import REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL
from services.reports.app.metrics import reset_default_reports_metrics_emitter
from services.reports.app.repository import ReportsRepository
from services.storage.app.capability_tokens import StorageCapabilityService


def test_reports_download_capability_success_for_authorized_owner_tenant() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-success",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    payload = _response_json(response)
    capability = _as_object(payload["download_capability"])
    lineage = _as_object(payload["lineage_reference"])
    assert response.status_code == 200
    assert payload["report_id"] == report_id
    assert capability["report_id"] == report_id
    assert lineage["report_id"] == report_id
    assert lineage["report_version_id"] == payload["report_version_id"]
    assert capability["capability_id"]
    assert capability["download_url"]
    assert capability["expires_at"]
    events = _audit_events(app)
    assert str(events[-2]["event_type"]) == "report_download_link_issued"
    assert str(events[-1]["event_type"]) == "report_downloaded"
    assert str(events[-1]["report_id"]) == report_id
    assert _metric_count(metric_id=REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL) == 1


def test_reports_download_capability_unknown_report_returns_not_found() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        first = client.get(
            "/v1/reports/income-tax/artifacts/11111111-1111-4111-8111-111111111111/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-not-found",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            "/v1/reports/income-tax/artifacts/11111111-1111-4111-8111-111111111111/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-not-found",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 404
    assert detail["error_code"] == "report_not_found"
    assert detail["reason"] == "report_not_found"
    assert detail["reason_code"] == "report_not_found"
    assert detail == second_detail


def test_reports_download_capability_cross_tenant_forbidden() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-forbidden",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-b",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 403
    assert detail["error_code"] == "report_access_forbidden"
    assert detail["reason"] == "report_access_forbidden"
    assert detail["reason_code"] == "report_access_forbidden"


def test_reports_download_capability_wrong_owner_forbidden() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-owner-forbidden",
                "X-User-ID": "owner-b",
                "X-Tenant-ID": "tenant-a",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 403
    assert detail["error_code"] == "report_access_forbidden"
    assert detail["reason"] == "report_access_forbidden"
    assert detail["reason_code"] == "report_access_forbidden"


def test_reports_download_capability_storage_failure_maps_to_storage_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")
    storage_service = getattr(app.state, "storage_capability_service", None)
    assert isinstance(storage_service, StorageCapabilityService)

    def _raise_storage_failure(**_: object) -> object:
        raise RuntimeError("storage issuance unavailable")

    monkeypatch.setattr(storage_service, "issue_download_capability", _raise_storage_failure)
    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-storage-failure",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 503
    assert detail["error_code"] == "report_storage_unavailable"
    assert detail["reason"] == "report_storage_unavailable"
    assert detail["reason_code"] == "report_storage_unavailable"


def test_reports_download_capability_expired_artifact_maps_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORTS_REFERENCE_TIME", "2026-01-01T00:20:00+00:00")
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-expired",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 410
    assert detail["error_code"] == "report_artifact_expired"
    assert detail["reason"] == "report_artifact_expired"
    assert detail["reason_code"] == "report_artifact_expired"
    assert _metric_count(metric_id=REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL) == 1


def test_reports_download_capability_storage_expiry_maps_to_report_artifact_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORTS_REFERENCE_TIME", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("STORAGE_REFERENCE_TIME", "2026-01-01T00:20:00+00:00")
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-storage-expired",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 410
    assert detail["error_code"] == "report_artifact_expired"
    assert detail["reason"] == "report_artifact_expired"
    assert detail["reason_code"] == "report_artifact_expired"


def test_reports_download_capability_repeated_request_is_policy_consistent() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-determinism",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-download-capability-determinism",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    first_capability = _as_object(first_payload["download_capability"])
    second_capability = _as_object(second_payload["download_capability"])
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_capability["capability_id"] == second_capability["capability_id"]
    assert first_capability["expires_at"] == second_capability["expires_at"]
    assert first_capability["download_url"] == second_capability["download_url"]


def _fresh_app() -> FastAPI:
    reset_default_reports_metrics_emitter()
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = _repository(app)
    repository.reset()
    emitter = getattr(app.state, "reports_audit_emitter", None)
    assert isinstance(emitter, ReportsAuditEmitter)
    return app


def _repository(app: FastAPI) -> ReportsRepository:
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    return repository


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
                "X-Correlation-ID": "reports-download-capability-seed",
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


def _audit_events(app: FastAPI) -> tuple[dict[str, object], ...]:
    emitter = getattr(app.state, "reports_audit_emitter", None)
    assert isinstance(emitter, ReportsAuditEmitter)
    return emitter.snapshot()


def _metric_count(*, metric_id: str) -> int:
    return sum(
        1
        for event in get_default_reports_metrics_emitter().snapshot()
        if event.metric_id == metric_id
    )
