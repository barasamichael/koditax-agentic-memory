"""OpenAPI parse/integrity guard checks for Phase 10 forms baseline."""

from __future__ import annotations

from typing import Any
from typing import cast
from pathlib import Path

import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from services.forms.app.main import create_app

CONTRACT_PATH = Path("contracts/openapi/forms.yaml")

REQUIRED_BASELINE_PATHS = {
    "/v1/forms/income-tax/mappings",
    "/v1/forms/health-contribution/mappings",
    "/v1/forms/income-tax/version-bindings",
    "/v1/forms/income-tax/validations",
    "/v1/forms/income-tax/artifacts",
    "/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
}

REQUIRED_METHODS_BY_PATH = {
    "/v1/forms/income-tax/mappings": {"post"},
    "/v1/forms/health-contribution/mappings": {"post"},
    "/v1/forms/income-tax/version-bindings": {"post"},
    "/v1/forms/income-tax/validations": {"post"},
    "/v1/forms/income-tax/artifacts": {"post"},
    "/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata": {"get"},
}

REQUIRED_REQUEST_SCHEMA_REF_BY_OPERATION = {
    ("/v1/forms/income-tax/mappings", "post"): "FormMappingRequest",
    ("/v1/forms/health-contribution/mappings", "post"): "FormMappingRequest",
    ("/v1/forms/income-tax/version-bindings", "post"): "FormVersionBindingRequest",
    ("/v1/forms/income-tax/validations", "post"): "FormValidationRequest",
    ("/v1/forms/income-tax/artifacts", "post"): "FormArtifactGenerationRequest",
}

REQUIRED_SUCCESS_RESPONSE_SCHEMA_REF_BY_OPERATION = {
    ("/v1/forms/income-tax/mappings", "post", "200"): "FormMappingResponse",
    ("/v1/forms/health-contribution/mappings", "post", "200"): "FormMappingResponse",
    ("/v1/forms/income-tax/version-bindings", "post", "200"): "FormVersionBindingResponse",
    ("/v1/forms/income-tax/validations", "post", "200"): "FormValidationResponse",
    ("/v1/forms/income-tax/artifacts", "post", "201"): "FormArtifactGenerationResponse",
    (
        "/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
        "get",
        "200",
    ): "FormArtifactMetadataResponse",
}

REQUIRED_SCHEMAS = {
    "ErrorEnvelope",
    "FormsErrorReasonCode",
    "Traceability",
    "FormMappingRequest",
    "FormMappingResponse",
    "FormVersionBindingRequest",
    "FormVersionBindingResponse",
    "FormValidationRequest",
    "FormValidationResponse",
    "FormArtifactGenerationRequest",
    "FormArtifactGenerationResponse",
    "FormArtifactMetadataResponse",
}

UNSUPPORTED_SCOPE_PREFIXES = {
    "/v1/forms/vat",
    "/v1/forms/corporate-tax",
    "/v1/forms/payroll",
    "/v1/forms/customs",
    "/v1/forms/excise",
}


def test_forms_openapi_parses_and_exposes_expected_openapi_root() -> None:
    document = _load_contract()
    assert (
        document.get("openapi") == "3.1.0"
    ), "forms openapi drift: expected `openapi: 3.1.0` root version."
    assert isinstance(
        document.get("paths"), dict
    ), "forms openapi drift: `paths` mapping missing or invalid."
    assert isinstance(
        document.get("components"), dict
    ), "forms openapi drift: `components` mapping missing or invalid."


def test_forms_openapi_contains_required_baseline_paths_and_methods() -> None:
    paths = _load_paths(_load_contract())
    missing_paths = sorted(REQUIRED_BASELINE_PATHS - set(paths))
    assert (
        not missing_paths
    ), "forms openapi drift: missing required baseline path(s): " + ", ".join(missing_paths)

    for path, required_methods in REQUIRED_METHODS_BY_PATH.items():
        declared_methods = {
            method_name.lower()
            for method_name in paths[path]
            if method_name.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
        }
        missing_methods = sorted(required_methods - declared_methods)
        assert (
            not missing_methods
        ), f"forms openapi drift: path `{path}` missing required method(s): " + ", ".join(
            missing_methods
        )


def test_forms_openapi_required_schemas_exist_and_required_refs_resolve() -> None:
    document = _load_contract()
    schemas = _load_schemas(document)

    missing_schemas = sorted(REQUIRED_SCHEMAS - set(schemas))
    assert not missing_schemas, "forms openapi drift: missing required schema(s): " + ", ".join(
        missing_schemas
    )

    paths = _load_paths(document)
    for (path, method), request_schema_name in REQUIRED_REQUEST_SCHEMA_REF_BY_OPERATION.items():
        operation = _operation(paths=paths, path=path, method=method)
        request_body = cast(dict[str, object], operation.get("requestBody"))
        content = cast(dict[str, object], request_body.get("content"))
        app_json = cast(dict[str, object], content.get("application/json"))
        schema = cast(dict[str, object], app_json.get("schema"))
        actual_ref = cast(str, schema.get("$ref"))
        expected_ref = f"#/components/schemas/{request_schema_name}"
        assert actual_ref == expected_ref, (
            f"forms openapi drift: `{method.upper()} {path}` request schema ref mismatch. "
            f"expected `{expected_ref}`, got `{actual_ref}`."
        )

    for (
        path,
        method,
        status_code,
    ), response_schema_name in REQUIRED_SUCCESS_RESPONSE_SCHEMA_REF_BY_OPERATION.items():
        operation = _operation(paths=paths, path=path, method=method)
        responses = cast(dict[str, object], operation.get("responses"))
        response = cast(dict[str, object], responses.get(status_code))
        content = cast(dict[str, object], response.get("content"))
        app_json = cast(dict[str, object], content.get("application/json"))
        schema = cast(dict[str, object], app_json.get("schema"))
        actual_ref = cast(str, schema.get("$ref"))
        expected_ref = f"#/components/schemas/{response_schema_name}"
        assert actual_ref == expected_ref, (
            f"forms openapi drift: `{method.upper()} {path}` `{status_code}` "
            "response schema ref mismatch. "
            f"expected `{expected_ref}`, got `{actual_ref}`."
        )


def test_forms_openapi_error_schema_contains_canonical_fields_and_reason_codes() -> None:
    document = _load_contract()
    schemas = _load_schemas(document)
    error_schema = cast(dict[str, object], schemas["ErrorEnvelope"])
    reason_schema = cast(dict[str, object], schemas["FormsErrorReasonCode"])

    required_keys = set(cast(list[str], error_schema.get("required", [])))
    assert {"error_code", "message", "reason", "trace_id", "correlation_id"}.issubset(
        required_keys
    ), (
        "forms openapi drift: ErrorEnvelope required keys must include "
        "`error_code`, `message`, `reason`, `trace_id`, `correlation_id`."
    )

    reason_values = set(cast(list[str], reason_schema.get("enum", [])))
    expected_reason_values = {
        "forms_operation_not_implemented",
        "forms_scope_not_supported",
        "invalid_tax_domain",
        "unsupported_tax_domain_path",
        "unimplemented_tax_domain_mapping",
        "forms_request_invalid",
        "forms_contract_violation",
    }
    assert expected_reason_values.issubset(reason_values), (
        "forms openapi drift: FormsErrorReasonCode enum missing required reason code(s): "
        + ", ".join(sorted(expected_reason_values - reason_values))
    )


def test_forms_openapi_scope_guard_is_tax_domain_aware() -> None:
    document = _load_contract()
    info = cast(dict[str, object], document.get("info", {}))
    description = cast(str, info.get("description", "")).lower()
    assert (
        "tax-domain-aware" in description
    ), "forms openapi drift: contract description must state tax-domain-aware scope handling."
    assert "governed form mapping is implemented for income-tax and" in description
    assert "health_contribution outputs" in description
    assert "recognized domains such as" in description
    for domain_name in ("vat", "withholding_tax", "corporate_tax"):
        assert domain_name in description

    paths = _load_paths(document)
    for unsupported_prefix in UNSUPPORTED_SCOPE_PREFIXES:
        matches = sorted(path for path in paths if path.startswith(unsupported_prefix))
        assert not matches, (
            f"forms openapi drift: unsupported scope `{unsupported_prefix}` was advertised: "
            + ", ".join(matches)
        )


def test_forms_runtime_routes_cover_required_openapi_baseline_paths() -> None:
    app = create_app()
    runtime_routes = {
        route.path: {method.lower() for method in route.methods if method != "HEAD"}
        for route in app.routes
        if isinstance(route, APIRoute)
    }

    for path, methods in REQUIRED_METHODS_BY_PATH.items():
        assert (
            path in runtime_routes
        ), f"forms runtime drift: required route `{path}` missing from FastAPI app."
        missing_methods = sorted(methods - runtime_routes[path])
        assert (
            not missing_methods
        ), f"forms runtime drift: route `{path}` missing method(s): " + ", ".join(missing_methods)


def test_forms_openapi_form_mapping_response_allows_health_form_type() -> None:
    schema = _load_schema(_load_contract(), "FormMappingResponse")
    properties = cast(dict[str, object], schema["properties"])
    form_type_schema = cast(dict[str, object], properties["form_type"])
    enum_values = set(cast(list[str], form_type_schema.get("enum", [])))
    assert {"income_tax_return", "health_contribution_summary"}.issubset(enum_values)
    assert properties["governed_validation"] == {
        "$ref": "#/components/schemas/GovernedValidationEnvelope"
    }


def test_forms_openapi_generation_and_mapping_conflict_paths_allow_governed_validation_block() -> (
    None
):
    document = _load_contract()
    paths = _load_paths(document)

    for path in (
        "/v1/forms/income-tax/mappings",
        "/v1/forms/health-contribution/mappings",
        "/v1/forms/income-tax/artifacts",
    ):
        operation = _operation(paths=paths, path=path, method="post")
        responses = cast(dict[str, object], operation["responses"])
        conflict_response = cast(dict[str, object], responses["409"])
        content = cast(dict[str, object], conflict_response["content"])
        application_json = cast(dict[str, object], content["application/json"])
        schema = cast(dict[str, object], application_json["schema"])
        one_of = cast(list[object], schema["oneOf"])
        assert {"$ref": "#/components/schemas/FormGenerationBlockedResponse"} in one_of


def test_forms_runtime_error_envelope_keys_match_openapi_error_schema() -> None:
    contract = _load_contract()
    error_schema = _load_schema(contract, "ErrorEnvelope")
    schema_properties = set(cast(dict[str, object], error_schema["properties"]).keys())
    schema_required = set(cast(list[str], error_schema["required"]))

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={},
            headers={"X-Correlation-ID": "forms-openapi-runtime-parity-corr"},
        )
    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])
    runtime_keys = set(detail.keys())

    assert schema_required.issubset(runtime_keys), (
        "forms runtime/contract drift: runtime error detail missing required contract keys: "
        + ", ".join(sorted(schema_required - runtime_keys))
    )
    assert runtime_keys.issubset(schema_properties), (
        "forms runtime/contract drift: runtime error detail contains non-contract key(s): "
        + ", ".join(sorted(runtime_keys - schema_properties))
    )


def _load_contract() -> dict[str, object]:
    assert CONTRACT_PATH.exists(), "forms openapi missing: contracts/openapi/forms.yaml"
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "forms openapi parse failure: root document is not a mapping."
    return cast(dict[str, object], loaded)


def _load_paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    paths_value = document.get("paths")
    assert isinstance(paths_value, dict), "forms openapi parse failure: `paths` is not a mapping."
    typed_paths = cast(dict[str, object], paths_value)
    output: dict[str, dict[str, object]] = {}
    for path, path_item in typed_paths.items():
        assert isinstance(
            path, str
        ), "forms openapi parse failure: non-string path key encountered."
        assert isinstance(
            path_item, dict
        ), f"forms openapi parse failure: path item for `{path}` must be an object."
        output[path] = cast(dict[str, object], path_item)
    return output


def _load_schemas(document: dict[str, object]) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    schemas = components.get("schemas")
    assert isinstance(schemas, dict), "forms openapi parse failure: `components.schemas` missing."
    return cast(dict[str, object], schemas)


def _load_schema(document: dict[str, object], name: str) -> dict[str, object]:
    schemas = _load_schemas(document)
    schema = schemas.get(name)
    assert isinstance(schema, dict), f"forms openapi drift: schema `{name}` missing."
    return cast(dict[str, object], schema)


def _operation(
    *,
    paths: dict[str, dict[str, object]],
    path: str,
    method: str,
) -> dict[str, object]:
    path_item = cast(dict[str, object], paths.get(path))
    assert path_item is not None, f"forms openapi drift: required path `{path}` missing."
    operation = path_item.get(method)
    assert isinstance(
        operation, dict
    ), f"forms openapi drift: `{method.upper()} {path}` operation missing."
    return cast(dict[str, object], operation)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(
        payload, dict
    ), "forms runtime parity failure: response payload is not object."
    return cast(dict[str, object], payload)
