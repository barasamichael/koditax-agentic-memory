"""Validate OpenAPI and JSON schema contracts."""

from __future__ import annotations

import sys
import json
from typing import cast
from pathlib import Path
from dataclasses import dataclass

import yaml
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for


class ContractValidationError(Exception):
    """Represent a base contract validation error.

    :param file_path: Path to the invalid contract file.
    :param message: Human-readable validation error message.
    """

    def __init__(self, file_path: Path, message: str) -> None:
        super().__init__(message)
        self.file_path = file_path
        self.message = message


class InvalidYamlError(ContractValidationError):
    """Represent an invalid YAML contract file.

    :param file_path: Path to the invalid YAML file.
    :param message: Human-readable validation error message.
    """


class InvalidJsonError(ContractValidationError):
    """Represent an invalid JSON contract file.

    :param file_path: Path to the invalid JSON file.
    :param message: Human-readable validation error message.
    """


class InvalidJsonSchemaError(ContractValidationError):
    """Represent an invalid JSON Schema contract file.

    :param file_path: Path to the invalid JSON Schema file.
    :param message: Human-readable validation error message.
    """


@dataclass(frozen=True)
class ValidationIssue:
    """Describe one contract validation issue.

    :param file_path: Path to the file containing the issue.
    :param error_type: Validation error class name.
    :param message: Human-readable validation error message.
    """

    file_path: Path
    error_type: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Represent the aggregate result of contract validation.

    :param success: Whether all contract files validated successfully.
    :param issues: Collection of detected validation issues.
    """

    success: bool
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True)
class DiscoveredContracts:
    """Represent discovered contract files for validation.

    :param openapi_files: YAML OpenAPI contract files.
    :param schema_files: JSON Schema contract files.
    """

    openapi_files: tuple[Path, ...]
    schema_files: tuple[Path, ...]


def discover_contract_files(repo_root: Path) -> DiscoveredContracts:
    """Discover contract files under the repository contracts directory.

    :param repo_root: Repository root path.
    :return: Discovered OpenAPI YAML and JSON schema files.
    """

    contracts_root = repo_root / "contracts"
    openapi_files = _glob_files(contracts_root / "openapi", "*.yaml")
    event_schema_files = _glob_files(contracts_root / "events", "*.json")
    tool_schema_files = _glob_files(contracts_root / "tools" / "schemas", "*.json")
    combined_schema_files = tuple(
        sorted([*event_schema_files, *tool_schema_files], key=lambda item: item.as_posix())
    )
    return DiscoveredContracts(
        openapi_files=openapi_files,
        schema_files=combined_schema_files,
    )


def validate_yaml_file(file_path: Path) -> None:
    """Validate YAML syntax for one OpenAPI file.

    :param file_path: Path to a YAML file.
    :return: None.
    :raises InvalidYamlError: If file read or YAML parsing fails.
    """

    try:
        contents = file_path.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidYamlError(file_path, f"Unable to read YAML file: {error}.") from error

    try:
        yaml.safe_load(contents)
    except yaml.YAMLError as error:
        raise InvalidYamlError(file_path, f"Invalid YAML syntax: {error}.") from error


def validate_json_schema_file(file_path: Path) -> None:
    """Validate JSON parsing and JSON Schema structure for one file.

    :param file_path: Path to a JSON schema file.
    :return: None.
    :raises InvalidJsonError: If JSON parsing fails.
    :raises InvalidJsonSchemaError: If JSON Schema structure is invalid.
    """

    document = _load_json_document(file_path)

    if isinstance(document, bool):
        return
    if not isinstance(document, dict):
        raise InvalidJsonSchemaError(
            file_path,
            "Invalid JSON Schema root type: expected object or boolean schema.",
        )
    document_object = cast(dict[object, object], document)
    if not all(isinstance(key, str) for key in document_object):
        raise InvalidJsonSchemaError(
            file_path,
            "Invalid JSON Schema root type: expected string keys.",
        )

    try:
        schema_object = cast(dict[str, object], document_object)
        validator_class = validator_for(schema_object)
        validator_class.check_schema(schema_object)
    except SchemaError as error:
        raise InvalidJsonSchemaError(file_path, f"Invalid JSON Schema: {error.message}.") from error
    except TypeError as error:
        raise InvalidJsonSchemaError(
            file_path,
            f"Invalid JSON Schema root type: {error}.",
        ) from error


def run_contract_validation(repo_root: Path | None = None) -> ValidationResult:
    """Run contract validation and return a typed result object.

    :param repo_root: Repository root path. Uses current working directory when omitted.
    :return: Aggregate contract validation result.
    """

    target_root = repo_root if repo_root is not None else Path.cwd()
    discovered = discover_contract_files(target_root)
    issues: list[ValidationIssue] = []

    for file_path in discovered.openapi_files:
        try:
            validate_yaml_file(file_path)
        except InvalidYamlError as error:
            issues.append(_issue_from_error(error))

    for file_path in discovered.schema_files:
        try:
            validate_json_schema_file(file_path)
        except InvalidJsonError as error:
            issues.append(_issue_from_error(error))
        except InvalidJsonSchemaError as error:
            issues.append(_issue_from_error(error))

    return ValidationResult(success=len(issues) == 0, issues=tuple(issues))


def main() -> int:
    """Execute contract validation as a CLI command.

    :return: Process exit code.
    """

    repo_root = Path(__file__).resolve().parents[2]

    try:
        result = run_contract_validation(repo_root)
    except Exception as error:  # pragma: no cover
        print(f"Unexpected validation failure: {error}", file=sys.stderr)
        return 2

    if result.success:
        print("Contract and schema validation passed.")
        return 0

    print(
        f"Contract and schema validation failed with {len(result.issues)} error(s):",
        file=sys.stderr,
    )
    for issue in result.issues:
        display_path = _format_path(repo_root, issue.file_path)
        print(f"- {issue.error_type}: {display_path} - {issue.message}", file=sys.stderr)

    return 1


def _glob_files(directory: Path, pattern: str) -> tuple[Path, ...]:
    if not directory.exists():
        return ()

    return tuple(sorted(directory.glob(pattern), key=lambda item: item.as_posix()))


def _load_json_document(file_path: Path) -> object:
    try:
        contents = file_path.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidJsonError(file_path, f"Unable to read JSON file: {error}.") from error

    try:
        return json.loads(contents)
    except json.JSONDecodeError as error:
        raise InvalidJsonError(
            file_path,
            f"Invalid JSON syntax: {error.msg} at line {error.lineno} column {error.colno}.",
        ) from error


def _issue_from_error(error: ContractValidationError) -> ValidationIssue:
    return ValidationIssue(
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
