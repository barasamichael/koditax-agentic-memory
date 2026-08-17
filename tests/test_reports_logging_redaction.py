"""Structured logging redaction and schema tests for reports/storage boundary."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.storage.app.main import create_app as create_storage_app
from services.reports.app.repository import ReportsRepository
from services.reports.app.logging_policy import REDACTED_VALUE
from services.reports.app.logging_policy import emit_report_structured_log
from services.reports.app.logging_policy import get_default_report_structured_log_store
from services.reports.app.logging_policy import reset_default_report_structured_log_store


@pytest.fixture(autouse=True)
def _reset_structured_logs() -> None:
    reset_default_report_structured_log_store()


def test_success_path_emits_structured_log_envelope_fields() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Correlation-ID": "reports-log-success-corr"})

    assert response.status_code == 200
    event = _latest_log_event()
    assert event["service"] == "reports"
    assert event["event_type"] == "reports_request_succeeded"
    assert event["correlation_id"] == "reports-log-success-corr"
    assert {"timestamp", "level", "tenant_id", "report_id", "details"}.issubset(event.keys())
    details = _as_object(event["details"])
    assert details["method"] == "GET"
    assert details["path"] == "/healthz"
    assert details["status_code"] == 200


def test_error_log_includes_canonical_reason_code() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload.pop("supported_lane_id")

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-log-error-corr"},
        )

    assert response.status_code == 400
    events = get_default_report_structured_log_store().snapshot()
    error_events = [event for event in events if event["event_type"] == "reports_error_response"]
    assert error_events
    latest_error = error_events[-1]
    assert latest_error["reason_code"] == "invalid_lineage_reference"
    details = _as_object(latest_error["details"])
    assert details["status_code"] == 400


def test_sensitive_headers_fields_and_urls_are_redacted_deterministically() -> None:
    emit_report_structured_log(
        level="info",
        service="reports",
        event_type="reports_test_redaction",
        correlation_id="reports-log-redaction-corr",
        tenant_id="tenant-a",
        report_id="report-a",
        reason_code=None,
        details={
            "authorization": "Bearer abc.def.ghi",
            "capability_id": "cap_12345",
            "download_url": "https://example.com/download?token=abcd1234",
            "payload": {"raw": "should-not-leak"},
            "path": "/v1/reports/income-tax/artifacts",
        },
    )

    event = _latest_log_event()
    details = _as_object(event["details"])
    assert details["authorization"] == REDACTED_VALUE
    assert details["capability_id"] == REDACTED_VALUE
    assert details["download_url"] == REDACTED_VALUE
    assert details["payload"] == REDACTED_VALUE
    assert details["path"] == "/v1/reports/income-tax/artifacts"


def test_raw_payload_body_not_logged_in_request_lifecycle_logs() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=_valid_generation_payload(),
            headers={"X-Correlation-ID": "reports-log-no-payload-corr"},
        )

    assert response.status_code == 201
    event = _latest_log_event()
    details = _as_object(event["details"])
    assert "payload" not in details
    assert "body" not in details
    assert "json" not in details


def test_repeated_events_keep_same_schema_and_redaction_markers() -> None:
    emit_report_structured_log(
        level="info",
        service="reports",
        event_type="reports_test_repeatability",
        correlation_id="reports-log-repeat-corr",
        tenant_id="tenant-a",
        report_id=None,
        reason_code=None,
        details={"authorization": "Bearer token-one", "method": "GET"},
    )
    emit_report_structured_log(
        level="info",
        service="reports",
        event_type="reports_test_repeatability",
        correlation_id="reports-log-repeat-corr",
        tenant_id="tenant-a",
        report_id=None,
        reason_code=None,
        details={"authorization": "Bearer token-two", "method": "GET"},
    )

    events = get_default_report_structured_log_store().snapshot()
    first = events[-2]
    second = events[-1]
    assert set(first.keys()) == set(second.keys())
    first_details = _as_object(first["details"])
    second_details = _as_object(second["details"])
    assert set(first_details.keys()) == set(second_details.keys())
    assert first_details["authorization"] == REDACTED_VALUE
    assert second_details["authorization"] == REDACTED_VALUE


def test_storage_boundary_logs_redact_capability_artifacts() -> None:
    app = create_storage_app()
    with TestClient(app) as client:
        upload_response = client.post(
            "/v1/storage/upload-capabilities",
            json={
                "tenant_id": "tenant-a",
                "owner_user_id": "owner-a",
                "object_key": "reports_income_tax_2023_report-a.json",
                "content_type": "application/json",
                "expected_size_bytes": 512,
                "checksum_sha256": "a" * 64,
            },
            headers={
                "Authorization": "Bearer storage:test",
                "Idempotency-Key": "reports-log-storage-upload-idem",
                "X-Correlation-ID": "reports-log-storage-upload-corr",
            },
        )
        assert upload_response.status_code == 201
        download_response = client.post(
            "/v1/storage/download-capabilities",
            json={
                "tenant_id": "tenant-a",
                "owner_user_id": "owner-a",
                "object_key": "reports_income_tax_2023_report-a.json",
            },
            headers={
                "Authorization": "Bearer storage:test",
                "Idempotency-Key": "reports-log-storage-download-idem",
                "X-Correlation-ID": "reports-log-storage-download-corr",
            },
        )
    assert download_response.status_code == 201
    events = get_default_report_structured_log_store().snapshot()
    assert events
    storage_events = [event for event in events if event["service"] == "storage"]
    assert storage_events
    details = _as_object(storage_events[-1]["details"])
    assert details["capability_id"] == REDACTED_VALUE
    assert details["download_url"] == REDACTED_VALUE


def _fresh_app() -> Any:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    repository.reset()
    return app


def _valid_generation_payload() -> dict[str, object]:
    return {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }


def _latest_log_event() -> dict[str, object]:
    events = get_default_report_structured_log_store().snapshot()
    assert events
    return cast(dict[str, object], events[-1])


def _as_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


_RESET_STRUCTURED_LOGS_FIXTURE = _reset_structured_logs
