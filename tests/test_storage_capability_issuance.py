"""Deterministic tests for storage capability issuance runtime paths."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.storage.app.main import create_app


def test_storage_upload_capability_issuance_succeeds_with_required_fields() -> None:
    app = _fresh_app()
    payload = _upload_payload()
    headers = _capability_headers("storage-upload-idempotency")

    with TestClient(app) as client:
        response = client.post("/v1/storage/upload-capabilities", json=payload, headers=headers)

    response_payload = _response_json(response)
    capability = _capability(response_payload)
    assert response.status_code == 201
    assert response_payload["status"] == "capability_issued"
    assert capability["capability_id"]
    assert capability["object_key"] == payload["object_key"]
    assert capability["expires_at"]
    assert capability["method"] == "PUT"
    assert isinstance(capability["headers"], dict)


def test_storage_download_capability_issuance_succeeds_with_required_fields() -> None:
    app = _fresh_app()
    upload_payload = _upload_payload()
    with TestClient(app) as client:
        client.post(
            "/v1/storage/upload-capabilities",
            json=upload_payload,
            headers=_capability_headers("storage-upload-before-download"),
        )
        response = client.post(
            "/v1/storage/download-capabilities",
            json=_download_payload(),
            headers=_capability_headers("storage-download-idempotency"),
        )

    response_payload = _response_json(response)
    capability = _capability(response_payload)
    assert response.status_code == 201
    assert response_payload["status"] == "capability_issued"
    assert capability["capability_id"]
    assert capability["object_key"] == upload_payload["object_key"]
    assert capability["expires_at"]
    assert capability["method"] == "GET"


def test_storage_metadata_retrieval_succeeds_with_required_fields() -> None:
    app = _fresh_app()
    payload = _upload_payload()
    with TestClient(app) as client:
        client.post(
            "/v1/storage/upload-capabilities",
            json=payload,
            headers=_capability_headers("storage-upload-before-metadata"),
        )
        response = client.get(
            f"/v1/storage/objects/{payload['object_key']}/metadata",
            headers={
                "Authorization": "Bearer storage:test",
                "X-Correlation-ID": "storage-meta-corr",
            },
        )

    response_payload = _response_json(response)
    metadata = _metadata(response_payload)
    assert response.status_code == 200
    assert response_payload["status"] == "ok"
    assert metadata["object_key"] == payload["object_key"]
    assert metadata["tenant_id"] == payload["tenant_id"]
    assert metadata["owner_user_id"] == payload["owner_user_id"]
    assert metadata["content_type"] == payload["content_type"]
    assert metadata["size_bytes"] == payload["expected_size_bytes"]
    assert metadata["checksum_sha256"] == payload["checksum_sha256"]
    assert metadata["created_at"]


def test_storage_invalid_request_is_rejected_canonically() -> None:
    app = _fresh_app()
    payload = _upload_payload()
    payload.pop("checksum_sha256")

    with TestClient(app) as client:
        response = client.post(
            "/v1/storage/upload-capabilities",
            json=payload,
            headers=_capability_headers("storage-invalid-request"),
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 400
    assert detail["error_code"] == "invalid_storage_request"
    assert detail["reason"] == "invalid_storage_request"


def test_storage_unsupported_scope_is_rejected_canonically() -> None:
    app = _fresh_app()

    with TestClient(app) as client:
        response = client.get("/v1/storage/reports/unsupported")

    detail = _error_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "unsupported_storage_scope"
    assert detail["reason"] == "unsupported_storage_scope"


def test_storage_repeated_identical_request_is_deterministic() -> None:
    app = _fresh_app()
    payload = _upload_payload()
    headers = _capability_headers("storage-upload-determinism")

    with TestClient(app) as client:
        first = client.post("/v1/storage/upload-capabilities", json=payload, headers=headers)
        second = client.post("/v1/storage/upload-capabilities", json=payload, headers=headers)

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
    assert first_capability["method"] == second_capability["method"] == "PUT"


def _fresh_app() -> FastAPI:
    return create_app()


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


def _capability_headers(idempotency_key: str) -> dict[str, str]:
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
    capability_object = cast(dict[str, object], capability)
    assert {"capability_id", "object_key", "expires_at", "method", "headers"}.issubset(
        capability_object.keys()
    )
    return capability_object


def _metadata(payload: dict[str, object]) -> dict[str, object]:
    metadata = payload.get("metadata")
    assert isinstance(metadata, dict)
    required = {
        "object_key",
        "tenant_id",
        "owner_user_id",
        "content_type",
        "size_bytes",
        "checksum_sha256",
        "created_at",
    }
    metadata_object = cast(dict[str, object], metadata)
    assert required.issubset(metadata_object.keys())
    return metadata_object


def _error_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_object = cast(dict[str, object], detail)
    assert {"error_code", "message", "reason", "reason_code"}.issubset(detail_object.keys())
    return detail_object
