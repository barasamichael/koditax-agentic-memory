"""Test governed mixed-income computation for supported income-tax categories."""

from __future__ import annotations

import copy
import json
from typing import cast
from pathlib import Path
from datetime import date

import pytest

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest

GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
)


def test_bind_rule_selection_resolves_supported_mixed_income_window() -> None:
    """Verify supported mixed-income signature binds to the mixed-income rule pack."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2023,
            rule_version="v1",
            primary_effective_date=date(2023, 7, 1),
            historical_version_id="KIT-VER-20230701-A",
            resident_status_assertion="resident",
            income_category_signature="employment+investment",
        )
    )

    assert (
        bound_rule.binding_id
        == "income_tax_resident_employment_plus_qualifying_interest_v1_2023_07_01"
    )


def test_execute_computation_returns_governed_mixed_income_payload() -> None:
    """Verify the supported mixed-income request computes the governed aggregate result."""

    result = execute_computation(_build_supported_request())
    result_payload = result.result_payload
    domain_outcomes = cast(dict[str, object], result_payload["domain_outcomes"])
    liability_summary = cast(dict[str, object], result_payload["liability_summary"])
    treatment_decisions = cast(dict[str, object], result_payload["treatment_decisions"])
    traceability = cast(dict[str, object], result_payload["traceability"])

    assert result.status == "ok"
    assert domain_outcomes["employment"] == {
        "status": "computed",
        "taxable_base_kes": "960000.00",
        "gross_tax_kes": "225400.00",
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": "0.00",
        "decision_refs": [
            "REM-20230701-005",
            "REM-20230701-010",
            "REM-20230701-012",
        ],
    }
    assert domain_outcomes["investment"] == {
        "status": "computed",
        "taxable_base_kes": "120000.00",
        "gross_tax_kes": "18000.00",
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": "18000.00",
        "decision_refs": [
            "MIX-REMP-QINT-20230701-004",
            "MIX-REMP-QINT-20230701-005",
            "MIX-REMP-QINT-20230701-006",
            "MIX-REMP-QINT-20230701-010",
        ],
    }
    assert domain_outcomes["withholding"] == {
        "status": "computed",
        "taxable_base_kes": None,
        "gross_tax_kes": None,
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": "18000.00",
        "decision_refs": [
            "ITC-POL-601",
            "ITC-POL-602",
            "MIX-REMP-QINT-20230701-010",
        ],
    }
    assert liability_summary == {
        "assessable_income_kes": "1080000.00",
        "chargeable_income_kes": "960000.00",
        "gross_tax_kes": "243400.00",
        "total_reliefs_kes": "28800.00",
        "creditable_withholding_kes": "0.00",
        "installment_tax_credit_kes": "0.00",
        "advance_tax_credit_kes": "0.00",
        "net_income_tax_due_kes": "214600.00",
        "refund_due_kes": "0.00",
        "final_tax_excluded_income_kes": "120000.00",
    }
    assert treatment_decisions["withholding_treatments"] == [
        {
            "income_reference_id": "payer-interest-001",
            "treatment": "final_tax",
            "decision_ref": "MIX-REMP-QINT-20230701-010",
        }
    ]
    assert traceability["validation_focus_domains"] == [
        "ITD-CORE-EMPLOYMENT",
        "ITD-CORE-INVESTMENT",
        "ITD-CORE-WHT",
        "ITD-CORE-DEDUCTIONS",
        "ITD-CORE-RELIEFS",
    ]
    assert traceability["input_hash"] == result.input_hash


def test_execute_computation_rejects_unsupported_mixed_category_combination() -> None:
    """Verify unsupported category combinations fail deterministically at the binding boundary."""

    payload = _supported_request_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    income_sections = cast(dict[str, object], input_payload["income_sections"])
    income_sections["business"] = {
        "business_items": [
            {
                "income_subtype": "trade",
                "gross_amount_kes": "1000.00",
                "event_date": "2023-07-20",
            }
        ]
    }

    with pytest.raises(RuleBindingError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unknown_rule_binding"


def test_execute_computation_rejects_unsupported_investment_subtype() -> None:
    """Verify unsupported investment subtype is not silently normalized."""

    payload = _supported_request_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    income_sections = cast(dict[str, object], input_payload["income_sections"])
    investment = cast(dict[str, object], income_sections["investment"])
    investment_items = cast(list[dict[str, object]], investment["investment_items"])
    investment_items[0]["income_subtype"] = "dividend"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_investment_income_subtype"


def test_mixed_income_execution_is_deterministic_for_logical_equivalent_requests() -> None:
    """Verify supported mixed-income requests stay deterministic under key reordering."""

    first = execute_computation(_build_supported_request()).model_dump(mode="json")
    second = execute_computation(
        ComputationExecutionRequest.model_validate(
            {
                "tax_type": "income_tax",
                "regime_type": "income_tax",
                "regime_identifier": None,
                "tax_year": 2023,
                "rule_version": "v1",
                "input_payload": {
                    "traceability_context": {
                        "preparation_profile": "manual_structured_entry",
                        "source_record_ids": [
                            "src-payroll-001",
                            "src-interest-001",
                        ],
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
                                "claim_reference_id": "claim-personal-relief-001",
                                "relief_type": "personal_relief",
                            }
                        ],
                    },
                    "income_sections": {
                        "investment": {
                            "investment_items": [
                                {
                                    "event_date": "2023-07-20",
                                    "gross_amount_kes": "120000.00",
                                    "income_subtype": "interest",
                                    "payer_reference_id": "payer-interest-001",
                                    "withholding_applied_kes": "18000.00",
                                }
                            ]
                        },
                        "employment": {
                            "employment_items": [
                                {
                                    "event_date": "2023-07-15",
                                    "income_subtype": "cash_emolument",
                                    "amount_kes": "960000.00",
                                    "employer_reference_id": "employer-001",
                                }
                            ]
                        },
                    },
                    "taxpayer_context": {
                        "resident_status_assertion": "resident",
                        "taxpayer_kind": "individual",
                        "residence_reference_period_start": "2023-01-01",
                        "residence_reference_period_end": "2023-12-31",
                    },
                    "version_context": {
                        "historical_version_id": "KIT-VER-20230701-A",
                        "version_selection_basis": "specific_event_date",
                        "primary_effective_date": "2023-07-01",
                        "source_anchor_ids": ["ITA-2023-07-01-A"],
                    },
                },
            }
        )
    ).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


def test_mixed_income_golden_fixture_matches_exact_output() -> None:
    """Verify the mixed-income golden case locks exact governed output."""

    fixture = _load_golden_fixture()
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(fixture["request"])
    ).model_dump(mode="json")

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def test_mixed_income_golden_fixture_detects_output_drift() -> None:
    """Verify mixed-income golden harness fails on drift."""

    fixture = _load_golden_fixture()
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(cast(dict[str, object], fixture["request"]))
    ).model_dump(mode="json")
    drifted_expected_output = copy.deepcopy(cast(dict[str, object], fixture["expected_output"]))
    drifted_payload = cast(dict[str, object], drifted_expected_output["result_payload"])
    liability_summary = cast(dict[str, object], drifted_payload["liability_summary"])
    liability_summary["net_income_tax_due_kes"] = "214601.00"

    with pytest.raises(AssertionError):
        assert _canonical_json(actual_output) == _canonical_json(drifted_expected_output)


def _build_supported_request() -> ComputationExecutionRequest:
    return ComputationExecutionRequest.model_validate(_supported_request_payload())


def _supported_request_payload() -> dict[str, object]:
    return {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2023,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": "2023-07-01",
                "version_selection_basis": "specific_event_date",
                "historical_version_id": "KIT-VER-20230701-A",
                "source_anchor_ids": ["ITA-2023-07-01-A"],
            },
            "taxpayer_context": {
                "taxpayer_kind": "individual",
                "resident_status_assertion": "resident",
                "residence_reference_period_start": "2023-01-01",
                "residence_reference_period_end": "2023-12-31",
            },
            "income_sections": {
                "employment": {
                    "employment_items": [
                        {
                            "income_subtype": "cash_emolument",
                            "amount_kes": "960000.00",
                            "event_date": "2023-07-15",
                            "employer_reference_id": "employer-001",
                        }
                    ]
                },
                "investment": {
                    "investment_items": [
                        {
                            "income_subtype": "interest",
                            "gross_amount_kes": "120000.00",
                            "event_date": "2023-07-20",
                            "withholding_applied_kes": "18000.00",
                            "payer_reference_id": "payer-interest-001",
                        }
                    ]
                },
            },
            "claims": {
                "relief_claims": [
                    {
                        "relief_type": "personal_relief",
                        "claim_reference_id": "claim-personal-relief-001",
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
                "source_record_ids": ["src-payroll-001", "src-interest-001"],
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
