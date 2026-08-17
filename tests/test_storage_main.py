"""Deterministic tests for storage service app factory and operational routes."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.storage.app.main import create_app


def test_storage_create_app_returns_fastapi_instance() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_storage_healthz_and_readyz_return_deterministic_payload() -> None:
    app = create_app()
    with TestClient(app) as client:
        health = client.get("/healthz", headers={"X-Correlation-ID": "storage-health-corr"})
        ready = client.get("/readyz", headers={"X-Correlation-ID": "storage-ready-corr"})

    health_payload = _response_json(health)
    ready_payload = _response_json(ready)
    assert health.status_code == 200
    assert ready.status_code == 200
    assert health_payload["status"] == "ok"
    assert ready_payload["status"] == "ready"
    assert health_payload["service"] == "storage"
    assert ready_payload["service"] == "storage"
    assert health_payload["version"]
    assert ready_payload["version"]
    assert health_payload["correlation_id"] == "storage-health-corr"
    assert ready_payload["correlation_id"] == "storage-ready-corr"


def test_storage_unknown_route_returns_canonical_error_envelope() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/v1/storage/unknown/path")

    detail = _error_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "unsupported_storage_scope"
    assert detail["reason"] == "unsupported_storage_scope"
    assert detail["message"]


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
