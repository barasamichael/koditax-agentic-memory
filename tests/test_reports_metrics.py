"""Deterministic reports metrics baseline coverage tests."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.metrics import REPORTS_GENERATION_TOTAL
from services.reports.app.metrics import ALLOWED_METRIC_DIMENSIONS
from services.reports.app.metrics import ReportsMetricsPolicyError
from services.reports.app.metrics import REPORTS_GENERATION_LATENCY_MS
from services.reports.app.metrics import REPORTS_GENERATION_FAILURES_TOTAL
from services.reports.app.metrics import REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL
from services.reports.app.metrics import get_default_reports_metrics_emitter
from services.reports.app.metrics import REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL
from services.reports.app.metrics import reset_default_reports_metrics_emitter
from services.reports.app.repository import ReportsRepository


@pytest.fixture(autouse=True)
def _reset_reports_metrics() -> None:
    reset_default_reports_metrics_emitter()


def test_generation_success_emits_latency_and_success_counter() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_valid_generation_payload(),
            headers={"X-Correlation-ID": "reports-metrics-generation-success"},
        )

    assert response.status_code == 201
    events = get_default_reports_metrics_emitter().snapshot()
    generation_total_event = _single_metric_event(
        events=events,
        metric_id=REPORTS_GENERATION_TOTAL,
    )
    latency_event = _single_metric_event(
        events=events,
        metric_id=REPORTS_GENERATION_LATENCY_MS,
    )
    assert generation_total_event.dimensions["status"] == "success"
    assert (
        generation_total_event.dimensions["supported_lane_id"]
        == "resident_employment_income_2023_07_01"
    )
    assert generation_total_event.dimensions["historical_version_id"] == "KIT-VER-20230701-A"
    assert latency_event.value >= 0
    assert latency_event.dimensions["supported_lane_id"] == "resident_employment_income_2023_07_01"
    assert latency_event.dimensions["historical_version_id"] == "KIT-VER-20230701-A"


def test_generation_failure_emits_reason_coded_failure_counter() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload.pop("supported_lane_id")
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-metrics-generation-failure"},
        )

    assert response.status_code == 400
    events = get_default_reports_metrics_emitter().snapshot()
    generation_total_event = _single_metric_event(
        events=events,
        metric_id=REPORTS_GENERATION_TOTAL,
    )
    generation_failure_event = _single_metric_event(
        events=events,
        metric_id=REPORTS_GENERATION_FAILURES_TOTAL,
    )
    assert generation_total_event.dimensions["status"] == "failure"
    assert generation_failure_event.dimensions["reason_code"] == "invalid_lineage_reference"


def test_download_link_issuance_emits_counter() -> None:
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-metrics-download-issued",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )

    assert response.status_code == 200
    events = get_default_reports_metrics_emitter().snapshot()
    issued_event = _single_metric_event(
        events=events,
        metric_id=REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL,
    )
    assert issued_event.dimensions == {
        "event_type": "report_download_link_issued",
        "status": "success",
    }


def test_download_expiry_rejection_emits_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORTS_REFERENCE_TIME", "2026-01-01T00:20:00+00:00")
    app = _fresh_app()
    report_id = _seed_report(app=app, owner_user_id="owner-a", tenant_id="tenant-a")
    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers={
                "X-Correlation-ID": "reports-metrics-download-expired",
                "X-User-ID": "owner-a",
                "X-Tenant-ID": "tenant-a",
            },
        )
    assert response.status_code == 410
    events = get_default_reports_metrics_emitter().snapshot()
    expiry_event = _single_metric_event(
        events=events,
        metric_id=REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL,
    )
    assert expiry_event.dimensions["event_type"] == "report_downloaded"
    assert expiry_event.dimensions["reason_code"] == "report_artifact_expired"


def test_non_blocking_metrics_failure_does_not_break_generation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()
    emitter = get_default_reports_metrics_emitter()

    def _raise_emitter_failure(**_: object) -> None:
        raise RuntimeError("metrics sink unavailable")

    monkeypatch.setattr(emitter, "increment_counter", _raise_emitter_failure)
    monkeypatch.setattr(emitter, "observe_histogram", _raise_emitter_failure)
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_valid_generation_payload(),
            headers={"X-Correlation-ID": "reports-metrics-non-blocking"},
        )

    assert response.status_code == 201


def test_metrics_label_guardrails_reject_sensitive_or_unsupported_dimensions() -> None:
    emitter = get_default_reports_metrics_emitter()
    with pytest.raises(ReportsMetricsPolicyError):
        emitter.increment_counter(
            REPORTS_GENERATION_TOTAL,
            dimensions={"email": "person@example.com"},
        )
    with pytest.raises(ReportsMetricsPolicyError):
        emitter.observe_histogram(
            REPORTS_GENERATION_LATENCY_MS,
            value=1.0,
            dimensions={"status": "Bearer abc.def.ghi"},
        )


def test_metrics_dimensions_stay_within_governed_low_cardinality_set() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_valid_generation_payload(),
            headers={"X-Correlation-ID": "reports-metrics-dimensions"},
        )
    assert response.status_code == 201
    events = get_default_reports_metrics_emitter().snapshot()
    for event in events:
        assert set(event.dimensions).issubset(ALLOWED_METRIC_DIMENSIONS)


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


def _seed_report(*, app: FastAPI, owner_user_id: str, tenant_id: str) -> str:
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_valid_generation_payload(),
            headers={
                "X-Correlation-ID": "reports-metrics-seed",
                "X-User-ID": owner_user_id,
                "X-Tenant-ID": tenant_id,
            },
        )
    response_payload = _response_json(response)
    assert response.status_code == 201
    return str(response_payload["report_id"])


def _single_metric_event(*, events: tuple[Any, ...], metric_id: str) -> Any:
    matches = [event for event in events if getattr(event, "metric_id", "") == metric_id]
    assert len(matches) == 1
    return matches[0]


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


_RESET_REPORTS_METRICS_FIXTURE = _reset_reports_metrics
