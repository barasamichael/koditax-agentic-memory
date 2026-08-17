"""Verify deterministic submission payload construction for supported income-tax lanes."""

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
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.forms.app.income_tax.submission_payload_construction import (
    construct_income_tax_submission_payload,
)
from services.forms.app.income_tax.submission_payload_construction import (
    IncomeTaxSubmissionPayloadConstructionError,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T21:00:00+03:00"


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
def test_supported_lanes_construct_deterministic_submission_payloads(
    fixture_name: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    report_output, report_binding = _build_report_and_binding_outputs(fixture_name)

    payload_output = construct_income_tax_submission_payload(
        report_output=report_output,
        report_version_binding=report_binding,
    )

    assert payload_output["construction_status"] == "constructed"
    assert payload_output["payload_type"] == "income_tax_submission_payload"
    assert payload_output["payload_version"] == "income_tax_submission_payload_v1"
    assert payload_output["report_id"] == report_output["report_id"]
    assert payload_output["form_artifact_id"] == report_output["form_artifact_id"]
    assert payload_output["computation_id"] == report_output["computation_id"]
    assert payload_output["supported_lane_id"] == expected_lane_id
    assert payload_output["historical_version_id"] == expected_historical_version_id
    assert payload_output["tax_year"] == report_output["tax_year"]
    assert payload_output["payload_id"]

    filing_payload = _as_object(payload_output["machine_usable_filing_payload"])
    filing_header = _as_object(filing_payload["filing_header"])
    audit_evidence = _as_object(payload_output["audit_evidence"])

    assert filing_header["supported_lane_id"] == expected_lane_id
    assert filing_header["historical_version_id"] == expected_historical_version_id
    assert filing_header["report_version_id"] == report_binding["report_version_id"]
    assert audit_evidence["action"] == "submission_payload_construction"
    assert audit_evidence["action_status"] == "constructed"
    assert audit_evidence["payload_id"] == payload_output["payload_id"]


def test_mixed_income_submission_payload_preserves_investment_filing_fields() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )

    payload_output = construct_income_tax_submission_payload(
        report_output=report_output,
        report_version_binding=report_binding,
    )
    filing_payload = _as_object(payload_output["machine_usable_filing_payload"])
    form_fields = _as_object(filing_payload["form_fields"])

    assert (
        payload_output["supported_lane_id"]
        == "resident_employment_plus_qualifying_interest_2023_07_01"
    )
    assert form_fields["investment_income_kes"] == "120000.00"
    assert form_fields["investment_final_tax_amount_kes"] == "18000.00"


def test_submission_payload_rejects_non_generated_report_output() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    report_output["generation_status"] = "draft"

    with pytest.raises(IncomeTaxSubmissionPayloadConstructionError) as error_info:
        construct_income_tax_submission_payload(
            report_output=report_output,
            report_version_binding=report_binding,
        )

    assert error_info.value.reason == "invalid_report_generation_status"


def test_submission_payload_rejects_unbound_report_version_output() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    report_binding["binding_status"] = "pending"

    with pytest.raises(IncomeTaxSubmissionPayloadConstructionError) as error_info:
        construct_income_tax_submission_payload(
            report_output=report_output,
            report_version_binding=report_binding,
        )

    assert error_info.value.reason == "invalid_report_binding_status"


def test_submission_payload_rejects_unsupported_lane_context() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    report_output["supported_lane_id"] = "resident_employment_income_2022_01_01"
    report_binding["supported_lane_id"] = "resident_employment_income_2022_01_01"

    with pytest.raises(IncomeTaxSubmissionPayloadConstructionError) as error_info:
        construct_income_tax_submission_payload(
            report_output=report_output,
            report_version_binding=report_binding,
        )

    assert error_info.value.reason == "unsupported_submission_payload_scope"


def test_submission_payload_rejects_incomplete_upstream_payload() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    del report_output["machine_usable_summary"]

    with pytest.raises(IncomeTaxSubmissionPayloadConstructionError) as error_info:
        construct_income_tax_submission_payload(
            report_output=report_output,
            report_version_binding=report_binding,
        )

    assert error_info.value.reason == "missing_required_field"
    assert error_info.value.details()["field_name"] == "machine_usable_summary"


def test_submission_payload_rejects_non_income_tax_report_scope() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    report_output["report_type"] = "vat_report"
    report_binding["report_type"] = "vat_report"

    with pytest.raises(IncomeTaxSubmissionPayloadConstructionError) as error_info:
        construct_income_tax_submission_payload(
            report_output=report_output,
            report_version_binding=report_binding,
        )

    assert error_info.value.reason == "unsupported_report_scope"


def test_submission_payload_construction_is_deterministic_for_same_inputs() -> None:
    report_output, report_binding = _build_report_and_binding_outputs(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )

    first = construct_income_tax_submission_payload(
        report_output=copy.deepcopy(report_output),
        report_version_binding=copy.deepcopy(report_binding),
    )
    second = construct_income_tax_submission_payload(
        report_output=copy.deepcopy(report_output),
        report_version_binding=copy.deepcopy(report_binding),
    )

    assert second == first


def _build_report_and_binding_outputs(
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    form_artifact_output = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )
    report_output = generate_income_tax_report(form_artifact_output=form_artifact_output)
    report_binding = bind_income_tax_report_version(report_output)
    return report_output, report_binding


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
