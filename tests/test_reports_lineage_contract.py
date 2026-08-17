"""Deterministic report-lineage contract drift guards for Phase 9.1.5."""

from __future__ import annotations

from typing import cast

from tests.test_reports_openapi_contract import load_reports_schemas
from tests.test_reports_openapi_contract import load_reports_contract

REQUIRED_LINEAGE_FIELDS = {
    "computation_id",
    "form_id",
    "report_id",
    "report_version_id",
    "historical_version_id",
    "supported_lane_id",
    "tax_type",
    "tax_year",
    "policy_anchor_ids",
    "source_anchor_ids",
}


def test_reports_lineage_contract_required_fields() -> None:
    schema = _lineage_schema()
    required_fields = set(cast(list[str], schema["required"]))
    assert (
        required_fields == REQUIRED_LINEAGE_FIELDS
    ), "reports lineage drift: required field set mismatch. " "Expected exact set: " + ", ".join(
        sorted(REQUIRED_LINEAGE_FIELDS)
    ) + "; actual: " + ", ".join(
        sorted(required_fields)
    )


def test_reports_lineage_array_and_identifier_types_are_contractual() -> None:
    properties = cast(dict[str, object], _lineage_schema()["properties"])

    report_id = cast(dict[str, object], properties["report_id"])
    assert report_id.get("type") == "string"
    assert report_id.get("format") == "uuid"

    policy_anchor_ids = cast(dict[str, object], properties["policy_anchor_ids"])
    source_anchor_ids = cast(dict[str, object], properties["source_anchor_ids"])
    assert policy_anchor_ids.get("type") == "array"
    assert source_anchor_ids.get("type") == "array"

    policy_items = cast(dict[str, object], policy_anchor_ids["items"])
    source_items = cast(dict[str, object], source_anchor_ids["items"])
    assert policy_items.get("type") == "string"
    assert source_items.get("type") == "string"


def test_reports_lineage_drift_guard_required_keys_cannot_be_removed_or_renamed() -> None:
    properties = cast(dict[str, object], _lineage_schema()["properties"])
    missing = sorted(REQUIRED_LINEAGE_FIELDS - set(properties))
    assert (
        not missing
    ), "reports lineage drift: missing required lineage property definition(s): " + ", ".join(
        missing
    )


def _lineage_schema() -> dict[str, object]:
    schemas = load_reports_schemas(load_reports_contract())
    lineage = schemas.get("ReportLineage")
    assert isinstance(lineage, dict), "reports lineage drift: ReportLineage schema missing."
    return cast(dict[str, object], lineage)
