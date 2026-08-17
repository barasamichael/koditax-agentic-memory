"""Contract checks for Phase 8 gateway auth-context enforcement baseline."""

from __future__ import annotations

from typing import cast
from pathlib import Path

import yaml

from services.gateway.app.main import create_app

CONTRACT_PATH = Path("contracts/openapi/gateway.yaml")


def test_gateway_openapi_parses() -> None:
    document = _load_contract()
    assert document.get("openapi") == "3.1.0"


def test_gateway_openapi_contains_tools_ping_with_post_method() -> None:
    paths = _load_paths(_load_contract())
    assert "/tools/ping" in paths
    methods = set(paths["/tools/ping"])
    assert "post" in methods


def test_gateway_openapi_contains_tax_domain_scope_guard_path() -> None:
    paths = _load_paths(_load_contract())
    assert "/v1/gateway/{scope}/{remaining_path}" in paths
    methods = set(paths["/v1/gateway/{scope}/{remaining_path}"])
    assert {"get", "post", "put", "patch", "delete"}.issubset(methods)


def test_gateway_openapi_requires_auth_context_header_on_tools_ping() -> None:
    document = _load_contract()
    paths = _load_paths(document)
    post = cast(dict[str, object], paths["/tools/ping"]["post"])
    parameters = cast(list[object], post["parameters"])

    has_auth_context_header = False
    for parameter in parameters:
        parameter_ref = cast(dict[str, object], parameter).get("$ref")
        if parameter_ref == "#/components/parameters/AuthContextHeader":
            has_auth_context_header = True
            break

    assert has_auth_context_header is True


def test_gateway_openapi_declares_forbidden_response_for_authz_failures() -> None:
    paths = _load_paths(_load_contract())
    post = cast(dict[str, object], paths["/tools/ping"]["post"])
    responses = cast(dict[str, object], post["responses"])
    assert "403" in responses


def test_gateway_openapi_error_envelope_contains_required_fields() -> None:
    document = _load_contract()
    error_schema = _load_schema(document, "ErrorEnvelope")
    assert error_schema["type"] == "object"
    assert error_schema["additionalProperties"] is False
    required_list = cast(list[object], error_schema["required"])
    required = {str(item) for item in required_list}
    assert {
        "error_code",
        "message",
        "reason",
        "trace_id",
        "correlation_id",
    }.issubset(required)


def test_gateway_runtime_routes_match_required_openapi_surface() -> None:
    runtime_routes = _runtime_route_methods()
    assert "/tools/ping" in runtime_routes
    assert "post" in runtime_routes["/tools/ping"]
    assert "/v1/gateway/{scope}/{remaining_path:path}" in runtime_routes
    assert {"get", "post", "put", "patch", "delete"}.issubset(
        runtime_routes["/v1/gateway/{scope}/{remaining_path:path}"]
    )


def _load_contract() -> dict[str, object]:
    assert CONTRACT_PATH.exists()
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _load_paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    paths_value = document.get("paths")
    assert isinstance(paths_value, dict)
    typed_paths = cast(dict[str, object], paths_value)
    output: dict[str, dict[str, object]] = {}
    for path, path_item in typed_paths.items():
        assert isinstance(path, str)
        assert isinstance(path_item, dict)
        output[path] = cast(dict[str, object], path_item)
    return output


def _load_schema(document: dict[str, object], name: str) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    schemas = cast(dict[str, object], components.get("schemas", {}))
    schema = schemas.get(name)
    assert isinstance(schema, dict)
    return cast(dict[str, object], schema)


def _runtime_route_methods() -> dict[str, set[str]]:
    app = create_app()
    route_methods: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not isinstance(methods, set):
            continue
        normalized = {str(method).lower() for method in cast(set[object], methods)}
        route_methods.setdefault(path, set()).update(normalized)
    return route_methods
