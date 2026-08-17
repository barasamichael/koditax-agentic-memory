"""OpenAPI contract checks for validation runtime boundary."""

from __future__ import annotations

from typing import cast
from pathlib import Path

import yaml

from services.validation.app.main import create_app

CONTRACT_PATH = Path("contracts/openapi/validation.yaml")
REQUIRED_PATHS = {"/healthz", "/readyz", "/validate/return"}
REQUIRED_SCHEMAS = {
    "ErrorEnvelope",
    "ValidationAuditEvidence",
    "ValidateReturnRequest",
    "ValidateReturnResponse",
    "ValidationRuleResult",
}


def test_validation_openapi_contract_parses_and_declares_required_paths() -> None:
    document = _load_contract()
    assert document.get("openapi") == "3.1.0"
    paths = _paths(document)
    missing = sorted(REQUIRED_PATHS - set(paths))
    assert not missing
    assert "post" in paths["/validate/return"]


def test_validation_openapi_contract_contains_canonical_error_envelope_fields() -> None:
    schemas = _schemas(_load_contract())
    error_schema = cast(dict[str, object], schemas["ErrorEnvelope"])
    required_list = cast(list[object], error_schema["required"])
    required = {str(item) for item in required_list}
    expected_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "correlation_id",
        "trace_id",
    }
    assert expected_fields.issubset(required)


def test_validation_openapi_contract_contains_required_schemas() -> None:
    schemas = _schemas(_load_contract())
    missing = sorted(REQUIRED_SCHEMAS - set(schemas))
    assert not missing


def test_validation_runtime_routes_match_required_openapi_surface() -> None:
    runtime_routes = _runtime_route_methods()
    assert "/healthz" in runtime_routes
    assert "get" in runtime_routes["/healthz"]
    assert "/readyz" in runtime_routes
    assert "get" in runtime_routes["/readyz"]
    assert "/validate/return" in runtime_routes
    assert "post" in runtime_routes["/validate/return"]


def test_validation_openapi_contract_requires_fields_and_rule_results() -> None:
    schemas = _schemas(_load_contract())
    request_schema = cast(dict[str, object], schemas["ValidateReturnRequest"])
    request_required = {str(item) for item in cast(list[object], request_schema["required"])}
    assert "fields" in request_required
    request_properties = cast(dict[str, object], request_schema["properties"])
    domain_schema = cast(dict[str, object], request_properties["tax_domain"])
    assert {"income_tax", "health_contribution"}.issubset(
        {str(item) for item in cast(list[object], domain_schema["enum"])}
    )
    mode_schema = cast(dict[str, object], request_properties["mode"])
    assert "health_contribution currently supports draft and pre_submission only" in cast(
        str, mode_schema["description"]
    )

    result_schema = cast(dict[str, object], schemas["ValidationResult"])
    result_required = {str(item) for item in cast(list[object], result_schema["required"])}
    assert "validation_id" in result_required
    assert "rule_results" in result_required
    result_properties = cast(dict[str, object], result_schema["properties"])
    result_domain_schema = cast(dict[str, object], result_properties["tax_domain"])
    assert {"income_tax", "health_contribution"}.issubset(
        {str(item) for item in cast(list[object], result_domain_schema["enum"])}
    )


def test_validation_openapi_contract_marks_validate_return_as_internal_boundary() -> None:
    document = _load_contract()
    paths = _paths(document)
    validate_return = cast(dict[str, object], paths["/validate/return"]["post"])
    parameters = cast(list[object], validate_return["parameters"])
    assert {"$ref": "#/components/parameters/InternalValidationKeyHeader"} in parameters

    responses = cast(dict[str, object], validate_return["responses"])
    assert "403" in responses

    info = cast(dict[str, object], document["info"])
    description = cast(str, info["description"])
    assert "internal governed" in description
    assert "direct frontend integration surface" in description


def test_validation_openapi_contract_exposes_audit_evidence_on_success() -> None:
    schemas = _schemas(_load_contract())
    response_schema = cast(dict[str, object], schemas["ValidateReturnResponse"])
    required = {str(item) for item in cast(list[object], response_schema["required"])}
    assert "audit_evidence" in required

    response_properties = cast(dict[str, object], response_schema["properties"])
    assert response_properties["audit_evidence"] == {
        "$ref": "#/components/schemas/ValidationAuditEvidence"
    }


def _load_contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = document.get("paths")
    assert isinstance(raw, dict)
    return cast(dict[str, dict[str, object]], raw)


def _schemas(document: dict[str, object]) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    raw = components.get("schemas")
    assert isinstance(raw, dict)
    return cast(dict[str, object], raw)


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
