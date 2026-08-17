"""Focused tests for immutable canonical reports audit event emission."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.audit import ReportsAuditEmitter
import services.reports.app.generation as generation_module
from services.reports.app.repository import ReportsRepository


def test_generation_success_emits_report_generated() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_generation_payload(),
            headers={
                "X-Correlation-ID": "audit-generation-success",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    assert response.status_code == 201
    events = _events(app)
    assert str(events[-1]["event_type"]) == "report_generated"
    assert {"event_id", "occurred_at", "correlation_id", "lineage"}.issubset(events[-1].keys())


def test_generation_failure_emits_report_generation_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()

    def _raise_failure(**_: object) -> object:
        raise generation_module.ReportPdfRenderingError(
            reason_code="report_rendering_failed",
            message="Failed to render report artifact as PDF.",
        )

    monkeypatch.setattr(generation_module, "render_report_pdf", _raise_failure)
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_generation_payload(),
            headers={
                "X-Correlation-ID": "audit-generation-failure",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    assert response.status_code == 503
    events = _events(app)
    failed = events[-1]
    assert str(failed["event_type"]) == "report_generation_failed"
    assert str(failed["error_code"]) == "report_rendering_failed"
    assert str(failed["reason_code"]) == "report_rendering_failed"
    assert str(failed["reason"]) == "report_rendering_failed"


def test_download_flow_emits_link_issued_and_downloaded_events() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app)
    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "audit-download-success",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    assert response.status_code == 200
    events = _events(app)
    assert str(events[-2]["event_type"]) == "report_download_link_issued"
    assert str(events[-1]["event_type"]) == "report_downloaded"


def test_emitted_events_are_append_only_immutable() -> None:
    app = _fresh_app()
    _seed_report(app=app)
    before = _events(app)
    _ = before[0]["event_type"]
    with TestClient(app) as client:
        client.post(
            "/v1/reports/income-tax/artifacts",
            json=_generation_payload(),
            headers={
                "X-Correlation-ID": "audit-append-only",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    after = _events(app)
    assert len(after) > len(before)
    assert after[: len(before)] == before


def test_repeated_same_scenario_has_same_required_event_schema() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        client.post(
            "/v1/reports/income-tax/artifacts",
            json=_generation_payload(),
            headers={
                "X-Correlation-ID": "audit-schema-repeat",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
        client.post(
            "/v1/reports/income-tax/artifacts",
            json=_generation_payload(),
            headers={
                "X-Correlation-ID": "audit-schema-repeat",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    events = _events(app)
    first = events[-2]
    second = events[-1]
    assert str(first["event_type"]) == "report_generated"
    assert str(second["event_type"]) == "report_generated"
    assert set(first.keys()) == set(second.keys())


def _fresh_app() -> FastAPI:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    repository.reset()
    emitter = getattr(app.state, "reports_audit_emitter", None)
    assert isinstance(emitter, ReportsAuditEmitter)
    return app


def _seed_report(*, app: FastAPI) -> str:
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_generation_payload(),
            headers={
                "X-Correlation-ID": "audit-seed-generation",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    payload = _response_json(response)
    assert response.status_code == 201
    return str(payload["report_id"])


def _generation_payload() -> dict[str, object]:
    return {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }


def _events(app: FastAPI) -> tuple[dict[str, object], ...]:
    emitter = getattr(app.state, "reports_audit_emitter", None)
    assert isinstance(emitter, ReportsAuditEmitter)
    return emitter.snapshot()


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
