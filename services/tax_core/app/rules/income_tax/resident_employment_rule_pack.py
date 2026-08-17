"""Implement the first governed resident employment income rule pack."""

from __future__ import annotations

import re
from typing import NoReturn
from decimal import Decimal
from decimal import ROUND_HALF_UP
from datetime import date

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError as PydanticValidationError

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import PreparedExecutionInput
from services.tax_core.app.rules.income_tax.reliefs_and_credits import EmploymentReliefClaim
from services.tax_core.app.rules.income_tax.reliefs_and_credits import (
    apply_supported_resident_employment_reliefs,
)
from services.tax_core.app.rules.income_tax.deductions_and_exemptions import (
    apply_supported_resident_employment_deductions_and_exemptions,
)

MONEY_PATTERN = re.compile(r"^\d+\.\d{2}$")
TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
SUPPORTED_EFFECTIVE_START = date(2023, 7, 1)
SUPPORTED_EFFECTIVE_END = date(2023, 8, 31)
SUPPORTED_HISTORICAL_VERSION_ID = "KIT-VER-20230701-A"
SUPPORTED_BINDING_ID = "income_tax_resident_employment_v1_2023_07_01"
SOURCE_ANCHOR_IDS = ["ITA-2023-07-01-A", "AMEND-2023-FA", "KRA-PAYE"]
APPLIED_POLICY_IDS = [
    "ITC-POL-201",
    "ITC-POL-204",
    "ITC-POL-803",
    "ITC-POL-804",
    "REM-20230701-002",
    "REM-20230701-003",
    "REM-20230701-005",
    "REM-20230701-010",
    "REM-20230701-011",
    "REM-20230701-012",
]
BAND_SCHEDULE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("288000.00"), Decimal("0.10")),
    (Decimal("100000.00"), Decimal("0.25")),
    (Decimal("5612000.00"), Decimal("0.30")),
    (Decimal("3600000.00"), Decimal("0.325")),
)
TOP_RATE = Decimal("0.35")


class SupportedResidentEmploymentInput(BaseModel):
    """Represent the first supported resident-employment input lane."""

    model_config = ConfigDict(extra="forbid")

    version_context: SupportedVersionContext
    taxpayer_context: SupportedTaxpayerContext
    income_sections: SupportedIncomeSections
    claims: SupportedClaims
    payment_pathways: SupportedPaymentPathways
    traceability_context: SupportedTraceabilityContext


class SupportedVersionContext(BaseModel):
    """Represent deterministic version-selection context for supported lane."""

    model_config = ConfigDict(extra="forbid")

    primary_effective_date: date
    version_selection_basis: str
    historical_version_id: str | None = None
    source_anchor_ids: list[str] = []


class SupportedTaxpayerContext(BaseModel):
    """Represent supported taxpayer classification facts."""

    model_config = ConfigDict(extra="forbid")

    taxpayer_kind: str
    resident_status_assertion: str
    residence_reference_period_start: date | None = None
    residence_reference_period_end: date | None = None


class SupportedIncomeSections(BaseModel):
    """Represent supported income sections for first rule lane."""

    model_config = ConfigDict(extra="forbid")

    employment: SupportedEmploymentSection
    business: object | None = None
    investment: object | None = None
    rental: object | None = None
    classification_pending_items: list[object] = []


class SupportedEmploymentSection(BaseModel):
    """Represent supported employment-only section."""

    model_config = ConfigDict(extra="forbid")

    employment_items: list[SupportedEmploymentItem]


class SupportedEmploymentItem(BaseModel):
    """Represent one supported employment income item."""

    model_config = ConfigDict(extra="forbid")

    income_subtype: str
    amount_kes: str
    event_date: date
    employer_reference_id: str | None = None
    paye_withheld_kes: str | None = None
    prescribed_rate_notice_id: str | None = None


class SupportedClaims(BaseModel):
    """Represent supported relief and claim structures."""

    model_config = ConfigDict(extra="forbid")

    relief_claims: list[EmploymentReliefClaim]
    deduction_claims: list[object]
    exemption_claims: list[object]


class SupportedPaymentPathways(BaseModel):
    """Represent supported payment pathways for first lane."""

    model_config = ConfigDict(extra="forbid")

    withholding_events: list[object]
    installment_tax_events: list[object]
    advance_tax_events: list[object]


class SupportedTraceabilityContext(BaseModel):
    """Represent traceability context required for governed execution."""

    model_config = ConfigDict(extra="forbid")

    source_record_ids: list[str]
    preparation_profile: str
    completeness_assertion: str
    evidence_reference_ids: list[str] = []


def execute_resident_employment_rule_pack(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
) -> dict[str, object]:
    """Compute governed resident employment tax outcome for first supported lane."""

    if bound_rule.binding_id != SUPPORTED_BINDING_ID:
        _raise_rule_input_error(
            reason="invalid_resident_employment_binding",
            message="Resident employment rule pack received an unexpected binding.",
            path="$.binding_id",
        )

    payload = _parse_supported_payload(prepared_input)
    annual_assessable_income = _compute_annual_taxable_income(
        payload.income_sections.employment.employment_items
    )
    deductions_result = apply_supported_resident_employment_deductions_and_exemptions(
        assessable_income=annual_assessable_income,
        deduction_claims=payload.claims.deduction_claims,
        exemption_claims=payload.claims.exemption_claims,
    )
    gross_tax = _compute_gross_tax(deductions_result.chargeable_income)
    reliefs_result = apply_supported_resident_employment_reliefs(
        relief_claims=payload.claims.relief_claims,
        gross_tax=gross_tax,
    )

    return {
        "version_identity": {
            "historical_version_id": SUPPORTED_HISTORICAL_VERSION_ID,
            "tax_year": prepared_input.tax_year,
            "rule_version": prepared_input.rule_version,
            "effective_start": SUPPORTED_EFFECTIVE_START.isoformat(),
            "effective_end": SUPPORTED_EFFECTIVE_END.isoformat(),
            "version_selection_basis": payload.version_context.version_selection_basis,
            "source_anchor_ids": SOURCE_ANCHOR_IDS,
        },
        "taxpayer_outcome": {
            "taxpayer_kind": "individual",
            "resident_status": "resident",
            "classification_outcome": "fully_classified",
        },
        "domain_outcomes": {
            "employment": {
                "status": "computed",
                "taxable_base_kes": _format_money(deductions_result.chargeable_income),
                "gross_tax_kes": _format_money(gross_tax),
                "creditable_amount_kes": ZERO_STR,
                "final_tax_amount_kes": ZERO_STR,
                "decision_refs": [
                    "REM-20230701-005",
                    "REM-20230701-010",
                    "REM-20230701-012",
                ],
            },
            "business": _not_applicable_domain("ITC-POL-301"),
            "investment": _not_applicable_domain("ITC-POL-401"),
            "rental": _not_applicable_domain("ITC-POL-501"),
            "withholding": _not_applicable_domain("ITC-POL-601"),
            "installment_tax": _not_applicable_domain("ITC-POL-701"),
            "advance_tax": _not_applicable_domain("ITC-POL-702"),
            "reliefs": {
                **reliefs_result.relief_domain_outcome,
            },
            "deductions_and_exemptions": {
                **deductions_result.deduction_domain_outcome,
            },
            "prescribed_rate_resolution": _not_applicable_domain("REM-20230701-008"),
            "adjacent_regime_interactions": _not_applicable_domain("ITC-POL-902"),
        },
        "liability_summary": {
            "assessable_income_kes": _format_money(annual_assessable_income),
            "chargeable_income_kes": _format_money(deductions_result.chargeable_income),
            "gross_tax_kes": _format_money(gross_tax),
            "total_reliefs_kes": _format_money(reliefs_result.total_reliefs),
            "creditable_withholding_kes": ZERO_STR,
            "installment_tax_credit_kes": ZERO_STR,
            "advance_tax_credit_kes": ZERO_STR,
            "net_income_tax_due_kes": _format_money(reliefs_result.net_tax_due),
            "refund_due_kes": ZERO_STR,
            "final_tax_excluded_income_kes": ZERO_STR,
        },
        "treatment_decisions": {
            "withholding_treatments": [],
            "adjacent_regime_flags": [],
        },
        "impact_summary": {
            "relief_impacts": reliefs_result.relief_impacts,
            "deduction_impacts": deductions_result.deduction_impacts,
            "exemption_impacts": deductions_result.exemption_impacts,
        },
        "unsupported_or_unresolved": [],
        "traceability": {
            "input_hash": prepared_input.input_hash,
            "applied_policy_ids": APPLIED_POLICY_IDS,
            "source_anchor_ids": SOURCE_ANCHOR_IDS,
            "validation_focus_domains": [
                "ITD-CORE-EMPLOYMENT",
                "ITD-CORE-DEDUCTIONS",
                "ITD-CORE-RELIEFS",
            ],
            "computation_status": "complete",
            "replay_safe": True,
        },
    }


def _parse_supported_payload(
    prepared_input: PreparedExecutionInput,
) -> SupportedResidentEmploymentInput:
    try:
        payload = SupportedResidentEmploymentInput.model_validate(
            prepared_input.canonical_input_payload
        )
    except PydanticValidationError as error:
        _raise_rule_input_error(
            reason="unsupported_resident_employment_request_shape",
            message="Resident employment request does not match the supported governed shape.",
            path="$.input_payload",
            details=str(error),
        )

    _validate_supported_version_context(payload.version_context, prepared_input.tax_year)
    _validate_supported_taxpayer_context(payload.taxpayer_context, prepared_input.tax_year)
    _validate_supported_income_sections(payload.income_sections)
    _validate_supported_payment_pathways(payload.payment_pathways)
    _validate_supported_traceability(payload.traceability_context)
    return payload


def _validate_supported_version_context(
    version_context: SupportedVersionContext,
    tax_year: int,
) -> None:
    if tax_year != 2023:
        _raise_rule_input_error(
            reason="unsupported_tax_year",
            message="Resident employment rule pack supports tax year 2023 only.",
            path="$.tax_year",
        )
    if version_context.version_selection_basis != "specific_event_date":
        _raise_rule_input_error(
            reason="unsupported_version_selection_basis",
            message=(
                "Resident employment rule pack requires "
                "version_selection_basis=specific_event_date."
            ),
            path="$.input_payload.version_context.version_selection_basis",
        )
    if not (
        SUPPORTED_EFFECTIVE_START
        <= version_context.primary_effective_date
        <= SUPPORTED_EFFECTIVE_END
    ):
        _raise_rule_input_error(
            reason="unsupported_effective_date_window",
            message=(
                "Resident employment rule pack supports only the 2023-07-01 to 2023-08-31 window."
            ),
            path="$.input_payload.version_context.primary_effective_date",
        )
    if (
        version_context.historical_version_id is not None
        and version_context.historical_version_id != SUPPORTED_HISTORICAL_VERSION_ID
    ):
        _raise_rule_input_error(
            reason="unsupported_historical_version_id",
            message=(
                "historical_version_id does not match the supported resident employment window."
            ),
            path="$.input_payload.version_context.historical_version_id",
        )


def _validate_supported_taxpayer_context(
    taxpayer_context: SupportedTaxpayerContext,
    tax_year: int,
) -> None:
    if taxpayer_context.taxpayer_kind != "individual":
        _raise_rule_input_error(
            reason="unsupported_taxpayer_kind",
            message="Resident employment rule pack supports individual taxpayers only.",
            path="$.input_payload.taxpayer_context.taxpayer_kind",
        )
    if taxpayer_context.resident_status_assertion != "resident":
        _raise_rule_input_error(
            reason="unsupported_resident_status",
            message="Resident employment rule pack supports resident taxpayers only.",
            path="$.input_payload.taxpayer_context.resident_status_assertion",
        )
    if taxpayer_context.residence_reference_period_start not in (None, date(tax_year, 1, 1)):
        _raise_rule_input_error(
            reason="unsupported_residency_scope",
            message="Resident employment rule pack supports full-year resident scope only.",
            path="$.input_payload.taxpayer_context.residence_reference_period_start",
        )
    if taxpayer_context.residence_reference_period_end not in (None, date(tax_year, 12, 31)):
        _raise_rule_input_error(
            reason="unsupported_residency_scope",
            message="Resident employment rule pack supports full-year resident scope only.",
            path="$.input_payload.taxpayer_context.residence_reference_period_end",
        )


def _validate_supported_income_sections(income_sections: SupportedIncomeSections) -> None:
    if income_sections.business is not None:
        _raise_rule_input_error(
            reason="unsupported_income_domain",
            message="Resident employment rule pack does not support business income.",
            path="$.input_payload.income_sections.business",
        )
    if income_sections.investment is not None:
        _raise_rule_input_error(
            reason="unsupported_income_domain",
            message="Resident employment rule pack does not support investment income.",
            path="$.input_payload.income_sections.investment",
        )
    if income_sections.rental is not None:
        _raise_rule_input_error(
            reason="unsupported_income_domain",
            message="Resident employment rule pack does not support rental income.",
            path="$.input_payload.income_sections.rental",
        )
    if income_sections.classification_pending_items:
        _raise_rule_input_error(
            reason="pending_income_classification",
            message="Resident employment rule pack does not support pending classification items.",
            path="$.input_payload.income_sections.classification_pending_items",
        )
    if not income_sections.employment.employment_items:
        _raise_rule_input_error(
            reason="missing_employment_items",
            message="Resident employment rule pack requires employment items.",
            path="$.input_payload.income_sections.employment.employment_items",
        )
    for index, item in enumerate(income_sections.employment.employment_items):
        if item.income_subtype != "cash_emolument":
            _raise_rule_input_error(
                reason="unsupported_employment_income_subtype",
                message="Resident employment rule pack supports cash_emolument items only.",
                path=f"$.input_payload.income_sections.employment.employment_items[{index}].income_subtype",
            )
        if item.paye_withheld_kes is not None:
            _raise_rule_input_error(
                reason="unsupported_payroll_withholding_input",
                message=(
                    "Resident employment rule pack does not support payroll "
                    "withholding normalization input in the first lane."
                ),
                path=f"$.input_payload.income_sections.employment.employment_items[{index}].paye_withheld_kes",
            )
        if item.prescribed_rate_notice_id is not None:
            _raise_rule_input_error(
                reason="unsupported_prescribed_rate_dependency",
                message=(
                    "Resident employment rule pack does not support "
                    "prescribed-rate-dependent items."
                ),
                path=f"$.input_payload.income_sections.employment.employment_items[{index}].prescribed_rate_notice_id",
            )
        amount = _parse_money_amount(
            item.amount_kes,
            path=f"$.input_payload.income_sections.employment.employment_items[{index}].amount_kes",
        )
        if amount < ZERO:
            _raise_rule_input_error(
                reason="negative_employment_amount",
                message="Resident employment amounts must be non-negative.",
                path=f"$.input_payload.income_sections.employment.employment_items[{index}].amount_kes",
            )


def _validate_supported_payment_pathways(
    payment_pathways: SupportedPaymentPathways,
) -> None:
    if payment_pathways.withholding_events:
        _raise_rule_input_error(
            reason="unsupported_withholding_events",
            message=(
                "Resident employment rule pack does not support "
                "withholding events in the first lane."
            ),
            path="$.input_payload.payment_pathways.withholding_events",
        )
    if payment_pathways.installment_tax_events:
        _raise_rule_input_error(
            reason="unsupported_installment_tax_events",
            message=(
                "Resident employment rule pack does not support "
                "installment-tax events in the first lane."
            ),
            path="$.input_payload.payment_pathways.installment_tax_events",
        )
    if payment_pathways.advance_tax_events:
        _raise_rule_input_error(
            reason="unsupported_advance_tax_events",
            message=(
                "Resident employment rule pack does not support "
                "advance-tax events in the first lane."
            ),
            path="$.input_payload.payment_pathways.advance_tax_events",
        )


def _validate_supported_traceability(
    traceability_context: SupportedTraceabilityContext,
) -> None:
    if not traceability_context.source_record_ids:
        _raise_rule_input_error(
            reason="missing_traceability_sources",
            message="Resident employment rule pack requires source_record_ids for traceability.",
            path="$.input_payload.traceability_context.source_record_ids",
        )
    if traceability_context.completeness_assertion != "complete":
        _raise_rule_input_error(
            reason="unsupported_traceability_completeness",
            message="Resident employment rule pack requires completeness_assertion=complete.",
            path="$.input_payload.traceability_context.completeness_assertion",
        )


def _compute_annual_taxable_income(
    employment_items: list[SupportedEmploymentItem],
) -> Decimal:
    total = ZERO
    for index, item in enumerate(employment_items):
        item_amount = _parse_money_amount(
            item.amount_kes,
            path=f"$.input_payload.income_sections.employment.employment_items[{index}].amount_kes",
        )
        total += item_amount
    return total.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _compute_gross_tax(annual_taxable_income: Decimal) -> Decimal:
    remaining = annual_taxable_income
    gross_tax = ZERO
    for band_width, band_rate in BAND_SCHEDULE:
        if remaining <= ZERO:
            break
        taxable_in_band = band_width if remaining > band_width else remaining
        gross_tax += taxable_in_band * band_rate
        remaining -= taxable_in_band
    if remaining > ZERO:
        gross_tax += remaining * TOP_RATE
    return gross_tax.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _not_applicable_domain(decision_ref: str) -> dict[str, object]:
    return {
        "status": "not_applicable",
        "taxable_base_kes": None,
        "gross_tax_kes": None,
        "creditable_amount_kes": None,
        "final_tax_amount_kes": None,
        "decision_refs": [decision_ref],
    }


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
    details: str | None = None,
) -> NoReturn:
    if details is None:
        raise InputHashError(reason=reason, message=message, path=path)
    raise InputHashError(reason=reason, message=f"{message} {details}", path=path)


ZERO_STR = _format_money(ZERO)
