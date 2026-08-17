"""Retention/expiry governance drift guard tests for Phase 9.1.4."""

from __future__ import annotations

import re
from typing import Any
from typing import cast
from pathlib import Path

import yaml

from tests.test_reports_openapi_contract import load_reports_paths
from tests.test_reports_openapi_contract import load_reports_schemas
from tests.test_reports_openapi_contract import load_reports_contract
from tests.test_reports_openapi_contract import load_reports_operation

POLICY_PATH = Path("docs/governance/phase-9-report-retention-and-expiry-policy.md")

REQUIRED_SECTIONS = {
    "## purpose",
    "## artifact_classes",
    "## policy_constants",
    "## lifecycle_states",
    "## deterministic_expiry_behavior",
    "## cleanup_trigger_semantics",
    "## scope_guard",
}

EXPECTED_ARTIFACT_RETENTION_DAYS = {
    "tax_summary": 2555,
    "worksheet": 2555,
    "comparative_view": 2555,
    "audit_package": 3650,
    "export_bundle": 365,
}

EXPECTED_DOWNLOAD_LINK_TTL_SECONDS = 900
EXPECTED_EXPIRED_ARTIFACT_REASON = "report_artifact_expired"
EXPECTED_EXPIRED_LINK_REASON = "download_link_expired"
EXPECTED_CLEANUP_SCHEDULE = "daily_utc_02_00"
EXPECTED_CLEANUP_MODE = "soft_delete_then_hard_delete_window"


def test_policy_doc_exists_and_contains_required_sections() -> None:
    assert POLICY_PATH.exists()
    content = POLICY_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_policy_constants_block_has_expected_keys_and_values() -> None:
    constants = _load_policy_constants()
    retention_days = cast(dict[str, int], constants.get("artifact_retention_days"))
    assert retention_days == EXPECTED_ARTIFACT_RETENTION_DAYS
    assert constants.get("download_link_ttl_seconds") == EXPECTED_DOWNLOAD_LINK_TTL_SECONDS
    assert constants.get("expired_artifact_reason_code") == EXPECTED_EXPIRED_ARTIFACT_REASON
    assert constants.get("expired_download_link_reason_code") == EXPECTED_EXPIRED_LINK_REASON

    cleanup_trigger = cast(dict[str, str], constants.get("cleanup_trigger"))
    assert cleanup_trigger.get("schedule") == EXPECTED_CLEANUP_SCHEDULE
    assert cleanup_trigger.get("mode") == EXPECTED_CLEANUP_MODE


def test_policy_constants_drift_guard_required_keys_cannot_be_removed_or_renamed() -> None:
    constants = _load_policy_constants()
    required_top_level_keys = {
        "artifact_retention_days",
        "download_link_ttl_seconds",
        "expired_artifact_reason_code",
        "expired_download_link_reason_code",
        "cleanup_trigger",
    }
    missing_top_level_keys = sorted(required_top_level_keys - set(constants))
    assert (
        not missing_top_level_keys
    ), "reports retention/expiry policy drift: missing required key(s): " + ", ".join(
        missing_top_level_keys
    )

    retention_days = cast(dict[str, Any], constants["artifact_retention_days"])
    missing_classes = sorted(EXPECTED_ARTIFACT_RETENTION_DAYS.keys() - set(retention_days))
    assert not missing_classes, (
        "reports retention/expiry policy drift: missing artifact class retention key(s): "
        + ", ".join(missing_classes)
    )


def test_reports_expiry_semantics_contract_mapping() -> None:
    document = load_reports_contract()
    schemas = load_reports_schemas(document)
    reason_codes = set(
        cast(list[str], cast(dict[str, object], schemas["ReportErrorReasonCode"]).get("enum", []))
    )
    assert EXPECTED_EXPIRED_ARTIFACT_REASON in reason_codes
    assert EXPECTED_EXPIRED_LINK_REASON in reason_codes

    paths = load_reports_paths(document)
    metadata_operation = load_reports_operation(
        paths=paths,
        path="/v1/reports/income-tax/artifacts/{report_id}/metadata",
        method="get",
    )
    export_operation = load_reports_operation(
        paths=paths,
        path="/v1/reports/income-tax/exports/{export_package_id}/metadata",
        method="get",
    )

    metadata_responses = cast(dict[str, object], metadata_operation.get("responses", {}))
    export_responses = cast(dict[str, object], export_operation.get("responses", {}))
    assert "410" in metadata_responses
    assert "410" in export_responses


def _load_policy_constants() -> dict[str, object]:
    content = POLICY_PATH.read_text(encoding="utf-8")
    block_match = re.search(r"```yaml\s*(.*?)\s*```", content, flags=re.DOTALL)
    assert (
        block_match is not None
    ), "reports retention/expiry policy drift: missing YAML constants block."
    loaded = yaml.safe_load(block_match.group(1))
    assert isinstance(
        loaded, dict
    ), "reports retention/expiry policy drift: constants block must parse to object."
    return cast(dict[str, object], loaded)
