from __future__ import annotations

from typing import Any
from typing import cast
from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from services.knowledge.app.main import create_app

CONTRACT_PATH = Path("contracts/openapi/knowledge.yaml")
PUBLIC_ROUTE_SPECS = {
    ("GET", "/healthz"),
    ("GET", "/readyz"),
}
ADMIN_ROUTE_SPECS = {
    ("POST", "/knowledge/search"),
    ("POST", "/knowledge/retrieve"),
    ("POST", "/knowledge/timeline/search"),
    ("POST", "/knowledge/ingestion/files"),
    ("POST", "/knowledge/ingestion/documents"),
    ("POST", "/knowledge/ingestion/urls"),
    ("GET", "/knowledge/ingestion"),
    ("GET", "/knowledge/ingestion/{ingestion_job_id}"),
    ("POST", "/knowledge/ingestion/{ingestion_job_id}/approve"),
    ("POST", "/knowledge/ingestion/{ingestion_job_id}/publish"),
    ("POST", "/knowledge/ingestion/{ingestion_job_id}/metadata-correction"),
    ("GET", "/knowledge/source-versions"),
    ("POST", "/knowledge/source-versions/{source_version_id}/supersede"),
    ("POST", "/knowledge/source-versions/{source_version_id}/archive"),
    ("GET", "/knowledge/sources"),
    ("GET", "/knowledge/sources/{source_id}"),
    ("GET", "/knowledge/anchors/{anchor_id}"),
}


def test_openapi_documents_current_required_public_and_admin_routes() -> None:
    contract = _load_contract()
    paths = require_mapping(contract["paths"])
    documented_specs = {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in require_mapping(operations).keys()
    }

    assert PUBLIC_ROUTE_SPECS <= documented_specs
    assert ADMIN_ROUTE_SPECS <= documented_specs


def test_openapi_required_routes_match_runtime_route_surface() -> None:
    app = create_app()
    runtime_specs = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }

    assert PUBLIC_ROUTE_SPECS <= runtime_specs
    assert ADMIN_ROUTE_SPECS <= runtime_specs


def test_openapi_documents_internal_auth_boundary() -> None:
    contract = _load_contract()
    paths = require_mapping(contract["paths"])

    for method, path in PUBLIC_ROUTE_SPECS:
        operation = require_mapping(require_mapping(paths[path])[method.lower()])
        assert not _has_auth_context_parameter(operation)

    for method, path in ADMIN_ROUTE_SPECS:
        operation = require_mapping(require_mapping(paths[path])[method.lower()])
        assert _has_auth_context_parameter(operation)


def test_openapi_error_envelope_includes_canonical_error_fields() -> None:
    contract = _load_contract()
    schemas = require_mapping(require_mapping(contract["components"])["schemas"])
    error_envelope = require_mapping(schemas["ErrorEnvelope"])
    properties = require_mapping(error_envelope["properties"])

    assert "error_code" in properties
    assert "message" in properties
    assert "reason" in properties


def test_openapi_documents_current_public_request_boundary_limits() -> None:
    contract = _load_contract()
    schemas = require_mapping(require_mapping(contract["components"])["schemas"])
    search_request = require_mapping(schemas["KnowledgeSearchRequest"])
    retrieve_request = require_mapping(schemas["KnowledgeRetrieveRequest"])
    search_properties = require_mapping(search_request["properties"])
    retrieve_properties = require_mapping(retrieve_request["properties"])
    source_ids = require_mapping(retrieve_properties["source_ids"])
    source_items = require_mapping(source_ids["items"])
    anchor_ids = require_mapping(retrieve_properties["anchor_ids"])
    anchor_items = require_mapping(anchor_ids["items"])

    assert search_properties["query"]["maxLength"] == 512
    assert source_ids["maxItems"] == 50
    assert source_items["maxLength"] == 255
    assert anchor_ids["maxItems"] == 50
    assert anchor_items["maxLength"] == 255


def _load_contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return require_mapping(loaded)


def _has_auth_context_parameter(operation: dict[str, object]) -> bool:
    parameters = operation.get("parameters")
    if not isinstance(parameters, list):
        return False
    parameter_items = cast(list[object], parameters)
    for item in parameter_items:
        if item == {"$ref": "#/components/parameters/AuthContextHeader"}:
            return True
    return False


def require_mapping(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)
