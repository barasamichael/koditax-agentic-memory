"""Deterministic OpenAPI baseline tests for the Phase 9 reports contract."""

from __future__ import annotations

from typing import cast
from pathlib import Path

import yaml

CONTRACT_PATH = Path("contracts/openapi/reports.yaml")

REQUIRED_BASELINE_PATHS = {
    "/v1/reports/income-tax/artifacts",
    "/v1/reports/health-contribution/artifacts",
    "/v1/reports/income-tax/artifacts/{report_id}/metadata",
    "/v1/reports/health-contribution/artifacts/{report_id}/metadata",
    "/v1/reports/income-tax/history",
    "/v1/reports/income-tax/exports/{export_package_id}/metadata",
}

REQUIRED_METHODS_BY_PATH = {
    "/v1/reports/income-tax/artifacts": {"post"},
    "/v1/reports/health-contribution/artifacts": {"post"},
    "/v1/reports/income-tax/artifacts/{report_id}/metadata": {"get"},
    "/v1/reports/health-contribution/artifacts/{report_id}/metadata": {"get"},
    "/v1/reports/income-tax/history": {"get"},
    "/v1/reports/income-tax/exports/{export_package_id}/metadata": {"get"},
}

REQUIRED_SCHEMAS = {
    "ReportError",
    "ReportLineage",
    "ReportAuditEventType",
    "ReportAuditLineage",
    "ReportAuditEvent",
    "ReportAuditFailureEvent",
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "ReportArtifactMetadataResponse",
    "ReportHistoryItem",
    "ReportHistoryListResponse",
    "ReportExportPackageMetadataResponse",
}

UNSUPPORTED_SCOPE_PREFIXES = {
    "/v1/reports/vat",
    "/v1/reports/withholding-tax",
    "/v1/reports/payroll",
    "/v1/reports/customs",
    "/v1/reports/excise",
    "/v1/reports/corporate-tax",
}


def test_reports_openapi_contract_parses() -> None:
    document = load_reports_contract()
    assert document.get("openapi") == "3.1.0"
    assert isinstance(document.get("paths"), dict)
    assert isinstance(document.get("components"), dict)


def test_reports_openapi_contract_required_paths_and_methods_exist() -> None:
    paths = load_reports_paths(load_reports_contract())
    missing_paths = sorted(REQUIRED_BASELINE_PATHS - set(paths))
    assert not missing_paths, "reports openapi drift: missing path(s): " + ", ".join(missing_paths)

    for path, required_methods in REQUIRED_METHODS_BY_PATH.items():
        declared_methods = {
            method_name.lower()
            for method_name in paths[path]
            if method_name.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
        }
        missing_methods = sorted(required_methods - declared_methods)
        assert (
            not missing_methods
        ), f"reports openapi drift: path `{path}` missing method(s): " + ", ".join(missing_methods)


def test_reports_openapi_contract_contains_required_baseline_schemas() -> None:
    schemas = load_reports_schemas(load_reports_contract())
    missing_schemas = sorted(REQUIRED_SCHEMAS - set(schemas))
    assert not missing_schemas, "reports openapi drift: missing schema(s): " + ", ".join(
        missing_schemas
    )


def test_reports_openapi_contract_scope_guard_is_tax_domain_aware() -> None:
    document = load_reports_contract()
    info = cast(dict[str, object], document.get("info", {}))
    description = cast(str, info.get("description", "")).lower()
    assert "tax-domain-aware" in description
    assert "implemented for income-tax and health_contribution summary outputs" in description
    assert "recognized domains such as" in description
    for domain_name in ("vat", "withholding_tax", "corporate_tax"):
        assert domain_name in description

    paths = load_reports_paths(document)
    for unsupported_prefix in UNSUPPORTED_SCOPE_PREFIXES:
        matches = sorted(path for path in paths if path.startswith(unsupported_prefix))
        assert not matches, (
            f"reports openapi drift: unsupported scope `{unsupported_prefix}` advertised: "
            + ", ".join(matches)
        )


def test_reports_openapi_contract_allows_health_summary_report_type() -> None:
    document = load_reports_contract()
    schemas = load_reports_schemas(document)
    request_schema = cast(dict[str, object], schemas["ReportGenerationRequest"])
    properties = cast(dict[str, object], request_schema["properties"])
    report_type_schema = cast(dict[str, object], properties["report_type"])
    enum_values = set(cast(list[str], report_type_schema.get("enum", [])))
    assert "health_contribution_summary" in enum_values


def test_reports_openapi_contract_describes_governed_validation_conflict_path() -> None:
    document = load_reports_contract()
    paths = load_reports_paths(document)
    operation = load_reports_operation(
        paths=paths,
        path="/v1/reports/health-contribution/artifacts",
        method="post",
    )
    responses = cast(dict[str, object], operation["responses"])
    conflict_response = cast(dict[str, object], responses["409"])
    description = cast(str, conflict_response.get("description", "")).lower()
    assert "governed validation" in description


def load_reports_contract() -> dict[str, object]:
    assert CONTRACT_PATH.exists(), "reports openapi missing: contracts/openapi/reports.yaml"
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "reports openapi parse failure: root must be object."
    return cast(dict[str, object], loaded)


def load_reports_paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    paths_value = document.get("paths")
    assert isinstance(paths_value, dict), "reports openapi parse failure: `paths` is not a mapping."
    typed_paths = cast(dict[str, object], paths_value)
    output: dict[str, dict[str, object]] = {}
    for path, path_item in typed_paths.items():
        assert isinstance(
            path, str
        ), "reports openapi parse failure: non-string path key encountered."
        assert isinstance(
            path_item, dict
        ), f"reports openapi parse failure: path item for `{path}` must be an object."
        output[path] = cast(dict[str, object], path_item)
    return output


def load_reports_schemas(document: dict[str, object]) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    schemas = components.get("schemas")
    assert isinstance(schemas, dict), "reports openapi parse failure: `components.schemas` missing."
    return cast(dict[str, object], schemas)


def load_reports_operation(
    *,
    paths: dict[str, dict[str, object]],
    path: str,
    method: str,
) -> dict[str, object]:
    path_item = cast(dict[str, object], paths.get(path))
    assert path_item is not None, f"reports openapi drift: required path `{path}` missing."
    operation = path_item.get(method.lower())
    assert isinstance(
        operation, dict
    ), f"reports openapi drift: `{method.upper()} {path}` operation missing."
    return cast(dict[str, object], operation)
