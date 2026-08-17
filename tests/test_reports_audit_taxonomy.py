"""Drift-lock tests for canonical reports audit taxonomy."""

from __future__ import annotations

from typing import cast
from pathlib import Path

from tests.test_reports_openapi_contract import load_reports_schemas
from tests.test_reports_openapi_contract import load_reports_contract

GOVERNANCE_DOC_PATH = Path("docs/governance/phase-9-reports-audit-taxonomy.md")

REQUIRED_EVENT_TYPES = {
    "report_generated",
    "report_download_link_issued",
    "report_downloaded",
    "report_generation_failed",
}

REQUIRED_SHARED_FIELDS = {
    "event_id",
    "event_type",
    "occurred_at",
    "correlation_id",
    "report_id",
    "report_version_id",
    "tenant_id",
    "actor_id",
    "lineage",
}

REQUIRED_FAILURE_FIELDS = {
    "error_code",
    "reason_code",
    "reason",
}


def test_reports_audit_taxonomy_governance_doc_exists_and_declares_required_events() -> None:
    assert GOVERNANCE_DOC_PATH.exists(), "reports audit taxonomy doc is missing."
    content = GOVERNANCE_DOC_PATH.read_text(encoding="utf-8")
    for event_type in sorted(REQUIRED_EVENT_TYPES):
        assert f"`{event_type}`" in content


def test_reports_audit_taxonomy_event_type_set_is_exact() -> None:
    schemas = load_reports_schemas(load_reports_contract())
    event_type_schema = _schema_object(schemas["ReportAuditEventType"])
    enum_values = event_type_schema.get("enum")
    assert isinstance(enum_values, list)
    enum_values_list = cast(list[object], enum_values)
    assert {str(value) for value in enum_values_list} == REQUIRED_EVENT_TYPES


def test_reports_audit_taxonomy_shared_required_fields_exist() -> None:
    schemas = load_reports_schemas(load_reports_contract())
    event_schema = _schema_object(schemas["ReportAuditEvent"])
    required_fields = event_schema.get("required")
    assert isinstance(required_fields, list)
    required_fields_list = cast(list[object], required_fields)
    assert REQUIRED_SHARED_FIELDS.issubset({str(value) for value in required_fields_list})


def test_reports_audit_taxonomy_failure_fields_exist() -> None:
    schemas = load_reports_schemas(load_reports_contract())
    failure_schema = _schema_object(schemas["ReportAuditFailureEvent"])
    all_of = failure_schema.get("allOf")
    assert isinstance(all_of, list)
    all_of_list = cast(list[object], all_of)
    assert len(all_of_list) == 2
    failure_detail = _schema_object(all_of_list[1])
    required_fields = failure_detail.get("required")
    assert isinstance(required_fields, list)
    required_fields_list = cast(list[object], required_fields)
    assert REQUIRED_FAILURE_FIELDS == {str(value) for value in required_fields_list}


def _schema_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)
