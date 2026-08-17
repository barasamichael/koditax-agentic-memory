"""Deterministic report error-contract drift guards for Phase 9.1.5."""

from __future__ import annotations

from typing import cast

from tests.test_reports_openapi_contract import load_reports_paths
from tests.test_reports_openapi_contract import load_reports_schemas
from tests.test_reports_openapi_contract import load_reports_contract
from tests.test_reports_openapi_contract import load_reports_operation

REQUIRED_REASON_CODES = {
    "unsupported_report_scope",
    "invalid_tax_domain",
    "unsupported_tax_domain_path",
    "unimplemented_tax_domain_report_generation",
    "report_not_found",
    "report_generation_not_supported",
    "invalid_report_request",
    "invalid_lineage_reference",
    "report_access_forbidden",
    "report_artifact_expired",
    "report_storage_unavailable",
}

ERROR_ENVELOPE_REQUIRED_FIELDS = {
    "error_code",
    "message",
    "reason",
    "reason_code",
}

ERROR_RESPONSE_OPERATIONS = {
    ("/v1/reports/income-tax/artifacts", "post"),
    ("/v1/reports/income-tax/artifacts/{report_id}/metadata", "get"),
    ("/v1/reports/income-tax/history", "get"),
    ("/v1/reports/income-tax/exports/{export_package_id}/metadata", "get"),
}


def test_reports_error_contract_required_fields_and_reason_codes() -> None:
    schemas = load_reports_schemas(load_reports_contract())
    report_error = cast(dict[str, object], schemas["ReportError"])
    required_fields = set(cast(list[str], report_error.get("required", [])))
    missing_fields = sorted(ERROR_ENVELOPE_REQUIRED_FIELDS - required_fields)
    assert (
        not missing_fields
    ), "reports error contract drift: missing ReportError required field(s): " + ", ".join(
        missing_fields
    )

    properties = cast(dict[str, object], report_error.get("properties", {}))
    assert "context" in properties, "reports error contract drift: `context` property missing."

    reason_code = cast(dict[str, object], properties.get("reason_code", {}))
    ref = cast(str | None, reason_code.get("$ref"))
    expected_refs = {
        "#/components/schemas/ReportErrorReasonCode",
        "#/components/schemas/ReportsErrorReasonCode",
    }
    assert (
        ref in expected_refs
    ), "reports error contract drift: reason_code must reference reason enum."

    reason_schema_name = ref.split("/")[-1] if ref else "ReportErrorReasonCode"
    reason_schema = cast(dict[str, object], schemas[reason_schema_name])
    reason_codes = set(cast(list[str], reason_schema.get("enum", [])))
    missing_reason_codes = sorted(REQUIRED_REASON_CODES - reason_codes)
    assert (
        not missing_reason_codes
    ), "reports error contract drift: missing required reason code(s): " + ", ".join(
        missing_reason_codes
    )


def test_reports_error_contract_responses_reference_canonical_envelope() -> None:
    document = load_reports_contract()
    paths = load_reports_paths(document)
    expected_ref = "#/components/schemas/ReportError"
    accepted_legacy_ref = "#/components/schemas/ErrorEnvelope"

    for path, method in ERROR_RESPONSE_OPERATIONS:
        operation = load_reports_operation(paths=paths, path=path, method=method)
        responses = cast(dict[str, object], operation.get("responses", {}))
        error_status_codes = sorted(
            status_code for status_code in responses if status_code.startswith(("4", "5"))
        )
        for status_code in error_status_codes:
            response = cast(dict[str, object], responses[status_code])
            if "$ref" in response:
                assert response["$ref"] == "#/components/responses/ErrorResponse"
                continue

            content = cast(dict[str, object], response.get("content", {}))
            app_json = cast(dict[str, object], content.get("application/json", {}))
            schema = cast(dict[str, object], app_json.get("schema", {}))
            schema_ref = cast(str, schema.get("$ref", ""))
            assert schema_ref in {expected_ref, accepted_legacy_ref}, (
                f"reports error contract drift: `{method.upper()} {path}` status `{status_code}` "
                "must reference canonical report error schema."
            )


def test_reports_error_contract_unknown_reason_code_guard() -> None:
    schemas = load_reports_schemas(load_reports_contract())
    reason_schema = cast(dict[str, object], schemas["ReportErrorReasonCode"])
    reason_codes = set(cast(list[str], reason_schema.get("enum", [])))
    assert (
        "unknown_reason_code" not in reason_codes
    ), "reports error contract drift: unknown reason code should not be declared."
