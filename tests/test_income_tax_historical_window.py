"""Test deterministic income-tax computation for the next governed historical window."""

from __future__ import annotations

import copy
import json
from typing import cast
from pathlib import Path
from datetime import date

import pytest

from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest

GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_resident_employment_2021_01_01_case_001.json"
)


def test_bind_rule_selection_resolves_resident_historical_window() -> None:
    """Verify the 2021 resident employment window binds to its own governed pack."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2021,
            rule_version="v1",
            primary_effective_date=date(2021, 1, 1),
            historical_version_id="KIT-VER-20210101-A",
            resident_status_assertion="resident",
            income_category_signature="employment",
        )
    )

    assert bound_rule.binding_id == "income_tax_resident_employment_v1_2021_01_01"


def test_same_lane_binds_differently_across_2021_and_2023_windows() -> None:
    """Verify governed historical boundaries resolve to distinct bindings."""

    historical = bind_rule_selection(
        RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2021,
            rule_version="v1",
            primary_effective_date=date(2021, 1, 1),
            historical_version_id="KIT-VER-20210101-A",
            resident_status_assertion="resident",
            income_category_signature="employment",
        )
    )
    modern = bind_rule_selection(
        RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2023,
            rule_version="v1",
            primary_effective_date=date(2023, 7, 1),
            historical_version_id="KIT-VER-20230701-A",
            resident_status_assertion="resident",
            income_category_signature="employment",
        )
    )

    assert historical.binding_id == "income_tax_resident_employment_v1_2021_01_01"
    assert modern.binding_id == "income_tax_resident_employment_v1_2023_07_01"
    assert historical.binding_id != modern.binding_id


def test_execute_computation_returns_governed_resident_historical_payload() -> None:
    """Verify the 2021 resident historical request computes the governed result."""

    result = execute_computation(_build_supported_resident_request())
    result_payload = result.result_payload
    liability_summary = cast(dict[str, object], result_payload["liability_summary"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])

    assert result.status == "ok"
    assert result.tax_year == 2021
    assert result.rule_version == "v1"
    assert version_identity["historical_version_id"] == "KIT-VER-20210101-A"
    assert version_identity["effective_start"] == "2021-01-01"
    assert version_identity["effective_end"] == "2021-06-30"
    assert liability_summary == {
        "assessable_income_kes": "10000000.00",
        "chargeable_income_kes": "10000000.00",
        "gross_tax_kes": "2937400.00",
        "total_reliefs_kes": "28800.00",
        "creditable_withholding_kes": "0.00",
        "installment_tax_credit_kes": "0.00",
        "advance_tax_credit_kes": "0.00",
        "net_income_tax_due_kes": "2908600.00",
        "refund_due_kes": "0.00",
        "final_tax_excluded_income_kes": "0.00",
    }


def test_execute_computation_returns_governed_non_resident_historical_payload() -> None:
    """Verify the 2021 non-resident historical request computes the governed result."""

    result = execute_computation(_build_supported_non_resident_request())
    liability_summary = cast(dict[str, object], result.result_payload["liability_summary"])
    taxpayer_outcome = cast(dict[str, object], result.result_payload["taxpayer_outcome"])

    assert result.status == "ok"
    assert taxpayer_outcome["resident_status"] == "non_resident"
    assert liability_summary["gross_tax_kes"] == "2937400.00"
    assert liability_summary["total_reliefs_kes"] == "0.00"
    assert liability_summary["net_income_tax_due_kes"] == "2937400.00"


def test_execute_computation_rejects_date_outside_supported_historical_window() -> None:
    """Verify unsupported historical dates fail deterministically."""

    request_payload = _supported_resident_payload()
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    version_context = cast(dict[str, object], input_payload["version_context"])
    version_context["primary_effective_date"] = "2021-07-01"

    with pytest.raises(RuleBindingError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unknown_rule_binding"


def test_historical_execution_is_deterministic_for_logical_equivalent_requests() -> None:
    """Verify the 2021 historical request stays deterministic under key reordering."""

    first = execute_computation(_build_supported_resident_request()).model_dump(mode="json")
    second = execute_computation(
        ComputationExecutionRequest.model_validate(
            {
                "tax_type": "income_tax",
                "regime_type": "income_tax",
                "regime_identifier": None,
                "tax_year": 2021,
                "rule_version": "v1",
                "input_payload": {
                    "traceability_context": {
                        "preparation_profile": "manual_structured_entry",
                        "source_record_ids": ["src-payroll-2021-001"],
                        "completeness_assertion": "complete",
                        "evidence_reference_ids": [],
                    },
                    "payment_pathways": {
                        "advance_tax_events": [],
                        "installment_tax_events": [],
                        "withholding_events": [],
                    },
                    "claims": {
                        "exemption_claims": [],
                        "deduction_claims": [],
                        "relief_claims": [
                            {
                                "asserted_amount_kes": "28800.00",
                                "claim_reference_id": "claim-personal-relief-2021-001",
                                "relief_type": "personal_relief",
                            }
                        ],
                    },
                    "income_sections": {
                        "employment": {
                            "employment_items": [
                                {
                                    "event_date": "2021-03-15",
                                    "income_subtype": "cash_emolument",
                                    "amount_kes": "10000000.00",
                                    "employer_reference_id": "employer-2021-001",
                                }
                            ]
                        }
                    },
                    "taxpayer_context": {
                        "resident_status_assertion": "resident",
                        "taxpayer_kind": "individual",
                        "residence_reference_period_start": "2021-01-01",
                        "residence_reference_period_end": "2021-12-31",
                    },
                    "version_context": {
                        "historical_version_id": "KIT-VER-20210101-A",
                        "version_selection_basis": "specific_event_date",
                        "primary_effective_date": "2021-01-01",
                        "source_anchor_ids": ["ITA-2021-01-01-A"],
                    },
                },
            }
        )
    ).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


def test_historical_window_golden_fixture_matches_exact_output() -> None:
    """Verify the historical resident golden case locks exact output."""

    fixture = _load_golden_fixture()
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(fixture["request"])
    ).model_dump(mode="json")

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def test_historical_window_golden_fixture_detects_output_drift() -> None:
    """Verify the historical golden harness fails on output drift."""

    fixture = _load_golden_fixture()
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(cast(dict[str, object], fixture["request"]))
    ).model_dump(mode="json")
    drifted_expected_output = copy.deepcopy(cast(dict[str, object], fixture["expected_output"]))
    drifted_payload = cast(dict[str, object], drifted_expected_output["result_payload"])
    liability_summary = cast(dict[str, object], drifted_payload["liability_summary"])
    liability_summary["net_income_tax_due_kes"] = "2908601.00"

    with pytest.raises(AssertionError):
        assert _canonical_json(actual_output) == _canonical_json(drifted_expected_output)


def _build_supported_resident_request() -> ComputationExecutionRequest:
    return ComputationExecutionRequest.model_validate(_supported_resident_payload())


def _build_supported_non_resident_request() -> ComputationExecutionRequest:
    return ComputationExecutionRequest.model_validate(_supported_non_resident_payload())


def _supported_resident_payload() -> dict[str, object]:
    return {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2021,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": "2021-01-01",
                "version_selection_basis": "specific_event_date",
                "historical_version_id": "KIT-VER-20210101-A",
                "source_anchor_ids": ["ITA-2021-01-01-A"],
            },
            "taxpayer_context": {
                "taxpayer_kind": "individual",
                "resident_status_assertion": "resident",
                "residence_reference_period_start": "2021-01-01",
                "residence_reference_period_end": "2021-12-31",
            },
            "income_sections": {
                "employment": {
                    "employment_items": [
                        {
                            "income_subtype": "cash_emolument",
                            "amount_kes": "10000000.00",
                            "event_date": "2021-03-15",
                            "employer_reference_id": "employer-2021-001",
                        }
                    ]
                }
            },
            "claims": {
                "relief_claims": [
                    {
                        "relief_type": "personal_relief",
                        "claim_reference_id": "claim-personal-relief-2021-001",
                        "asserted_amount_kes": "28800.00",
                    }
                ],
                "deduction_claims": [],
                "exemption_claims": [],
            },
            "payment_pathways": {
                "withholding_events": [],
                "installment_tax_events": [],
                "advance_tax_events": [],
            },
            "traceability_context": {
                "source_record_ids": ["src-payroll-2021-001"],
                "preparation_profile": "manual_structured_entry",
                "completeness_assertion": "complete",
                "evidence_reference_ids": [],
            },
        },
    }


def _supported_non_resident_payload() -> dict[str, object]:
    return {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2021,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": "2021-01-01",
                "version_selection_basis": "specific_event_date",
                "historical_version_id": "KIT-VER-20210101-A",
                "source_anchor_ids": ["ITA-2021-01-01-A"],
            },
            "taxpayer_context": {
                "taxpayer_kind": "individual",
                "resident_status_assertion": "non_resident",
                "residence_reference_period_start": "2021-01-01",
                "residence_reference_period_end": "2021-12-31",
            },
            "income_sections": {
                "employment": {
                    "employment_items": [
                        {
                            "income_subtype": "cash_emolument",
                            "amount_kes": "10000000.00",
                            "event_date": "2021-03-15",
                            "employer_reference_id": "employer-2021-nr-001",
                        }
                    ]
                }
            },
            "claims": {
                "relief_claims": [],
                "deduction_claims": [],
                "exemption_claims": [],
            },
            "payment_pathways": {
                "withholding_events": [],
                "installment_tax_events": [],
                "advance_tax_events": [],
            },
            "traceability_context": {
                "source_record_ids": ["src-payroll-2021-nr-001"],
                "preparation_profile": "manual_structured_entry",
                "completeness_assertion": "complete",
                "evidence_reference_ids": [],
            },
        },
    }


def _load_golden_fixture() -> dict[str, object]:
    with GOLDEN_CASE_PATH.open("r", encoding="utf-8") as fixture_file:
        fixture = json.load(fixture_file)
    return cast(dict[str, object], fixture)


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
