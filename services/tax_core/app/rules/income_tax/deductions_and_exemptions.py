"""Implement governed deductions and exemptions handling for supported employment lanes."""

from __future__ import annotations

from typing import NoReturn
from decimal import Decimal
from decimal import ROUND_HALF_UP
from dataclasses import dataclass

from shared.determinism.input_hash import InputHashError

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class DeductionsAndExemptionsResult:
    """Represent deterministic pre-tax deductions and exemptions outcome."""

    chargeable_income: Decimal
    deduction_domain_outcome: dict[str, object]
    deduction_impacts: list[dict[str, object]]
    exemption_impacts: list[dict[str, object]]


def apply_supported_resident_employment_deductions_and_exemptions(
    assessable_income: Decimal,
    deduction_claims: list[object],
    exemption_claims: list[object],
) -> DeductionsAndExemptionsResult:
    """Apply the first supported resident employment deductions/exemptions layer."""

    _validate_no_supported_claims(
        deduction_claims=deduction_claims,
        exemption_claims=exemption_claims,
        lane_name="Resident employment rule pack",
    )
    return _zero_adjustment_result(
        assessable_income=assessable_income,
        decision_refs=["ITC-POL-803", "ITC-POL-804"],
    )


def apply_supported_non_resident_employment_deductions_and_exemptions(
    assessable_income: Decimal,
    deduction_claims: list[object],
    exemption_claims: list[object],
) -> DeductionsAndExemptionsResult:
    """Apply the first supported non-resident employment deductions/exemptions layer."""

    _validate_no_supported_claims(
        deduction_claims=deduction_claims,
        exemption_claims=exemption_claims,
        lane_name="Non-resident employment rule pack",
    )
    return _zero_adjustment_result(
        assessable_income=assessable_income,
        decision_refs=["ITC-POL-803", "ITC-POL-804"],
    )


def _validate_no_supported_claims(
    deduction_claims: list[object],
    exemption_claims: list[object],
    lane_name: str,
) -> None:
    if deduction_claims:
        _raise_rule_input_error(
            reason="unsupported_deduction_claims",
            message=f"{lane_name} does not support deduction claims in the first lane.",
            path="$.input_payload.claims.deduction_claims",
        )
    if exemption_claims:
        _raise_rule_input_error(
            reason="unsupported_exemption_claims",
            message=f"{lane_name} does not support exemption claims in the first lane.",
            path="$.input_payload.claims.exemption_claims",
        )


def _zero_adjustment_result(
    assessable_income: Decimal,
    decision_refs: list[str],
) -> DeductionsAndExemptionsResult:
    chargeable_income = assessable_income.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return DeductionsAndExemptionsResult(
        chargeable_income=chargeable_income,
        deduction_domain_outcome={
            "status": "computed",
            "taxable_base_kes": _format_money(chargeable_income),
            "gross_tax_kes": None,
            "creditable_amount_kes": _format_money(ZERO),
            "final_tax_amount_kes": None,
            "decision_refs": decision_refs,
        },
        deduction_impacts=[],
        exemption_impacts=[],
    )


def _format_money(value: Decimal) -> str:
    return format(value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP), ".2f")


def _raise_rule_input_error(
    reason: str,
    message: str,
    path: str,
) -> NoReturn:
    raise InputHashError(reason=reason, message=message, path=path)
