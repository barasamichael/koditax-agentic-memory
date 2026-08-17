"""Verify deterministic forms mapping over supported governed income-tax lanes."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import FORM_TYPE
from services.forms.app.income_tax.form_mapping import FORM_VERSION
from services.forms.app.income_tax.form_mapping import IncomeTaxFormMappingError
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-15T09:00:00+03:00"


@pytest.mark.parametrize(
    ("fixture_name", "expected_lane_id", "expected_resident_status"),
    [
        (
            "income_tax_resident_employment_2021_01_01_case_001.json",
            "resident_employment_income_2021_01_01",
            "resident",
        ),
        (
            "income_tax_non_resident_employment_2021_01_01_case_001.json",
            "non_resident_employment_income_2021_01_01",
            "non_resident",
        ),
        (
            "income_tax_resident_employment_2023_07_01_case_001.json",
            "resident_employment_income_2023_07_01",
            "resident",
        ),
        (
            "income_tax_non_resident_employment_2023_07_01_case_001.json",
            "non_resident_employment_income_2023_07_01",
            "non_resident",
        ),
        (
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json",
            "resident_employment_plus_qualifying_interest_2023_07_01",
            "resident",
        ),
    ],
)
def test_supported_income_tax_lanes_map_to_form_ready_structure(
    fixture_name: str,
    expected_lane_id: str,
    expected_resident_status: str,
) -> None:
    finalized_output = _build_finalized_output(fixture_name)

    mapped = map_finalized_income_tax_output_to_form_ready(finalized_output)

    result_payload = _as_object(finalized_output["result_payload"])
    version_identity = _as_object(result_payload["version_identity"])
    liability_summary = _as_object(result_payload["liability_summary"])
    taxpayer_outcome = _as_object(result_payload["taxpayer_outcome"])
    computation_identity = _as_object(mapped["computation_identity"])
    mapped_version_identity = _as_object(mapped["version_identity"])
    taxpayer = _as_object(mapped["taxpayer"])
    liability_fields = _as_object(mapped["liability_fields"])
    lineage = _as_object(mapped["lineage"])

    assert mapped["mapping_status"] == "ok"
    assert mapped["form_type"] == FORM_TYPE
    assert mapped["form_version"] == FORM_VERSION
    assert mapped["supported_lane_id"] == expected_lane_id
    assert computation_identity["computation_id"] == finalized_output["computation_id"]
    assert computation_identity["finalization_status"] == "finalized"
    assert (
        mapped_version_identity["historical_version_id"]
        == version_identity["historical_version_id"]
    )
    assert taxpayer["resident_status"] == expected_resident_status
    assert taxpayer["classification_outcome"] == taxpayer_outcome["classification_outcome"]
    assert liability_fields["net_income_tax_due_kes"] == liability_summary["net_income_tax_due_kes"]
    assert lineage["source_anchor_ids"] == version_identity["source_anchor_ids"]
    assert mapped["unsupported_fields"] == []


def test_mixed_income_lane_maps_explicit_investment_final_tax_fields() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )

    mapped = map_finalized_income_tax_output_to_form_ready(finalized_output)
    domain_fields = _as_object(mapped["domain_fields"])
    investment_fields = _as_object(domain_fields["investment"])
    form_fields = _as_object(mapped["form_fields"])
    treatment_fields = _as_object(mapped["treatment_fields"])

    assert investment_fields["status"] == "computed"
    assert form_fields["investment_income_kes"] == "120000.00"
    assert form_fields["investment_final_tax_amount_kes"] == "18000.00"
    assert treatment_fields["withholding_treatments"] == [
        {
            "decision_ref": "MIX-REMP-QINT-20230701-010",
            "income_reference_id": "payer-interest-001",
            "treatment": "final_tax",
        }
    ]


def test_mapping_requires_finalized_computation_identity() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    del finalized_output["finalized_audit_event_id"]

    with pytest.raises(IncomeTaxFormMappingError) as error_info:
        map_finalized_income_tax_output_to_form_ready(finalized_output)

    assert error_info.value.reason == "missing_required_field"
    assert error_info.value.details() == {
        "reason": "missing_required_field",
        "field_name": "finalized_audit_event_id",
    }


def test_mapping_rejects_unresolved_supported_scope_gaps() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    result_payload = _as_object(finalized_output["result_payload"])
    result_payload["unsupported_or_unresolved"] = [
        {
            "domain_id": "ITD-CORE-EMPLOYMENT",
            "reason": "ambiguous_input",
        }
    ]
    finalized_output["result_payload"] = result_payload

    with pytest.raises(IncomeTaxFormMappingError) as error_info:
        map_finalized_income_tax_output_to_form_ready(finalized_output)

    assert error_info.value.reason == "unsupported_result_scope"


def test_mapping_rejects_non_income_tax_outputs() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    finalized_output["tax_type"] = "vat"

    with pytest.raises(IncomeTaxFormMappingError) as error_info:
        map_finalized_income_tax_output_to_form_ready(finalized_output)

    assert error_info.value.reason == "unsupported_tax_type"
    assert error_info.value.details() == {
        "reason": "unsupported_tax_type",
        "tax_type": "vat",
    }


def test_mapping_rejects_unsupported_domain_computation() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    result_payload = _as_object(finalized_output["result_payload"])
    domain_outcomes = _as_object(result_payload["domain_outcomes"])
    business_domain = _as_object(domain_outcomes["business"])
    business_domain["status"] = "computed"
    domain_outcomes["business"] = business_domain
    result_payload["domain_outcomes"] = domain_outcomes
    finalized_output["result_payload"] = result_payload

    with pytest.raises(IncomeTaxFormMappingError) as error_info:
        map_finalized_income_tax_output_to_form_ready(finalized_output)

    assert error_info.value.reason == "unsupported_income_lane"


def test_mapping_rejects_non_resident_relief_mismatch() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )
    result_payload = _as_object(finalized_output["result_payload"])
    liability_summary = _as_object(result_payload["liability_summary"])
    liability_summary["total_reliefs_kes"] = "28800.00"
    result_payload["liability_summary"] = liability_summary
    finalized_output["result_payload"] = result_payload

    with pytest.raises(IncomeTaxFormMappingError) as error_info:
        map_finalized_income_tax_output_to_form_ready(finalized_output)

    assert error_info.value.reason == "resident_status_relief_mismatch"


def test_mapping_is_deterministic_for_same_finalized_output() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )

    first = map_finalized_income_tax_output_to_form_ready(copy.deepcopy(finalized_output))
    second = map_finalized_income_tax_output_to_form_ready(copy.deepcopy(finalized_output))

    assert second == first


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
