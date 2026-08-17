"""Prove tax_core owns the deterministic computation lifecycle boundary."""

from __future__ import annotations

import ast
import json
from typing import cast
from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from services.tax_core.app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = REPO_ROOT / "services"
OPENAPI_ROOT = REPO_ROOT / "contracts" / "openapi"
COMMUNICATION_MAP_PATH = REPO_ROOT / "contracts" / "service_communication_map.json"
TAX_CORE_CONTRACT_PATH = OPENAPI_ROOT / "tax_core.yaml"
EXPECTED_LIFECYCLE_PATHS = {
    "/computations/execute",
    "/computations/finalize",
    "/computations/replay",
    "/computations/validate",
}
EXPECTED_OPERATION_IDS = {
    "/computations/execute": "executeComputation",
    "/computations/finalize": "finalizeComputation",
    "/computations/replay": "replayComputation",
    "/computations/validate": "validateComputation",
}


def test_tax_core_contract_exposes_complete_lifecycle_boundary() -> None:
    """Verify tax_core OpenAPI is the complete lifecycle contract surface."""

    contract_document = _load_yaml_document(TAX_CORE_CONTRACT_PATH)
    paths = _load_path_operations(contract_document)

    assert set(paths) == EXPECTED_LIFECYCLE_PATHS
    for path, operation_id in EXPECTED_OPERATION_IDS.items():
        path_item = paths[path]
        post_operation = cast(dict[str, object], path_item["post"])
        assert post_operation["operationId"] == operation_id


def test_tax_core_app_routes_match_contract_lifecycle_boundary() -> None:
    """Verify runtime tax_core routes match the declared lifecycle contract."""

    app = create_app()
    route_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    }

    assert route_paths == EXPECTED_LIFECYCLE_PATHS


def test_no_other_service_contract_defines_tax_core_lifecycle_endpoints() -> None:
    """Verify no non-tax-core service contract claims computation lifecycle paths."""

    for contract_path in sorted(OPENAPI_ROOT.glob("*.yaml"), key=lambda item: item.name):
        if contract_path.name == "tax_core.yaml":
            continue

        contract_document = _load_yaml_document(contract_path)
        paths = _load_path_operations(contract_document)

        assert not (set(paths) & EXPECTED_LIFECYCLE_PATHS), contract_path.as_posix()


def test_non_tax_core_services_do_not_import_tax_core_internals() -> None:
    """Verify service code outside tax_core does not import tax_core internals."""

    offending_imports: list[str] = []

    for service_path in sorted(SERVICES_ROOT.iterdir(), key=lambda item: item.name):
        if not service_path.is_dir() or service_path.name == "tax_core":
            continue

        for file_path in sorted(service_path.rglob("*.py"), key=lambda item: item.as_posix()):
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for import_path in _iter_import_paths(tree):
                if not import_path.startswith("services.tax_core."):
                    continue
                offending_imports.append(
                    f"{file_path.relative_to(REPO_ROOT).as_posix()}::{import_path}"
                )

    assert offending_imports == []


def test_service_communication_map_declares_no_calls_to_tax_core() -> None:
    """Verify no runtime or planned inter-service calls target tax_core."""

    document = json.loads(COMMUNICATION_MAP_PATH.read_text(encoding="utf-8"))
    services = document["services"]

    assert services["tax_core"]["outbound_calls"] == []
    for caller_service, caller_entry in services.items():
        outbound_calls = caller_entry["outbound_calls"]
        for outbound_call in outbound_calls:
            assert outbound_call["target_service"] != "tax_core", caller_service


def _load_yaml_document(file_path: Path) -> dict[str, object]:
    with file_path.open("r", encoding="utf-8") as yaml_file:
        document = cast(object, yaml.safe_load(yaml_file))

    if document is None:
        return {}

    assert isinstance(document, dict)
    return cast(dict[str, object], document)


def _load_path_operations(document: dict[str, object]) -> dict[str, dict[str, object]]:
    paths = document.get("paths")
    if paths is None:
        return {}

    assert isinstance(paths, dict)
    typed_paths = cast(dict[object, object], paths)
    path_operations: dict[str, dict[str, object]] = {}
    for raw_path, raw_operation in typed_paths.items():
        if not isinstance(raw_path, str):
            continue
        if not isinstance(raw_operation, dict):
            continue
        path_operations[raw_path] = cast(dict[str, object], raw_operation)
    return path_operations


def _iter_import_paths(tree: ast.AST) -> tuple[str, ...]:
    import_paths: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_paths.append(alias.name)
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            import_paths.append(node.module)
    return tuple(import_paths)
