"""Contract checks for Phase 7 storage OpenAPI baseline."""

from __future__ import annotations

from typing import cast
from pathlib import Path

import yaml

CONTRACT_PATH = Path("contracts/openapi/storage.yaml")

REQUIRED_PATHS = {
    "/v1/storage/upload-capabilities",
    "/v1/storage/download-capabilities",
    "/v1/storage/objects/{object_key}/metadata",
}

REQUIRED_METHODS_BY_PATH = {
    "/v1/storage/upload-capabilities": {"post"},
    "/v1/storage/download-capabilities": {"post"},
    "/v1/storage/objects/{object_key}/metadata": {"get"},
}


def test_storage_openapi_parses() -> None:
    document = _load_contract()
    assert document.get("openapi") == "3.1.0"


def test_storage_openapi_contains_required_paths_and_methods() -> None:
    paths = _load_paths(_load_contract())

    assert REQUIRED_PATHS.issubset(set(paths))
    for path, methods in REQUIRED_METHODS_BY_PATH.items():
        assert methods.issubset(set(paths[path]))


def test_storage_capability_schema_has_security_critical_fields() -> None:
    document = _load_contract()
    capability_schema = _load_schema(document, "Capability")
    required = set(cast(list[str], capability_schema["required"]))
    assert {
        "capability_id",
        "object_key",
        "expires_at",
        "method",
        "headers",
    }.issubset(required)


def test_storage_error_envelope_exists_with_deterministic_fields() -> None:
    document = _load_contract()
    error_schema = _load_schema(document, "ErrorEnvelope")
    assert error_schema["type"] == "object"
    assert error_schema["additionalProperties"] is False
    required = set(cast(list[str], error_schema["required"]))
    assert {"error_code", "message", "reason"}.issubset(required)


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
