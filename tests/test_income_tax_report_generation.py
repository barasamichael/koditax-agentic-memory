"""Verify deterministic income-tax report generation for supported phase-5 lanes."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.report_generation import generate_income_tax_report
from services.forms.app.income_tax.report_generation import IncomeTaxReportGenerationError
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T19:00:00+03:00"


@pytest.mark.parametrize(
    ("fixture_name", "expected_lane_id", "expected_historical_version_id"),
    [
        (
            "income_tax_resident_employment_2021_01_01_case_001.json",
            "resident_employment_income_2021_01_01",
            "KIT-VER-20210101-A",
        ),
        (
            "income_tax_non_resident_employment_2021_01_01_case_001.json",
            "non_resident_employment_income_2021_01_01",
            "KIT-VER-20210101-A",
        ),
        (
            "income_tax_resident_employment_2023_07_01_case_001.json",
            "resident_employment_income_2023_07_01",
            "KIT-VER-20230701-A",
        ),
        (
            "income_tax_non_resident_employment_2023_07_01_case_001.json",
            "non_resident_employment_income_2023_07_01",
            "KIT-VER-20230701-A",
        ),
        (
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json",
            "resident_employment_plus_qualifying_interest_2023_07_01",
            "KIT-VER-20230701-A",
        ),
    ],
)
def test_supported_lanes_generate_deterministic_reports(
    fixture_name: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    form_artifact = _build_form_artifact(fixture_name)

    report_output = generate_income_tax_report(form_artifact_output=form_artifact)

    assert report_output["generation_status"] == "generated"
    assert report_output["report_type"] == "income_tax_computation_report"
    assert report_output["report_version"] == "income_tax_vertical_slice_report_v1"
    assert report_output["form_artifact_id"] == form_artifact["artifact_id"]
    assert report_output["computation_id"] == form_artifact["computation_id"]
    assert report_output["supported_lane_id"] == expected_lane_id
    assert report_output["historical_version_id"] == expected_historical_version_id
    assert report_output["tax_year"] == form_artifact["tax_year"]
    assert report_output["report_id"]

    human_summary = _as_object(report_output["human_readable_summary"])
    machine_summary = _as_object(report_output["machine_usable_summary"])
    audit_evidence = _as_object(report_output["audit_evidence"])

    assert human_summary["supported_lane_id"] == expected_lane_id
    assert human_summary["historical_version_id"] == expected_historical_version_id
    assert machine_summary["liability_fields"]
    assert audit_evidence["action"] == "report_generation"
    assert audit_evidence["action_status"] == "generated"
    assert audit_evidence["report_id"] == report_output["report_id"]


def test_mixed_income_lane_report_includes_investment_and_final_tax_fields() -> None:
    form_artifact = _build_form_artifact(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )

    report_output = generate_income_tax_report(form_artifact_output=form_artifact)
    machine_summary = _as_object(report_output["machine_usable_summary"])
    form_fields = _as_object(machine_summary["form_fields"])

    assert (
        report_output["supported_lane_id"]
        == "resident_employment_plus_qualifying_interest_2023_07_01"
    )
    assert form_fields["investment_income_kes"] == "120000.00"
    assert form_fields["investment_final_tax_amount_kes"] == "18000.00"


def test_report_generation_rejects_non_generated_artifact() -> None:
    form_artifact = _build_form_artifact("income_tax_resident_employment_2023_07_01_case_001.json")
    form_artifact["generation_status"] = "failed"

    with pytest.raises(IncomeTaxReportGenerationError) as error_info:
        generate_income_tax_report(form_artifact_output=form_artifact)

    assert error_info.value.reason == "upstream_artifact_not_generated"


def test_report_generation_rejects_non_finalized_upstream_lineage() -> None:
    form_artifact = _build_form_artifact(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    lineage = _as_object(form_artifact["lineage"])
    lineage["finalization_status"] = "draft"
    form_artifact["lineage"] = lineage

    with pytest.raises(IncomeTaxReportGenerationError) as error_info:
        generate_income_tax_report(form_artifact_output=form_artifact)

    assert error_info.value.reason == "upstream_not_finalized"


def test_report_generation_rejects_incomplete_artifact_payload() -> None:
    form_artifact = _build_form_artifact("income_tax_resident_employment_2021_01_01_case_001.json")
    del form_artifact["generated_content_payload"]

    with pytest.raises(IncomeTaxReportGenerationError) as error_info:
        generate_income_tax_report(form_artifact_output=form_artifact)

    assert error_info.value.reason == "missing_required_field"
    assert error_info.value.details()["field_name"] == "generated_content_payload"


def test_report_generation_rejects_unsupported_lane_context() -> None:
    form_artifact = _build_form_artifact("income_tax_resident_employment_2023_07_01_case_001.json")
    form_artifact["supported_lane_id"] = "resident_employment_income_2022_01_01"

    with pytest.raises(IncomeTaxReportGenerationError) as error_info:
        generate_income_tax_report(form_artifact_output=form_artifact)

    assert error_info.value.reason == "unsupported_report_scope"


def test_report_generation_rejects_non_income_tax_form_scope() -> None:
    form_artifact = _build_form_artifact("income_tax_resident_employment_2023_07_01_case_001.json")
    form_artifact["form_type"] = "vat_return"

    with pytest.raises(IncomeTaxReportGenerationError) as error_info:
        generate_income_tax_report(form_artifact_output=form_artifact)

    assert error_info.value.reason == "unsupported_form_scope"


def test_report_generation_is_deterministic_for_same_input() -> None:
    form_artifact = _build_form_artifact(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )

    first = generate_income_tax_report(form_artifact_output=copy.deepcopy(form_artifact))
    second = generate_income_tax_report(form_artifact_output=copy.deepcopy(form_artifact))

    assert second == first


def _build_form_artifact(fixture_name: str) -> dict[str, object]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    return generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )


def _build_finalized_output(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = fixture["fixture_id"]
    expected_output = copy.deepcopy(fixture["expected_output"])

    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": FINALIZED_AT,
        "finalized_audit_event_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:finalized-audit")),
        "tax_type": expected_output["tax_type"],
        "regime_type": expected_output["regime_type"],
        "tax_year": expected_output["tax_year"],
        "rule_version": expected_output["rule_version"],
        "input_hash": expected_output["input_hash"],
        "result_payload": expected_output["result_payload"],
    }


def _as_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)
