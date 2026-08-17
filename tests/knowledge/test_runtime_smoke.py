from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import stable_headers
from tests.knowledge.support import admin_auth_headers
from services.knowledge.app.main import create_app
from services.knowledge.app.main import KNOWLEDGE_SERVICE_NAME
from services.knowledge.app.main import KNOWLEDGE_SERVICE_VERSION


def test_create_app_mounts_expected_runtime_routes() -> None:
    app = create_app()

    route_specs = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }

    assert ("GET", "/healthz") in route_specs
    assert ("GET", "/readyz") in route_specs
    assert ("POST", "/knowledge/search") in route_specs
    assert ("POST", "/knowledge/retrieve") in route_specs
    assert ("POST", "/knowledge/timeline/search") in route_specs


def test_healthz_returns_deterministic_service_envelope() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/healthz", headers=stable_headers("health"))

    payload = response_json(response)
    assert response.status_code == 200
    assert payload == {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "version": KNOWLEDGE_SERVICE_VERSION,
        "correlation_id": "health-corr",
        "trace_id": "health-trace",
    }


def test_readyz_returns_deterministic_service_envelope() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/readyz", headers=stable_headers("ready"))

    payload = response_json(response)
    assert response.status_code == 200
    assert payload == {
        "status": "ready",
        "service": KNOWLEDGE_SERVICE_NAME,
        "version": KNOWLEDGE_SERVICE_VERSION,
        "correlation_id": "ready-corr",
        "trace_id": "ready-trace",
    }


def test_protected_search_route_is_available_for_admin(client: TestClient) -> None:
    response = client.post(
        "/knowledge/search",
        json={"query": "allowable deductions"},
        headers=admin_auth_headers("search"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["service"] == KNOWLEDGE_SERVICE_NAME
    assert payload["correlation_id"] == "search-corr"
    assert payload["trace_id"] == "search-trace"
    assert result["total"] == 2


def test_protected_retrieval_route_rejects_missing_auth_context_deterministically(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/search",
        json={"query": "allowable deductions"},
        headers=stable_headers("auth-search"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 401
    assert detail["error_code"] == "auth_context_missing"
    assert detail["message"] == "Auth context header is required."
    assert detail["reason"] == "auth_context_missing"
    assert detail["correlation_id"] == "auth-search-corr"
    assert detail["trace_id"] == "auth-search-trace"


def test_protected_admin_mutation_route_rejects_missing_auth_context_deterministically(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/urls",
        json={
            "requested_by": "123e4567-e89b-12d3-a456-426614174000",
            "idempotency_key": "url-job-001",
            "url": "https://example.com/source",
        },
        headers=stable_headers("auth"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 401
    assert detail["error_code"] == "auth_context_missing"
    assert detail["message"] == "Auth context header is required."
    assert detail["reason"] == "auth_context_missing"
    assert detail["correlation_id"] == "auth-corr"
    assert detail["trace_id"] == "auth-trace"
