"""Test architecture guard service-boundary and contract checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.validation import architecture_guard
from shared.validation.architecture_guard import GuardResult
from shared.validation.architecture_guard import CrossServiceImportError
from shared.validation.architecture_guard import ArchitectureGuardFailure
from shared.validation.architecture_guard import MissingOpenApiContractError
from shared.validation.architecture_guard import CrossBoundaryPersistenceImportError


def test_architecture_guard_passes_for_valid_structure(tmp_path: Path) -> None:
    """Verify guard passes when services import only allowed modules.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_repo(tmp_path)

    result = architecture_guard.run_architecture_guard(tmp_path)

    assert result == GuardResult(success=True, issues=())
    architecture_guard.enforce_architecture_guard(tmp_path)


def test_architecture_guard_detects_cross_service_import(tmp_path: Path) -> None:
    """Verify guard fails for direct cross-service imports.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_repo(tmp_path)
    _write_text(
        tmp_path / "services" / "gateway" / "app" / "main.py",
        "from services.event_store.app.main import create_app\n",
    )

    failure = _run_and_capture_failure(tmp_path)

    assert any(isinstance(error, CrossServiceImportError) for error in failure.errors)


def test_architecture_guard_detects_cross_boundary_persistence_import(tmp_path: Path) -> None:
    """Verify guard fails for cross-boundary persistence imports.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_repo(tmp_path)
    _write_text(
        tmp_path / "services" / "gateway" / "app" / "main.py",
        "from services.event_store.database.repo import load_events\n",
    )

    failure = _run_and_capture_failure(tmp_path)

    assert any(isinstance(error, CrossBoundaryPersistenceImportError) for error in failure.errors)


def test_architecture_guard_detects_missing_contract_file(tmp_path: Path) -> None:
    """Verify guard fails when a service lacks its OpenAPI contract.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_repo(tmp_path)
    contract_path = tmp_path / "contracts" / "openapi" / "event_store.yaml"
    contract_path.unlink()

    failure = _run_and_capture_failure(tmp_path)

    assert any(isinstance(error, MissingOpenApiContractError) for error in failure.errors)


def _run_and_capture_failure(repo_root: Path) -> ArchitectureGuardFailure:
    """Run guard and return the raised failure object.

    :param repo_root: Temporary repository root.
    :return: Captured architecture guard failure.
    """

    with pytest.raises(ArchitectureGuardFailure) as captured:
        architecture_guard.enforce_architecture_guard(repo_root)
    return captured.value


def _create_valid_repo(repo_root: Path) -> None:
    """Create a minimal valid repository layout for guard checks.

    :param repo_root: Temporary repository root.
    :return: None.
    """

    _write_text(
        repo_root / "services" / "gateway" / "app" / "main.py",
        "from shared.errors import codes\n",
    )
    _write_text(
        repo_root / "services" / "event_store" / "app" / "main.py",
        "from shared.tracing.correlation import get_correlation_id\n",
    )
    _write_text(repo_root / "contracts" / "openapi" / "gateway.yaml", "openapi: 3.0.0\n")
    _write_text(repo_root / "contracts" / "openapi" / "event_store.yaml", "openapi: 3.0.0\n")


def _write_text(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
