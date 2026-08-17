"""Deterministic retention cleanup-hook tests for storage runtime."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.storage.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.storage.app.repository import StorageRetentionRepository


def test_storage_upload_persists_retention_metadata() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/storage/upload-capabilities",
            json=_upload_payload(object_key="reports_income_tax_2023_report-a.json"),
            headers=_headers("storage-retention-upload"),
        )
    assert response.status_code == 201
    repository = _retention_repository(app)
    record = repository.get_record(object_key="reports_income_tax_2023_report-a.json")
    assert record is not None
    assert record.retention_class == "export_bundle"
    assert record.retention_expires_at
    assert record.cleanup_status == "active"


def test_storage_cleanup_hook_selects_expired_records_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_REFERENCE_TIME", "2030-01-01T00:00:00+00:00")
    app = _fresh_app()
    with TestClient(app) as client:
        _upload(client=client, object_key="reports_income_tax_2023_report-b.json", seed="seed-b")
        _upload(client=client, object_key="reports_income_tax_2023_report-a.json", seed="seed-a")
        response = client.post(
            "/v1/storage/internal/retention/cleanup-hooks/run",
            json={"limit": 50},
            headers={"X-Correlation-ID": "storage-cleanup-run"},
        )

    payload = _response_json(response)
    summary = _summary(payload)
    assert response.status_code == 200
    assert summary["processed"] == 2
    assert summary["skipped"] == 0
    assert summary["failed"] == 0
    processed_items = cast(list[dict[str, object]], summary["processed_items"])
    assert [str(item["object_key"]) for item in processed_items] == [
        "reports_income_tax_2023_report-a.json",
        "reports_income_tax_2023_report-b.json",
    ]


def test_storage_cleanup_hook_is_idempotent_on_repeated_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_REFERENCE_TIME", "2030-01-01T00:00:00+00:00")
    app = _fresh_app()
    with TestClient(app) as client:
        _upload(client=client, object_key="reports_income_tax_2023_report-a.json", seed="seed-a")
        first = client.post(
            "/v1/storage/internal/retention/cleanup-hooks/run",
            json={"limit": 50},
            headers={"X-Correlation-ID": "storage-cleanup-idempotent"},
        )
        second = client.post(
            "/v1/storage/internal/retention/cleanup-hooks/run",
            json={"limit": 50},
            headers={"X-Correlation-ID": "storage-cleanup-idempotent"},
        )

    first_summary = _summary(_response_json(first))
    second_summary = _summary(_response_json(second))
    assert first_summary["processed"] == 1
    assert second_summary["processed"] == 0
    assert second_summary["skipped"] == 1


def test_storage_cleanup_non_eligible_record_is_rejected_canonically() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        _upload(client=client, object_key="reports_income_tax_2023_report-a.json", seed="seed-a")
        first = client.post(
            "/v1/storage/internal/retention/cleanup-hooks/reports_income_tax_2023_report-a.json",
            headers={"X-Correlation-ID": "storage-cleanup-not-eligible"},
        )
        second = client.post(
            "/v1/storage/internal/retention/cleanup-hooks/reports_income_tax_2023_report-a.json",
            headers={"X-Correlation-ID": "storage-cleanup-not-eligible"},
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 409
    assert first_detail["error_code"] == "cleanup_not_eligible"
    assert first_detail["reason"] == "cleanup_not_eligible"
    assert first_detail["reason_code"] == "cleanup_not_eligible"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_storage_cleanup_failure_maps_to_canonical_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_REFERENCE_TIME", "2030-01-01T00:00:00+00:00")
    app = _fresh_app()
    failing_key = "fail-cleanup-reports-income-tax-report.json"
    with TestClient(app) as client:
        _upload(client=client, object_key=failing_key, seed="seed-fail")
        first = client.post(
            f"/v1/storage/internal/retention/cleanup-hooks/{failing_key}",
            headers={"X-Correlation-ID": "storage-cleanup-fail"},
        )
        second = client.post(
            f"/v1/storage/internal/retention/cleanup-hooks/{failing_key}",
            headers={"X-Correlation-ID": "storage-cleanup-fail"},
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 503
    assert first_detail["error_code"] == "storage_cleanup_failed"
    assert first_detail["reason"] == "storage_cleanup_failed"
    assert first_detail["reason_code"] == "storage_cleanup_failed"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def _fresh_app() -> FastAPI:
    return create_app()


def _retention_repository(app: FastAPI) -> StorageRetentionRepository:
    repository = getattr(app.state, "storage_retention_repository", None)
    assert isinstance(repository, StorageRetentionRepository)
    return repository


def _upload(*, client: TestClient, object_key: str, seed: str) -> None:
    response = client.post(
        "/v1/storage/upload-capabilities",
        json=_upload_payload(object_key=object_key),
        headers=_headers(seed),
    )
    assert response.status_code == 201


def _upload_payload(*, object_key: str) -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "owner_user_id": "8f3e0730-4763-4f0e-9f66-df31f8f235f7",
        "object_key": object_key,
        "content_type": "application/json",
        "expected_size_bytes": 2048,
        "checksum_sha256": "a" * 64,
    }


def _headers(seed: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer storage:test",
        "Idempotency-Key": f"{seed}-idempotency",
        "X-Correlation-ID": f"{seed}-corr",
    }


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _summary(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("summary")
    assert isinstance(summary, dict)
    return cast(dict[str, object], summary)


def _error_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_object = cast(dict[str, object], detail)
    assert {"error_code", "message", "reason", "reason_code"}.issubset(detail_object.keys())
    return detail_object
