"""Deterministic storage download capability expiry and stale-link rejection tests."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.storage.app.main import create_app
from services.storage.app.errors import STORAGE_CAPABILITY_EXPIRED
from services.storage.app.errors import STORAGE_CAPABILITY_NOT_FOUND
from shared.determinism.input_hash import canonical_json_dumps
from services.storage.app.capability_tokens import StorageCapabilityService
from services.storage.app.capability_tokens import StorageCapabilityResolutionError


def test_storage_download_capability_valid_non_expired_succeeds() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        _seed_upload_object(client=client)
        response = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-valid"),
        )

    payload = _response_json(response)
    capability = _capability(payload)
    assert response.status_code == 201
    assert payload["status"] == "capability_issued"
    assert capability["capability_id"]
    assert capability["expires_at"]


def test_storage_download_capability_pre_expiry_boundary_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_REFERENCE_TIME", "2026-01-01T00:14:59+00:00")
    app = _fresh_app()
    with TestClient(app) as client:
        _seed_upload_object(client=client)
        response = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-pre-expiry"),
        )

    payload = _response_json(response)
    capability = _capability(payload)
    assert response.status_code == 201
    assert payload["status"] == "capability_issued"
    assert capability["capability_id"]
    assert capability["expires_at"]


def test_storage_download_capability_expired_is_rejected_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_REFERENCE_TIME", "2026-01-01T00:20:00+00:00")
    app = _fresh_app()
    with TestClient(app) as client:
        _seed_upload_object(client=client)
        first = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-expired"),
        )
        second = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-expired"),
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 410
    assert first_detail["error_code"] == STORAGE_CAPABILITY_EXPIRED
    assert first_detail["reason"] == STORAGE_CAPABILITY_EXPIRED
    assert first_detail["reason_code"] == STORAGE_CAPABILITY_EXPIRED
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_storage_download_capability_stale_id_is_rejected_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()
    service = _service(app)

    def _raise_not_found(*, capability_id: str) -> object:
        _ = capability_id
        raise StorageCapabilityResolutionError(
            reason_code=STORAGE_CAPABILITY_NOT_FOUND,
            message="Storage capability was not found.",
        )

    monkeypatch.setattr(service, "resolve_download_capability", _raise_not_found)
    with TestClient(app) as client:
        _seed_upload_object(client=client)
        first = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-stale"),
        )
        second = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-stale"),
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 404
    assert first_detail["error_code"] == STORAGE_CAPABILITY_NOT_FOUND
    assert first_detail["reason"] == STORAGE_CAPABILITY_NOT_FOUND
    assert first_detail["reason_code"] == STORAGE_CAPABILITY_NOT_FOUND
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def test_storage_download_capability_idempotency_replay_is_stable() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        _seed_upload_object(client=client)
        first = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-replay"),
        )
        second = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_headers("storage-download-replay"),
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    first_capability = _capability(first_payload)
    second_capability = _capability(second_payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first_payload["status"] == "capability_issued"
    assert second_payload["status"] == "capability_replayed"
    assert first_capability["capability_id"] == second_capability["capability_id"]
    assert first_capability["expires_at"] == second_capability["expires_at"]


def _fresh_app() -> FastAPI:
    return create_app()


def _service(app: FastAPI) -> StorageCapabilityService:
    configured = getattr(app.state, "storage_capability_service", None)
    assert isinstance(configured, StorageCapabilityService)
    return configured


def _seed_upload_object(*, client: TestClient) -> None:
    response = client.post(
        "/v1/storage/upload-capabilities",
        json=_upload_payload(),
        headers=_headers("storage-upload-seed"),
    )
    assert response.status_code == 201


def _upload_payload() -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "owner_user_id": "8f3e0730-4763-4f0e-9f66-df31f8f235f7",
        "object_key": "reports_income_tax_2023_report-a.json",
        "content_type": "application/json",
        "expected_size_bytes": 2048,
        "checksum_sha256": "a" * 64,
    }


def _download_payload() -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "owner_user_id": "8f3e0730-4763-4f0e-9f66-df31f8f235f7",
        "object_key": "reports_income_tax_2023_report-a.json",
    }


def _headers(idempotency_key: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer storage:test",
        "Idempotency-Key": idempotency_key,
        "X-Correlation-ID": f"{idempotency_key}-corr",
    }


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _capability(payload: dict[str, object]) -> dict[str, object]:
    capability = payload.get("capability")
    assert isinstance(capability, dict)
    return cast(dict[str, object], capability)


def _error_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_object = cast(dict[str, object], detail)
    assert {"error_code", "message", "reason", "reason_code"}.issubset(detail_object.keys())
    return detail_object
