"""Implement governed relief and credit handling for supported employment lanes."""

from __future__ import annotations

import re
from typing import NoReturn
from decimal import Decimal
from decimal import ROUND_HALF_UP
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic import ConfigDict

from shared.determinism.input_hash import InputHashError

MONEY_PATTERN = re.compile(r"^\d+\.\d{2}$")
TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
PERSONAL_RELIEF_ANNUAL = Decimal("28800.00")


class EmploymentReliefClaim(BaseModel):
    """Represent one supported employment-lane relief claim."""

    model_config = ConfigDict(extra="forbid")

    relief_type: str
    claim_reference_id: str
    asserted_amount_kes: str | None = None


@dataclass(frozen=True)
class ReliefsAndCreditsResult:
    """Represent deterministic post-base-tax relief application outcome."""

    total_reliefs: Decimal
    net_tax_due: Decimal
    relief_domain_outcome: dict[str, object]
    relief_impacts: list[dict[str, object]]


def apply_supported_resident_employment_reliefs(
    relief_claims: list[EmploymentReliefClaim],
    gross_tax: Decimal,
) -> ReliefsAndCreditsResult:
    """Apply the first supported resident employment relief layer."""

    personal_relief_claims = [
        claim for claim in relief_claims if claim.relief_type == "personal_relief"
    ]
    insurance_relief_claims = [
        claim for claim in relief_claims if claim.relief_type == "insurance_relief"
    ]
    unsupported_relief_claims = [
        claim
        for claim in relief_claims
        if claim.relief_type not in {"personal_relief", "insurance_relief"}
    ]

    if unsupported_relief_claims:
        _raise_rule_input_error(
            reason="unsupported_relief_claim_type",
            message="Resident employment rule pack does not support this relief type.",
            path="$.input_payload.claims.relief_claims",
        )

    if len(personal_relief_claims) != 1:
        _raise_rule_input_error(
            reason="unsupported_personal_relief_claim_shape",
            message="Resident employment rule pack requires exactly one personal_relief claim.",
            path="$.input_payload.claims.relief_claims",
        )

    personal_relief_claim = personal_relief_claims[0]
    if (
        personal_relief_claim.asserted_amount_kes is not None
        and _parse_money_amount(
            personal_relief_claim.asserted_amount_kes,
            path="$.input_payload.claims.relief_claims.personal_relief.asserted_amount_kes",
        )
        != PERSONAL_RELIEF_ANNUAL
    ):
        _raise_rule_input_error(
            reason="invalid_personal_relief_amount",
            message=(
                "Resident employment rule pack requires the annual "
                "personal relief amount of 28800.00."
            ),
            path="$.input_payload.claims.relief_claims.personal_relief.asserted_amount_kes",
        )

    for claim in insurance_relief_claims:
        if claim.asserted_amount_kes is None:
            _raise_rule_input_error(
                reason="unsupported_insurance_relief_claim",
                message=(
                    "Resident employment rule pack does not support "
                    "insurance relief claims in the first lane."
                ),
                path="$.input_payload.claims.relief_claims.insurance_relief",
            )
        insurance_amount = _parse_money_amount(
            claim.asserted_amount_kes,
            path="$.input_payload.claims.relief_claims.insurance_relief.asserted_amount_kes",
        )
        if insurance_amount != ZERO:
            _raise_rule_input_error(
                reason="unsupported_insurance_relief_claim",
                message=(
                    "Resident employment rule pack does not support "
                    "non-zero insurance relief claims in the first lane."
                ),
                path="$.input_payload.claims.relief_claims.insurance_relief.asserted_amount_kes",
            )

    total_reliefs = PERSONAL_RELIEF_ANNUAL
    net_tax_due = gross_tax - total_reliefs
    if net_tax_due < ZERO:
        net_tax_due = ZERO

    return ReliefsAndCreditsResult(
        total_reliefs=total_reliefs,
        net_tax_due=net_tax_due.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        relief_domain_outcome={
            "status": "computed",
            "taxable_base_kes": None,
            "gross_tax_kes": None,
            "creditable_amount_kes": _format_money(total_reliefs),
            "final_tax_amount_kes": _format_money(ZERO),
            "decision_refs": [
                "REM-20230701-003",
                "REM-20230701-011",
            ],
        },
        relief_impacts=[
            {
                "impact_type": "personal_relief",
                "claim_reference_id": personal_relief_claim.claim_reference_id,
                "status": "applied",
                "impact_amount_kes": _format_money(total_reliefs),
            }
        ],
    )


def apply_supported_non_resident_employment_reliefs(
    relief_claims: list[EmploymentReliefClaim],
    gross_tax: Decimal,
) -> ReliefsAndCreditsResult:
    """Apply the first supported non-resident employment relief layer."""

    if relief_claims:
        _raise_rule_input_error(
            reason="unsupported_relief_claims",
            message=(
                "Non-resident employment rule pack does not support "
                "relief claims in the first lane."
            ),
            path="$.input_payload.claims.relief_claims",
        )

    return ReliefsAndCreditsResult(
        total_reliefs=ZERO,
        net_tax_due=gross_tax.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        relief_domain_outcome={
            "status": "computed",
            "taxable_base_kes": None,
            "gross_tax_kes": None,
            "creditable_amount_kes": _format_money(ZERO),
            "final_tax_amount_kes": _format_money(ZERO),
            "decision_refs": [
                "NREM-20230701-005",
                "NREM-20230701-006",
            ],
        },
        relief_impacts=[],
    )


def _parse_money_amount(value: str, path: str) -> Decimal:
    if not MONEY_PATTERN.fullmatch(value):
        _raise_rule_input_error(
            reason="invalid_money_amount",
            message="Money amounts must be strings with two decimal places.",
            path=path,
        )
    return Decimal(value)


def _format_money(value: Decimal) -> str:
    return format(value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP), ".2f")


def _raise_rule_input_error(
    reason: str,
    message: str,
    path: str,
) -> NoReturn:
    raise InputHashError(reason=reason, message=message, path=path)
