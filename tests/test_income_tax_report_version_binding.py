"""Verify deterministic report-version binding for supported income-tax report outputs."""

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
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.report_version_binding import bind_income_tax_report_version
from services.forms.app.income_tax.report_version_binding import IncomeTaxReportVersionBindingError
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T20:00:00+03:00"


@pytest.mark.parametrize(
    ("fixture_name", "expected_report_version_id", "expected_report_template_id"),
    [
        (
            "income_tax_resident_employment_2021_01_01_case_001.json",
            "ITX-RPT-20210101-RES-EMP-V1",
            "income_tax_report_resident_employment_2021_01_01_v1",
        ),
        (
            "income_tax_non_resident_employment_2021_01_01_case_001.json",
            "ITX-RPT-20210101-NRES-EMP-V1",
            "income_tax_report_non_resident_employment_2021_01_01_v1",
        ),
        (
            "income_tax_resident_employment_2023_07_01_case_001.json",
            "ITX-RPT-20230701-RES-EMP-V1",
            "income_tax_report_resident_employment_2023_07_01_v1",
        ),
        (
            "income_tax_non_resident_employment_2023_07_01_case_001.json",
            "ITX-RPT-20230701-NRES-EMP-V1",
            "income_tax_report_non_resident_employment_2023_07_01_v1",
        ),
        (
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json",
            "ITX-RPT-20230701-RES-EMP-QINT-V1",
            "income_tax_report_resident_employment_plus_qualifying_interest_2023_07_01_v1",
        ),
    ],
)
def test_supported_report_lanes_bind_to_expected_report_versions(
    fixture_name: str,
    expected_report_version_id: str,
    expected_report_template_id: str,
) -> None:
    report_output = _build_report_output(fixture_name)

    binding = bind_income_tax_report_version(report_output)
    binding_lineage = _as_object(binding["binding_lineage"])

    assert binding["binding_status"] == "bound"
    assert binding["report_type"] == "income_tax_computation_report"
    assert binding["report_version"] == "income_tax_vertical_slice_report_v1"
    assert binding["report_version_id"] == expected_report_version_id
    assert binding["report_template_id"] == expected_report_template_id
    assert binding["report_id"] == report_output["report_id"]
    assert binding["form_artifact_id"] == report_output["form_artifact_id"]
    assert binding["computation_id"] == report_output["computation_id"]
    report_audit_evidence = _as_object(report_output["audit_evidence"])
    assert binding_lineage["report_audit_evidence_id"] == report_audit_evidence["audit_evidence_id"]


def test_mixed_income_report_binding_preserves_historical_context() -> None:
    report_output = _build_report_output(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )

    binding = bind_income_tax_report_version(report_output)

    assert binding["supported_lane_id"] == "resident_employment_plus_qualifying_interest_2023_07_01"
    assert binding["historical_version_id"] == "KIT-VER-20230701-A"
    assert binding["tax_year"] == 2023


def test_report_binding_rejects_unsupported_historical_context() -> None:
    report_output = _build_report_output("income_tax_resident_employment_2023_07_01_case_001.json")
    report_output["historical_version_id"] = "KIT-VER-20200901-A"

    with pytest.raises(IncomeTaxReportVersionBindingError) as error_info:
        bind_income_tax_report_version(report_output)

    assert error_info.value.reason == "unsupported_report_version_binding"


def test_report_binding_rejects_unsupported_report_type_scope() -> None:
    report_output = _build_report_output("income_tax_resident_employment_2023_07_01_case_001.json")
    report_output["report_type"] = "vat_report"

    with pytest.raises(IncomeTaxReportVersionBindingError) as error_info:
        bind_income_tax_report_version(report_output)

    assert error_info.value.reason == "unsupported_report_type"


def test_report_binding_rejects_non_generated_report_output() -> None:
    report_output = _build_report_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    report_output["generation_status"] = "draft"

    with pytest.raises(IncomeTaxReportVersionBindingError) as error_info:
        bind_income_tax_report_version(report_output)

    assert error_info.value.reason == "invalid_report_generation_status"


def test_report_binding_is_deterministic_for_same_report_output() -> None:
    report_output = _build_report_output(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )

    first = bind_income_tax_report_version(copy.deepcopy(report_output))
    second = bind_income_tax_report_version(copy.deepcopy(report_output))

    assert second == first


def _build_report_output(fixture_name: str) -> dict[str, object]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    form_artifact_output = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )
    return generate_income_tax_report(form_artifact_output=form_artifact_output)


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
