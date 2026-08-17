"""Enforce service-boundary and contract structural invariants."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from dataclasses import dataclass

FORBIDDEN_BOUNDARY_MODULES = {"persistence", "database", "models"}


class ArchitectureGuardError(Exception):
    """Represent a base architecture guard violation."""

    def __init__(self, file_path: Path, message: str) -> None:
        super().__init__(message)
        self.file_path = file_path
        self.message = message


class CrossServiceImportError(ArchitectureGuardError):
    """Represent a cross-service import violation."""


class CrossBoundaryPersistenceImportError(ArchitectureGuardError):
    """Represent a cross-boundary persistence import violation."""


class MissingOpenApiContractError(ArchitectureGuardError):
    """Represent a missing per-service OpenAPI contract violation."""


class InvalidPythonSyntaxError(ArchitectureGuardError):
    """Represent invalid Python syntax encountered during guard parsing."""


class ArchitectureGuardFailure(Exception):
    """Represent aggregate architecture guard failures."""

    def __init__(self, errors: tuple[ArchitectureGuardError, ...]) -> None:
        super().__init__("Architecture guard violations detected.")
        self.errors = errors


@dataclass(frozen=True)
class GuardIssue:
    """Describe one architecture guard issue."""

    file_path: Path
    error_type: str
    message: str


@dataclass(frozen=True)
class GuardResult:
    """Represent aggregate architecture guard results."""

    success: bool
    issues: tuple[GuardIssue, ...]


@dataclass(frozen=True)
class ServiceLayout:
    """Represent one discovered service package layout."""

    service_name: str
    service_path: Path
    python_files: tuple[Path, ...]


def run_architecture_guard(repo_root: Path | None = None) -> GuardResult:
    """Run architecture drift checks and return a typed result object.

    :param repo_root: Repository root path. Uses current working directory when omitted.
    :return: Aggregate architecture guard result.
    """

    target_root = repo_root if repo_root is not None else Path.cwd()
    errors = collect_architecture_errors(target_root)
    issues = tuple(_issue_from_error(error) for error in errors)
    return GuardResult(success=len(issues) == 0, issues=issues)


def enforce_architecture_guard(repo_root: Path | None = None) -> None:
    """Run architecture checks and raise on violations.

    :param repo_root: Repository root path. Uses current working directory when omitted.
    :return: None.
    :raises ArchitectureGuardFailure: If one or more violations are detected.
    """

    target_root = repo_root if repo_root is not None else Path.cwd()
    errors = collect_architecture_errors(target_root)
    if errors:
        raise ArchitectureGuardFailure(tuple(errors))


def collect_architecture_errors(repo_root: Path) -> tuple[ArchitectureGuardError, ...]:
    """Collect all architecture guard violations for the repository.

    :param repo_root: Repository root path.
    :return: Tuple of architecture guard violations.
    """

    errors: list[ArchitectureGuardError] = []

    for service_layout in discover_service_layouts(repo_root):
        for contract_error in _collect_contract_errors(repo_root, service_layout.service_name):
            errors.append(contract_error)
        for import_error in _collect_import_errors(service_layout):
            errors.append(import_error)

    return tuple(errors)


def discover_service_layouts(repo_root: Path) -> tuple[ServiceLayout, ...]:
    """Discover services and their Python files under the repository.

    :param repo_root: Repository root path.
    :return: Tuple of discovered service layouts.
    """

    services_root = repo_root / "services"
    if not services_root.exists():
        return ()

    layouts: list[ServiceLayout] = []
    for service_path in sorted(services_root.iterdir(), key=lambda item: item.name):
        if not service_path.is_dir():
            continue
        python_files = tuple(
            sorted(service_path.rglob("*.py"), key=lambda file_path: file_path.as_posix())
        )
        layouts.append(
            ServiceLayout(
                service_name=service_path.name,
                service_path=service_path,
                python_files=python_files,
            )
        )

    return tuple(layouts)


def _collect_contract_errors(
    repo_root: Path,
    service_name: str,
) -> tuple[ArchitectureGuardError, ...]:
    expected_contract = repo_root / "contracts" / "openapi" / f"{service_name}.yaml"
    if expected_contract.exists():
        return ()

    return (
        MissingOpenApiContractError(
            file_path=expected_contract,
            message=f"Missing OpenAPI contract for service '{service_name}'.",
        ),
    )


def _collect_import_errors(service_layout: ServiceLayout) -> tuple[ArchitectureGuardError, ...]:
    errors: list[ArchitectureGuardError] = []
    for file_path in service_layout.python_files:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        except SyntaxError as error:
            errors.append(
                InvalidPythonSyntaxError(
                    file_path=file_path,
                    message=f"Invalid Python syntax: {error.msg}.",
                )
            )
            continue

        for import_path in _iter_import_paths(tree):
            try:
                _assert_import_allowed(service_layout.service_name, file_path, import_path)
            except ArchitectureGuardError as error:
                errors.append(error)

    return tuple(errors)


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


def _assert_import_allowed(service_name: str, file_path: Path, import_path: str) -> None:
    if import_path == "services":
        raise CrossServiceImportError(
            file_path=file_path,
            message="Importing from root 'services' package is not allowed inside a service.",
        )

    if not import_path.startswith("services."):
        return

    parts = import_path.split(".")
    if len(parts) < 2:
        return

    target_service = parts[1]
    if target_service == service_name:
        return

    if len(parts) >= 3 and parts[2] in FORBIDDEN_BOUNDARY_MODULES:
        raise CrossBoundaryPersistenceImportError(
            file_path=file_path,
            message=(
                f"Cross-boundary persistence import '{import_path}' is not allowed "
                f"for service '{service_name}'."
            ),
        )

    raise CrossServiceImportError(
        file_path=file_path,
        message=(
            f"Cross-service import '{import_path}' is not allowed for service '{service_name}'."
        ),
    )


def main() -> int:
    """Execute architecture guard checks as a CLI command.

    :return: Process exit code.
    """

    repo_root = Path(__file__).resolve().parents[2]
    try:
        enforce_architecture_guard(repo_root)
    except ArchitectureGuardFailure as error:
        print(
            f"Architecture guard failed with {len(error.errors)} violation(s):",
            file=sys.stderr,
        )
        for issue in error.errors:
            display_path = _format_path(repo_root, issue.file_path)
            print(
                f"- {issue.__class__.__name__}: {display_path} - {issue.message}",
                file=sys.stderr,
            )
        return 1
    except Exception as error:  # pragma: no cover
        print(f"Unexpected architecture guard failure: {error}", file=sys.stderr)
        return 2

    print("Architecture guard passed.")
    return 0


def _issue_from_error(error: ArchitectureGuardError) -> GuardIssue:
    return GuardIssue(
        file_path=error.file_path,
        error_type=error.__class__.__name__,
        message=error.message,
    )


def _format_path(repo_root: Path, file_path: Path) -> str:
    try:
        return str(file_path.relative_to(repo_root))
    except ValueError:
        return str(file_path)


if __name__ == "__main__":
    raise SystemExit(main())
