"""Tests for contract and schema validator behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.validation import contract_validator


def test_run_contract_validation_success(tmp_path: Path) -> None:
    """Validate successful contract validation on minimal valid fixtures.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_contract_tree(tmp_path)

    result = contract_validator.run_contract_validation(tmp_path)

    assert result.success is True
    assert result.issues == ()


def test_validate_json_schema_file_fails_on_malformed_json(tmp_path: Path) -> None:
    """Validate malformed JSON raises InvalidJsonError.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_contract_tree(tmp_path)
    schema_path = tmp_path / "contracts" / "events" / "event.schema.json"
    _write_text(schema_path, "{\n")

    with pytest.raises(contract_validator.InvalidJsonError):
        contract_validator.validate_json_schema_file(schema_path)


def test_validate_yaml_file_fails_on_malformed_yaml(tmp_path: Path) -> None:
    """Validate malformed YAML raises InvalidYamlError.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    openapi_path = tmp_path / "contracts" / "openapi" / "service.yaml"
    _write_text(openapi_path, "openapi: 3.0.0\ninfo: [\n")

    with pytest.raises(contract_validator.InvalidYamlError):
        contract_validator.validate_yaml_file(openapi_path)


def test_validate_json_schema_file_fails_on_invalid_schema(tmp_path: Path) -> None:
    """Validate structurally invalid JSON Schema raises InvalidJsonSchemaError.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_contract_tree(tmp_path)
    schema_path = tmp_path / "contracts" / "events" / "event.schema.json"
    _write_text(
        schema_path,
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": 123
}
""",
    )

    with pytest.raises(contract_validator.InvalidJsonSchemaError):
        contract_validator.validate_json_schema_file(schema_path)


def test_run_contract_validation_reports_error_type(tmp_path: Path) -> None:
    """Validate result object reports failure details for malformed fixtures.

    :param tmp_path: Temporary pytest directory.
    :return: None.
    """

    _create_valid_contract_tree(tmp_path)
    schema_path = tmp_path / "contracts" / "events" / "event.schema.json"
    _write_text(schema_path, "{\n")

    result = contract_validator.run_contract_validation(tmp_path)

    assert result.success is False
    assert any(issue.error_type == "InvalidJsonError" for issue in result.issues)


def _create_valid_contract_tree(root_path: Path) -> None:
    _write_text(
        root_path / "contracts" / "openapi" / "service.yaml",
        """openapi: 3.0.0
info:
  title: Service API
  version: 1.0.0
paths: {}
""",
    )
    _write_text(
        root_path / "contracts" / "events" / "event.schema.json",
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "event_id": {
      "type": "string"
    }
  }
}
""",
    )
    _write_text(
        root_path / "contracts" / "tools" / "schemas" / "tool.schema.json",
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "tool_name": {
      "type": "string"
    }
  }
}
""",
    )


def _write_text(file_path: Path, contents: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(contents, encoding="utf-8")
