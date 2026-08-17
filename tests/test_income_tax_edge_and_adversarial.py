"""Harden supported income-tax lanes with deterministic edge and adversarial proofs."""

from __future__ import annotations

from typing import cast
from datetime import date

import pytest

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest


@pytest.mark.parametrize(
    ("amount_kes", "expected_gross_tax_kes", "expected_net_tax_due_kes"),
    [
        ("288000.00", "28800.00", "0.00"),
        ("288001.00", "28800.25", "0.25"),
        ("388000.00", "53800.00", "25000.00"),
        ("388001.00", "53800.30", "25000.30"),
    ],
)
def test_resident_2023_threshold_boundaries_compute_exact_expected_amounts(
    amount_kes: str,
    expected_gross_tax_kes: str,
    expected_net_tax_due_kes: str,
) -> None:
    """Verify 2023 resident employment lane is exact at and just over band thresholds."""

    payload = _resident_2023_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    employment_items = cast(
        list[dict[str, object]],
        _employment_section_from_input(input_payload)["employment_items"],
    )
    employment_items[0]["amount_kes"] = amount_kes

    result = execute_computation(ComputationExecutionRequest.model_validate(payload))
    liability_summary = cast(dict[str, object], result.result_payload["liability_summary"])

    assert liability_summary["gross_tax_kes"] == expected_gross_tax_kes
    assert liability_summary["net_income_tax_due_kes"] == expected_net_tax_due_kes


@pytest.mark.parametrize(
    ("amount_kes", "expected_gross_tax_kes", "expected_net_tax_due_kes"),
    [
        ("288000.00", "28800.00", "0.00"),
        ("288001.00", "28800.25", "0.25"),
        ("388000.00", "53800.00", "25000.00"),
        ("388001.00", "53800.30", "25000.30"),
    ],
)
def test_resident_2021_threshold_boundaries_compute_exact_expected_amounts(
    amount_kes: str,
    expected_gross_tax_kes: str,
    expected_net_tax_due_kes: str,
) -> None:
    """Verify 2021 resident historical lane is exact at and just over band thresholds."""

    payload = _resident_2021_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    employment_items = cast(
        list[dict[str, object]],
        _employment_section_from_input(input_payload)["employment_items"],
    )
    employment_items[0]["amount_kes"] = amount_kes

    result = execute_computation(ComputationExecutionRequest.model_validate(payload))
    liability_summary = cast(dict[str, object], result.result_payload["liability_summary"])

    assert liability_summary["gross_tax_kes"] == expected_gross_tax_kes
    assert liability_summary["net_income_tax_due_kes"] == expected_net_tax_due_kes


@pytest.mark.parametrize(
    ("effective_date", "expected_binding_id"),
    [
        (date(2023, 7, 1), "income_tax_resident_employment_v1_2023_07_01"),
        (date(2023, 8, 31), "income_tax_resident_employment_v1_2023_07_01"),
        (date(2021, 1, 1), "income_tax_resident_employment_v1_2021_01_01"),
        (date(2021, 6, 30), "income_tax_resident_employment_v1_2021_01_01"),
    ],
)
def test_effective_window_edges_bind_exact_supported_rules(
    effective_date: date,
    expected_binding_id: str,
) -> None:
    """Verify supported historical/current windows bind correctly on exact start/end dates."""

    tax_year = effective_date.year
    historical_version_id = "KIT-VER-20230701-A" if tax_year == 2023 else "KIT-VER-20210101-A"

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=tax_year,
            rule_version="v1",
            primary_effective_date=effective_date,
            historical_version_id=historical_version_id,
            resident_status_assertion="resident",
            income_category_signature="employment",
        )
    )

    assert bound_rule.binding_id == expected_binding_id


@pytest.mark.parametrize(
    ("payload_name", "effective_date", "expected_reason"),
    [
        ("resident_2023", "2023-06-30", "unknown_rule_binding"),
        ("resident_2023", "2023-09-01", "unknown_rule_binding"),
        ("resident_2021", "2020-12-31", "unknown_rule_binding"),
        ("resident_2021", "2021-07-01", "unknown_rule_binding"),
    ],
)
def test_just_outside_window_dates_fail_deterministically(
    payload_name: str,
    effective_date: str,
    expected_reason: str,
) -> None:
    """Verify just-outside supported windows fail explicitly."""

    payload = (
        _resident_2023_payload() if payload_name == "resident_2023" else _resident_2021_payload()
    )
    version_context = cast(
        dict[str, object],
        cast(dict[str, object], payload["input_payload"])["version_context"],
    )
    version_context["primary_effective_date"] = effective_date

    with pytest.raises(RuleBindingError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == expected_reason


def test_resident_status_flip_from_non_resident_lane_fails_deterministically() -> None:
    """Verify flipping taxpayer status without resident relief shape is not normalized silently."""

    payload = _non_resident_2023_payload()
    taxpayer_context = cast(
        dict[str, object],
        cast(dict[str, object], payload["input_payload"])["taxpayer_context"],
    )
    taxpayer_context["resident_status_assertion"] = "resident"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_personal_relief_claim_shape"
    assert error.value.details() == {
        "reason": "unsupported_personal_relief_claim_shape",
        "path": "$.input_payload.claims.relief_claims",
    }


def test_resident_status_flip_from_resident_lane_fails_deterministically() -> None:
    """Verify flipping taxpayer status to non-resident fails with explicit relief mismatch."""

    payload = _resident_2023_payload()
    taxpayer_context = cast(
        dict[str, object],
        cast(dict[str, object], payload["input_payload"])["taxpayer_context"],
    )
    taxpayer_context["resident_status_assertion"] = "non_resident"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_relief_claims"
    assert error.value.details() == {
        "reason": "unsupported_relief_claims",
        "path": "$.input_payload.claims.relief_claims",
    }


def test_supported_lane_rejects_adversarial_deduction_claims() -> None:
    """Verify supported resident lane rejects positive deduction claims explicitly."""

    payload = _resident_2023_payload()
    claims = cast(dict[str, object], cast(dict[str, object], payload["input_payload"])["claims"])
    deduction_claims = cast(list[object], claims["deduction_claims"])
    deduction_claims.append(
        {
            "deduction_type": "mortgage_interest",
            "claim_reference_id": "deduction-001",
            "asserted_amount_kes": "1000.00",
        }
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_deduction_claims"


def test_supported_lane_rejects_adversarial_exemption_claims() -> None:
    """Verify supported non-resident lane rejects exemption claims explicitly."""

    payload = _non_resident_2023_payload()
    claims = cast(dict[str, object], cast(dict[str, object], payload["input_payload"])["claims"])
    exemption_claims = cast(list[object], claims["exemption_claims"])
    exemption_claims.append(
        {
            "exemption_type": "employment_exemption",
            "claim_reference_id": "exemption-001",
        }
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_exemption_claims"


def test_mixed_lane_rejects_unsupported_extra_income_section() -> None:
    """Verify mixed-income lane does not silently absorb unsupported extra sections."""

    payload = _mixed_2023_payload()
    income_sections = cast(
        dict[str, object],
        cast(dict[str, object], payload["input_payload"])["income_sections"],
    )
    income_sections["rental"] = {
        "rental_items": [
            {
                "gross_amount_kes": "1000.00",
                "event_date": "2023-07-20",
            }
        ]
    }

    with pytest.raises(RuleBindingError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unknown_rule_binding"


def test_mixed_lane_rejects_incorrect_qualifying_interest_withholding_rate() -> None:
    """Verify governed mixed lane enforces the exact qualifying-interest withholding amount."""

    payload = _mixed_2023_payload()
    investment_items = cast(
        list[dict[str, object]],
        _investment_section(payload)["investment_items"],
    )
    investment_items[0]["withholding_applied_kes"] = "18000.01"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_qualifying_interest_withholding"


def test_contradictory_version_context_fails_at_binding_boundary() -> None:
    """Verify contradictory historical version context is not normalized silently."""

    payload = _resident_2023_payload()
    version_context = cast(
        dict[str, object],
        cast(dict[str, object], payload["input_payload"])["version_context"],
    )
    version_context["historical_version_id"] = "KIT-VER-20210101-A"

    with pytest.raises(RuleBindingError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unknown_rule_binding"


def test_adversarial_failure_details_are_deterministic_across_repeated_runs() -> None:
    """Verify repeated malformed mixed-income requests fail with identical deterministic details."""

    payload = _mixed_2023_payload()
    investment_items = cast(
        list[dict[str, object]],
        _investment_section(payload)["investment_items"],
    )
    investment_items[0]["withholding_applied_kes"] = "17999.99"
    request = ComputationExecutionRequest.model_validate(payload)

    with pytest.raises(InputHashError) as first_error:
        execute_computation(request)
    with pytest.raises(InputHashError) as second_error:
        execute_computation(request)

    assert first_error.value.reason == second_error.value.reason
    assert first_error.value.message == second_error.value.message
    assert first_error.value.details() == second_error.value.details()


def _resident_2023_payload() -> dict[str, object]:
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
                }
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
                "source_record_ids": ["src-payroll-001"],
                "preparation_profile": "manual_structured_entry",
                "completeness_assertion": "complete",
                "evidence_reference_ids": [],
            },
        },
    }


def _non_resident_2023_payload() -> dict[str, object]:
    payload = _resident_2023_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    taxpayer_context = cast(dict[str, object], input_payload["taxpayer_context"])
    taxpayer_context["resident_status_assertion"] = "non_resident"
    claims = cast(dict[str, object], input_payload["claims"])
    claims["relief_claims"] = []
    employment_items = cast(
        list[dict[str, object]],
        _employment_section_from_input(input_payload)["employment_items"],
    )
    employment_items[0]["employer_reference_id"] = "employer-nr-001"
    traceability_context = cast(dict[str, object], input_payload["traceability_context"])
    traceability_context["source_record_ids"] = ["src-payroll-nr-001"]
    return payload


def _resident_2021_payload() -> dict[str, object]:
    payload = _resident_2023_payload()
    payload["tax_year"] = 2021
    input_payload = cast(dict[str, object], payload["input_payload"])
    version_context = cast(dict[str, object], input_payload["version_context"])
    version_context["primary_effective_date"] = "2021-01-01"
    version_context["historical_version_id"] = "KIT-VER-20210101-A"
    version_context["source_anchor_ids"] = ["ITA-2021-01-01-A"]
    taxpayer_context = cast(dict[str, object], input_payload["taxpayer_context"])
    taxpayer_context["residence_reference_period_start"] = "2021-01-01"
    taxpayer_context["residence_reference_period_end"] = "2021-12-31"
    employment_items = cast(
        list[dict[str, object]],
        _employment_section_from_input(input_payload)["employment_items"],
    )
    employment_items[0]["event_date"] = "2021-03-15"
    employment_items[0]["employer_reference_id"] = "employer-2021-001"
    claims = cast(dict[str, object], input_payload["claims"])
    relief_claims = cast(list[dict[str, object]], claims["relief_claims"])
    relief_claims[0]["claim_reference_id"] = "claim-personal-relief-2021-001"
    traceability_context = cast(dict[str, object], input_payload["traceability_context"])
    traceability_context["source_record_ids"] = ["src-payroll-2021-001"]
    return payload


def _mixed_2023_payload() -> dict[str, object]:
    payload = _resident_2023_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    input_payload["income_sections"] = {
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
    }
    traceability_context = cast(dict[str, object], input_payload["traceability_context"])
    traceability_context["source_record_ids"] = ["src-payroll-001", "src-interest-001"]
    return payload


def _investment_section(payload: dict[str, object]) -> dict[str, object]:
    input_payload = cast(dict[str, object], payload["input_payload"])
    income_sections = cast(dict[str, object], input_payload["income_sections"])
    return cast(dict[str, object], income_sections["investment"])


def _employment_section_from_input(input_payload: dict[str, object]) -> dict[str, object]:
    income_sections = cast(dict[str, object], input_payload["income_sections"])
    return cast(dict[str, object], income_sections["employment"])
