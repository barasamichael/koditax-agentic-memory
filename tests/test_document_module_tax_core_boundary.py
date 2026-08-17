"""Guardrail tests to enforce tax_core-only computation boundary for document evidence linkage."""

from __future__ import annotations

import ast
import json
from typing import cast
from pathlib import Path

import pytest

import tests.income_tax_prompt_flow_support as prompt_flow_support

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = REPO_ROOT / "services"
DOCUMENT_AI_ROOT = SERVICES_ROOT / "document_ai" / "app"
ORCHESTRATION_LINKAGE_FILES = (
    SERVICES_ROOT / "orchestration" / "app" / "final_outcome_envelope.py",
)
SERVICE_COMMUNICATION_MAP_PATH = REPO_ROOT / "contracts" / "service_communication_map.json"
SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
FORBIDDEN_COMPUTATION_CALL_NAMES = frozenset(
    {
        "execute_computation",
        "execute_prepared_input",
        "prepare_execution_input",
        "finalize_computation",
        "validate_persisted_computation",
        "verify_persisted_computation_replay",
        "bind_rule_selection",
    }
)


def test_document_module_and_evidence_linkage_do_not_import_tax_core() -> None:
    target_files = _target_python_files()
    violations: list[str] = []

    for file_path in target_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for import_path in _iter_import_paths(tree):
            if not import_path.startswith("services.tax_core."):
                continue
            violations.append(
                f"{file_path.relative_to(REPO_ROOT).as_posix()}::forbidden_tax_core_import::"
                f"{import_path}"
            )

    assert (
        violations == []
    ), f"Document evidence modules must not import tax_core internals. Violations: {violations}"


def test_document_module_and_evidence_linkage_do_not_call_tax_compute_signatures() -> None:
    target_files = _target_python_files()
    violations: list[str] = []

    for file_path in target_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for call_name in _iter_called_symbol_names(tree):
            if call_name not in FORBIDDEN_COMPUTATION_CALL_NAMES:
                continue
            violations.append(
                f"{file_path.relative_to(REPO_ROOT).as_posix()}::forbidden_compute_call::"
                f"{call_name}"
            )

    assert violations == [], (
        "Document evidence modules must not invoke tax computation signatures directly. "
        f"Violations: {violations}"
    )


def test_service_communication_map_keeps_document_module_disconnected_from_tax_core() -> None:
    document = json.loads(SERVICE_COMMUNICATION_MAP_PATH.read_text(encoding="utf-8"))
    services = cast(dict[str, dict[str, object]], document["services"])

    document_ai_outbound = cast(list[dict[str, object]], services["document_ai"]["outbound_calls"])
    assert all(call["target_service"] != "tax_core" for call in document_ai_outbound)



def test_prompt_flow_tax_computation_still_routes_through_tax_core_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    original_executor = prompt_flow_support.execute_computation

    def _spy_execute_computation(request: object) -> object:
        nonlocal call_count
        call_count += 1
        return original_executor(request)  # type: ignore[arg-type]

    monkeypatch.setattr(prompt_flow_support, "execute_computation", _spy_execute_computation)
    result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)

    assert result["status"] == "draft_ready"
    assert call_count == 1


def _target_python_files() -> tuple[Path, ...]:
    document_files = sorted(DOCUMENT_AI_ROOT.rglob("*.py"), key=lambda item: item.as_posix())
    target_files: list[Path] = list(document_files)
    for file_path in ORCHESTRATION_LINKAGE_FILES:
        if file_path.exists():
            target_files.append(file_path)
    return tuple(target_files)


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


def _iter_called_symbol_names(tree: ast.AST) -> tuple[str, ...]:
    called_names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_node = node.func
        if isinstance(function_node, ast.Name):
            called_names.append(function_node.id)
            continue
        if isinstance(function_node, ast.Attribute):
            called_names.append(function_node.attr)
    return tuple(called_names)
