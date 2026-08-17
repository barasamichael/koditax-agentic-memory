"""Implement the first governed SHA/SHIF health-contribution rule pack."""

from __future__ import annotations

from re import compile
from typing import NoReturn
from decimal import Decimal
from decimal import ROUND_HALF_UP
from datetime import date
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError as PydanticValidationError

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import PreparedExecutionInput
from services.tax_core.app.rules.health_contribution.mixed_contexts import (
    reject_governed_mixed_context_request,
)
from services.tax_core.app.rules.health_contribution.exemptions_and_special_cases import (
    UNRESOLVED_SPECIAL_CASE_POLICY_ID,
)
from services.tax_core.app.rules.health_contribution.exemptions_and_special_cases import (
    reject_unresolved_special_case_assertions,
)

MONEY_PATTERN = compile(r"^\d+\.\d{2}$")
TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
ZERO_STR = "0.00"
SALARIED_RATE = Decimal("0.0275")
SALARIED_FLOOR = Decimal("300.00")
NON_SALARIED_RATE = Decimal("0.0275")
NON_SALARIED_ANNUAL_FLOOR = Decimal("3600.00")
SUPPORTED_BINDINGS = {
    "health_contribution_sha_shif_v1_2024_10_01",
    "health_contribution_sha_shif_v1_2025_02_28_pit",
}
BASE_POLICY_IDS = [
    "HCP-POL-001",
    "HCP-POL-002",
    "HCP-POL-003",
    "HCP-POL-201",
    "HCP-POL-202",
    "HCP-POL-205",
]
VALIDATION_FOCUS_DOMAINS_BY_PATH = {
    "sha_shif_salaried": [
        "HCD-CORE-CONTRIBUTOR-CLASSIFICATION",
        "HCD-CORE-SHI-SALARIED",
        "HCD-XCUT-VERSION-SELECTION",
        "HCD-XCUT-VALIDATION-EVIDENCE",
    ],
    "sha_shif_non_salaried": [
        "HCD-CORE-CONTRIBUTOR-CLASSIFICATION",
        "HCD-CORE-SHI-NONSALARIED",
        "HCD-XCUT-VERSION-SELECTION",
        "HCD-XCUT-VALIDATION-EVIDENCE",
    ],
}


@dataclass(frozen=True)
class _SupportedWindow:
    binding_id: str
    historical_version_id: str
    effective_start: date
    effective_end: date | None
    governing_change_id: str
    source_anchor_ids: tuple[str, ...]
    salaried_rule_id: str
    non_salaried_rule_id: str
    non_salaried_gate_rule_id: str
    consolidated_policy_id: str


WINDOWS_BY_BINDING_ID: dict[str, _SupportedWindow] = {
    "health_contribution_sha_shif_v1_2024_10_01": _SupportedWindow(
        binding_id="health_contribution_sha_shif_v1_2024_10_01",
        historical_version_id="HCH-VER-20241001-A",
        effective_start=date(2024, 10, 1),
        effective_end=date(2025, 2, 27),
        governing_change_id="HC-CHG-2024-10-01-A",
        source_anchor_ids=("HC-SHI-REG-2024-09-20",),
        salaried_rule_id="HC-SHI-NPOL-2024-001",
        non_salaried_rule_id="HC-SHI-NPOL-2024-002",
        non_salaried_gate_rule_id="HC-SHI-NPOL-2024-003",
        consolidated_policy_id="HCP-POL-303",
    ),
    "health_contribution_sha_shif_v1_2025_02_28_pit": _SupportedWindow(
        binding_id="health_contribution_sha_shif_v1_2025_02_28_pit",
        historical_version_id="HCH-VER-20250228-PIT",
        effective_start=date(2025, 2, 28),
        effective_end=None,
        governing_change_id="HC-CHG-2025-02-28-B",
        source_anchor_ids=("HC-SHI-REG-2025-02-28",),
        salaried_rule_id="HC-SHI-NPOL-2025-001",
        non_salaried_rule_id="HC-SHI-NPOL-2025-002",
        non_salaried_gate_rule_id="HC-SHI-NPOL-2025-003",
        consolidated_policy_id="HCP-POL-303",
    ),
}


class SupportedShaShifExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_context: SupportedVersionContext
    contributor_context: SupportedContributorContext
    nhif_legacy_inputs: SupportedNhifLegacyInputs
    sha_shif_salaried_inputs: SupportedShaShifSalariedInputs
    sha_shif_non_salaried_inputs: SupportedShaShifNonSalariedInputs
    special_case_assertions: SupportedSpecialCaseAssertions
    mixed_context_inputs: SupportedMixedContextInputs
    operational_context: SupportedOperationalContext
    traceability_context: SupportedTraceabilityContext


class SupportedVersionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_effective_date: date
    version_selection_basis: str
    contribution_period_start: date | None = None
    contribution_period_end: date | None = None
    historical_version_id: str | None = None
    governing_change_ids: list[str] = []
    source_anchor_ids: list[str] = []


class SupportedContributorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contributor_kind: str
    asserted_domain_path: str
    contribution_subject_reference_id: str | None = None
    employer_reference_id: str | None = None
    household_reference_id: str | None = None
    payroll_reference_id: str | None = None


class SupportedNhifLegacyInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    earning_items: list[object]
    member_class_assertions: list[object]
    deduction_reference_ids: list[str]


class SupportedShaShifSalariedInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payroll_items: list[SupportedShaShifSalariedPayrollItem]
    employer_assertions: list[SupportedEmployerAssertion]
    remittance_reference_ids: list[str]


class SupportedShaShifSalariedPayrollItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    income_basis_type: str
    amount_kes: str
    event_date: date
    reference_id: str | None = None


class SupportedEmployerAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_type: str
    assertion_status: str
    source_reference_id: str | None = None


class SupportedShaShifNonSalariedInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_income_items: list[SupportedShaShifNonSalariedIncomeItem]
    means_testing_assertions: list[SupportedMeansTestingAssertion]
    household_member_reference_ids: list[str]


class SupportedShaShifNonSalariedIncomeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    income_basis_type: str
    amount_kes: str
    event_date: date
    reference_id: str | None = None


class SupportedMeansTestingAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_type: str
    assertion_status: str
    source_reference_id: str | None = None


class SupportedSpecialCaseAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_items: list[object]


class SupportedMixedContextInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_items: list[object]


class SupportedOperationalContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_flags: list[str]
    registration_status: str
    remittance_channel: str
    reference_ids: list[str]


class SupportedTraceabilityContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_ids: list[str]
    preparation_profile: str
    completeness_assertion: str
    evidence_reference_ids: list[str] = []


def execute_sha_shif_rule_pack(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
) -> dict[str, object]:
    """Compute governed SHA/SHIF contribution outcomes for supported windows only."""

    window = _resolve_window(bound_rule)
    payload = _parse_supported_payload(prepared_input)
    _validate_supported_payload(payload=payload, prepared_input=prepared_input, window=window)

    resolved_domain_path = payload.contributor_context.asserted_domain_path
    if resolved_domain_path == "sha_shif_salaried":
        return _build_salaried_result(
            payload=payload,
            prepared_input=prepared_input,
            window=window,
        )
    if resolved_domain_path == "sha_shif_non_salaried":
        return _build_non_salaried_result(
            payload=payload,
            prepared_input=prepared_input,
            window=window,
        )

    _raise_rule_input_error(
        reason="unsupported_asserted_domain_path",
        message="SHA/SHIF rule pack supports salaried or non-salaried governed paths only.",
        path="$.input_payload.contributor_context.asserted_domain_path",
    )


def _resolve_window(bound_rule: BoundRule) -> _SupportedWindow:
    if bound_rule.binding_id not in SUPPORTED_BINDINGS:
        _raise_rule_input_error(
            reason="invalid_sha_shif_binding",
            message="SHA/SHIF rule pack received an unexpected binding.",
            path="$.binding_id",
        )
    return WINDOWS_BY_BINDING_ID[bound_rule.binding_id]


def _parse_supported_payload(
    prepared_input: PreparedExecutionInput,
) -> SupportedShaShifExecutionInput:
    try:
        return SupportedShaShifExecutionInput.model_validate(prepared_input.canonical_input_payload)
    except PydanticValidationError as error:
        _raise_rule_input_error(
            reason="unsupported_sha_shif_request_shape",
            message="SHA/SHIF request does not match the supported governed shape.",
            path="$.input_payload",
            details=str(error),
        )


def _validate_supported_payload(
    *,
    payload: SupportedShaShifExecutionInput,
    prepared_input: PreparedExecutionInput,
    window: _SupportedWindow,
) -> None:
    if prepared_input.tax_type != "health_contribution":
        _raise_rule_input_error(
            reason="unsupported_tax_type",
            message="SHA/SHIF rule pack supports health_contribution tax_type only.",
            path="$.tax_type",
        )
    if prepared_input.regime_type != "health_contribution":
        _raise_rule_input_error(
            reason="unsupported_regime_type",
            message="SHA/SHIF rule pack supports health_contribution regime_type only.",
            path="$.regime_type",
        )
    if prepared_input.regime_identifier != "sha_shif":
        _raise_rule_input_error(
            reason="unsupported_regime_identifier",
            message="SHA/SHIF rule pack supports regime_identifier=sha_shif only.",
            path="$.regime_identifier",
        )
    if prepared_input.tax_year != payload.version_context.primary_effective_date.year:
        _raise_rule_input_error(
            reason="tax_year_primary_effective_date_mismatch",
            message=(
                "tax_year must match the year of primary_effective_date for SHA/SHIF execution."
            ),
            path="$.tax_year",
        )
    if payload.version_context.version_selection_basis not in {
        "payroll_period_end",
        "payment_due_date",
        "specific_event_date",
        "household_income_reference_date",
    }:
        _raise_rule_input_error(
            reason="unsupported_version_selection_basis",
            message="SHA/SHIF rule pack received an unsupported version_selection_basis.",
            path="$.input_payload.version_context.version_selection_basis",
        )
    if payload.version_context.primary_effective_date < window.effective_start:
        _raise_rule_input_error(
            reason="unsupported_effective_date_window",
            message="primary_effective_date is outside the supported SHA/SHIF window.",
            path="$.input_payload.version_context.primary_effective_date",
        )
    if (
        window.effective_end is not None
        and payload.version_context.primary_effective_date > window.effective_end
    ):
        _raise_rule_input_error(
            reason="unsupported_effective_date_window",
            message="primary_effective_date is outside the supported SHA/SHIF window.",
            path="$.input_payload.version_context.primary_effective_date",
        )
    if payload.version_context.historical_version_id not in (None, window.historical_version_id):
        _raise_rule_input_error(
            reason="unsupported_historical_version_id",
            message="historical_version_id does not match the bound SHA/SHIF window.",
            path="$.input_payload.version_context.historical_version_id",
        )
    if payload.version_context.governing_change_ids not in ([], [window.governing_change_id]):
        _raise_rule_input_error(
            reason="unsupported_governing_change_ids",
            message="governing_change_ids must be empty or match the bound SHA/SHIF change anchor.",
            path="$.input_payload.version_context.governing_change_ids",
        )
    if payload.version_context.source_anchor_ids not in ([], list(window.source_anchor_ids)):
        _raise_rule_input_error(
            reason="unsupported_source_anchor_ids",
            message="source_anchor_ids must be empty or match the bound SHA/SHIF source anchors.",
            path="$.input_payload.version_context.source_anchor_ids",
        )
    reject_governed_mixed_context_request(prepared_input)
    reject_unresolved_special_case_assertions(
        payload.special_case_assertions.assertion_items,
        path="$.input_payload.special_case_assertions.assertion_items",
    )
    if (
        payload.nhif_legacy_inputs.earning_items
        or payload.nhif_legacy_inputs.member_class_assertions
        or payload.nhif_legacy_inputs.deduction_reference_ids
    ):
        _raise_rule_input_error(
            reason="unsupported_nhif_legacy_inputs",
            message="NHIF legacy inputs are outside the SHA/SHIF runtime lane.",
            path="$.input_payload.nhif_legacy_inputs",
        )
    if not payload.traceability_context.source_record_ids:
        _raise_rule_input_error(
            reason="missing_traceability_source_records",
            message="At least one source_record_id is required for SHA/SHIF execution.",
            path="$.input_payload.traceability_context.source_record_ids",
        )
    if payload.operational_context.registration_status != "active":
        _raise_rule_input_error(
            reason="unsupported_registration_status",
            message="SHA/SHIF runtime support requires registration_status=active.",
            path="$.input_payload.operational_context.registration_status",
        )
    if "payment_and_access_live" not in payload.operational_context.workflow_flags:
        _raise_rule_input_error(
            reason="unsupported_operational_context",
            message="SHA/SHIF runtime support requires payment_and_access_live workflow context.",
            path="$.input_payload.operational_context.workflow_flags",
        )

    resolved_domain_path = payload.contributor_context.asserted_domain_path
    if resolved_domain_path == "sha_shif_salaried":
        _validate_salaried_lane(payload=payload, window=window)
        return
    if resolved_domain_path == "sha_shif_non_salaried":
        _validate_non_salaried_lane(payload=payload, window=window)
        return

    _raise_rule_input_error(
        reason="unsupported_asserted_domain_path",
        message="SHA/SHIF rule pack supports asserted_domain_path for one governed lane only.",
        path="$.input_payload.contributor_context.asserted_domain_path",
    )


def _validate_salaried_lane(
    *,
    payload: SupportedShaShifExecutionInput,
    window: _SupportedWindow,
) -> None:
    if payload.contributor_context.contributor_kind != "employee":
        _raise_rule_input_error(
            reason="unsupported_contributor_kind",
            message="SHA/SHIF salaried requests require contributor_kind=employee.",
            path="$.input_payload.contributor_context.contributor_kind",
        )
    if payload.contributor_context.employer_reference_id is None:
        _raise_rule_input_error(
            reason="missing_employer_reference",
            message="SHA/SHIF salaried requests require employer_reference_id.",
            path="$.input_payload.contributor_context.employer_reference_id",
        )
    if payload.contributor_context.payroll_reference_id is None:
        _raise_rule_input_error(
            reason="missing_payroll_reference",
            message="SHA/SHIF salaried requests require payroll_reference_id.",
            path="$.input_payload.contributor_context.payroll_reference_id",
        )
    if len(payload.sha_shif_salaried_inputs.payroll_items) != 1:
        _raise_rule_input_error(
            reason="unsupported_sha_shif_salaried_item_count",
            message="SHA/SHIF salaried runtime supports exactly one payroll item.",
            path="$.input_payload.sha_shif_salaried_inputs.payroll_items",
        )
    if not payload.sha_shif_salaried_inputs.remittance_reference_ids:
        _raise_rule_input_error(
            reason="missing_remittance_reference_ids",
            message="SHA/SHIF salaried requests require remittance_reference_ids.",
            path="$.input_payload.sha_shif_salaried_inputs.remittance_reference_ids",
        )
    if (
        payload.sha_shif_non_salaried_inputs.household_income_items
        or payload.sha_shif_non_salaried_inputs.means_testing_assertions
        or payload.sha_shif_non_salaried_inputs.household_member_reference_ids
    ):
        _raise_rule_input_error(
            reason="unsupported_sha_shif_non_salaried_inputs",
            message="Non-salaried inputs are outside the salaried SHA/SHIF lane.",
            path="$.input_payload.sha_shif_non_salaried_inputs",
        )
    if payload.operational_context.remittance_channel not in {
        "employer_payroll_remittance",
        "sha_portal",
    }:
        _raise_rule_input_error(
            reason="unsupported_remittance_channel",
            message="SHA/SHIF salaried requests require an employer remittance channel.",
            path="$.input_payload.operational_context.remittance_channel",
        )
    if "employer_remittance_workflow_present" not in payload.operational_context.workflow_flags:
        _raise_rule_input_error(
            reason="unsupported_operational_context",
            message="SHA/SHIF salaried requests require employer remittance workflow context.",
            path="$.input_payload.operational_context.workflow_flags",
        )

    payroll_item = payload.sha_shif_salaried_inputs.payroll_items[0]
    if payroll_item.income_basis_type != "gross_salary_basis":
        _raise_rule_input_error(
            reason="unsupported_sha_shif_salaried_basis_type",
            message="SHA/SHIF salaried runtime requires income_basis_type=gross_salary_basis.",
            path="$.input_payload.sha_shif_salaried_inputs.payroll_items[0].income_basis_type",
        )
    if not _date_within_window(payroll_item.event_date, window):
        _raise_rule_input_error(
            reason="unsupported_sha_shif_salaried_event_date",
            message="SHA/SHIF salaried event_date must fall inside the bound version window.",
            path="$.input_payload.sha_shif_salaried_inputs.payroll_items[0].event_date",
        )
    if (
        _parse_money(
            payroll_item.amount_kes,
            "$.input_payload.sha_shif_salaried_inputs.payroll_items[0].amount_kes",
        )
        <= ZERO
    ):
        _raise_rule_input_error(
            reason="unsupported_non_positive_contribution_basis",
            message="SHA/SHIF salaried contribution basis must be greater than zero.",
            path="$.input_payload.sha_shif_salaried_inputs.payroll_items[0].amount_kes",
        )

    assertion_types = {
        assertion.assertion_type
        for assertion in payload.sha_shif_salaried_inputs.employer_assertions
    }
    if "employer_registered" not in assertion_types:
        _raise_rule_input_error(
            reason="missing_required_employer_assertion",
            message="SHA/SHIF salaried requests require employer_registered assertion.",
            path="$.input_payload.sha_shif_salaried_inputs.employer_assertions",
        )
    if not {
        "deduction_path_asserted",
        "remittance_path_asserted",
    }.intersection(assertion_types):
        _raise_rule_input_error(
            reason="missing_required_employer_assertion",
            message=(
                "SHA/SHIF salaried requests require deduction_path_asserted or "
                "remittance_path_asserted."
            ),
            path="$.input_payload.sha_shif_salaried_inputs.employer_assertions",
        )
    for index, assertion in enumerate(payload.sha_shif_salaried_inputs.employer_assertions):
        if assertion.assertion_status not in {"asserted", "confirmed_by_evidence"}:
            _raise_rule_input_error(
                reason="unsupported_employer_assertion_status",
                message="SHA/SHIF salaried employer assertions must be asserted or confirmed.",
                path=(
                    "$.input_payload.sha_shif_salaried_inputs.employer_assertions"
                    f"[{index}].assertion_status"
                ),
            )


def _validate_non_salaried_lane(
    *,
    payload: SupportedShaShifExecutionInput,
    window: _SupportedWindow,
) -> None:
    if payload.contributor_context.contributor_kind != "household":
        _raise_rule_input_error(
            reason="unsupported_contributor_kind",
            message="SHA/SHIF non-salaried requests require contributor_kind=household.",
            path="$.input_payload.contributor_context.contributor_kind",
        )
    if payload.contributor_context.household_reference_id is None:
        _raise_rule_input_error(
            reason="missing_household_reference",
            message="SHA/SHIF non-salaried requests require household_reference_id.",
            path="$.input_payload.contributor_context.household_reference_id",
        )
    if len(payload.sha_shif_non_salaried_inputs.household_income_items) != 1:
        _raise_rule_input_error(
            reason="unsupported_sha_shif_non_salaried_item_count",
            message="SHA/SHIF non-salaried runtime supports exactly one household income item.",
            path="$.input_payload.sha_shif_non_salaried_inputs.household_income_items",
        )
    if not payload.sha_shif_non_salaried_inputs.household_member_reference_ids:
        _raise_rule_input_error(
            reason="missing_household_member_reference_ids",
            message="SHA/SHIF non-salaried requests require household_member_reference_ids.",
            path="$.input_payload.sha_shif_non_salaried_inputs.household_member_reference_ids",
        )
    if (
        payload.sha_shif_salaried_inputs.payroll_items
        or payload.sha_shif_salaried_inputs.employer_assertions
        or payload.sha_shif_salaried_inputs.remittance_reference_ids
    ):
        _raise_rule_input_error(
            reason="unsupported_sha_shif_salaried_inputs",
            message="Salaried inputs are outside the non-salaried SHA/SHIF lane.",
            path="$.input_payload.sha_shif_salaried_inputs",
        )
    if payload.operational_context.remittance_channel not in {
        "household_self_service",
        "sha_portal",
    }:
        _raise_rule_input_error(
            reason="unsupported_remittance_channel",
            message="SHA/SHIF non-salaried requests require a household payment channel.",
            path="$.input_payload.operational_context.remittance_channel",
        )

    income_item = payload.sha_shif_non_salaried_inputs.household_income_items[0]
    if income_item.income_basis_type != "annual_household_income":
        _raise_rule_input_error(
            reason="unsupported_sha_shif_non_salaried_basis_type",
            message=(
                "SHA/SHIF non-salaried runtime requires income_basis_type=annual_household_income."
            ),
            path=(
                "$.input_payload.sha_shif_non_salaried_inputs.household_income_items"
                "[0].income_basis_type"
            ),
        )
    if not _date_within_window(income_item.event_date, window):
        _raise_rule_input_error(
            reason="unsupported_sha_shif_non_salaried_event_date",
            message="SHA/SHIF non-salaried event_date must fall inside the bound version window.",
            path="$.input_payload.sha_shif_non_salaried_inputs.household_income_items[0].event_date",
        )
    if (
        _parse_money(
            income_item.amount_kes,
            "$.input_payload.sha_shif_non_salaried_inputs.household_income_items[0].amount_kes",
        )
        <= ZERO
    ):
        _raise_rule_input_error(
            reason="unsupported_non_positive_contribution_basis",
            message="SHA/SHIF non-salaried contribution basis must be greater than zero.",
            path="$.input_payload.sha_shif_non_salaried_inputs.household_income_items[0].amount_kes",
        )

    completed_assertions = [
        assertion
        for assertion in payload.sha_shif_non_salaried_inputs.means_testing_assertions
        if assertion.assertion_type == "means_testing_completed"
    ]
    if len(completed_assertions) != 1:
        _raise_rule_input_error(
            reason="missing_means_testing_completion",
            message=(
                "SHA/SHIF non-salaried runtime requires exactly one "
                "means_testing_completed assertion."
            ),
            path="$.input_payload.sha_shif_non_salaried_inputs.means_testing_assertions",
        )
    if completed_assertions[0].assertion_status not in {"asserted", "confirmed_by_evidence"}:
        _raise_rule_input_error(
            reason="unsupported_means_testing_assertion_status",
            message=(
                "means_testing_completed must be asserted or confirmed for the "
                "supported non-salaried lane."
            ),
            path="$.input_payload.sha_shif_non_salaried_inputs.means_testing_assertions",
        )
    for index, assertion in enumerate(
        payload.sha_shif_non_salaried_inputs.means_testing_assertions
    ):
        if assertion.assertion_status not in {"asserted", "confirmed_by_evidence"}:
            _raise_rule_input_error(
                reason="unsupported_means_testing_assertion_status",
                message="All provided means-testing assertions must be asserted or confirmed.",
                path=(
                    "$.input_payload.sha_shif_non_salaried_inputs.means_testing_assertions"
                    f"[{index}].assertion_status"
                ),
            )


def _build_salaried_result(
    *,
    payload: SupportedShaShifExecutionInput,
    prepared_input: PreparedExecutionInput,
    window: _SupportedWindow,
) -> dict[str, object]:
    payroll_item = payload.sha_shif_salaried_inputs.payroll_items[0]
    contribution_basis = _parse_money(
        payroll_item.amount_kes,
        "$.input_payload.sha_shif_salaried_inputs.payroll_items[0].amount_kes",
    )
    contribution_amount = max(
        (contribution_basis * SALARIED_RATE).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        SALARIED_FLOOR,
    )
    applied_policy_ids = sorted([*BASE_POLICY_IDS, "HCP-POL-204"])
    return _build_result_payload(
        payload=payload,
        prepared_input=prepared_input,
        window=window,
        resolved_domain_path="sha_shif_salaried",
        applied_policy_ids=applied_policy_ids,
        lane_decision_refs=[window.salaried_rule_id],
        lane_policy_ids=["HCP-POL-204"],
        contribution_basis=_format_money(contribution_basis),
        employee_contribution=_format_money(contribution_amount),
        employer_contribution=ZERO_STR,
        household_contribution=ZERO_STR,
        total_contribution=_format_money(contribution_amount),
        active_domain_key="sha_shif_salaried",
        contributor_kind="employee",
        contribution_summary_employee=_format_money(contribution_amount),
        contribution_summary_household=ZERO_STR,
        operational_decision_refs=["HCP-POL-206"],
    )


def _build_non_salaried_result(
    *,
    payload: SupportedShaShifExecutionInput,
    prepared_input: PreparedExecutionInput,
    window: _SupportedWindow,
) -> dict[str, object]:
    income_item = payload.sha_shif_non_salaried_inputs.household_income_items[0]
    contribution_basis = _parse_money(
        income_item.amount_kes,
        "$.input_payload.sha_shif_non_salaried_inputs.household_income_items[0].amount_kes",
    )
    contribution_amount = max(
        (contribution_basis * NON_SALARIED_RATE).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        NON_SALARIED_ANNUAL_FLOOR,
    )
    applied_policy_ids = sorted([*BASE_POLICY_IDS, "HCP-POL-203"])
    return _build_result_payload(
        payload=payload,
        prepared_input=prepared_input,
        window=window,
        resolved_domain_path="sha_shif_non_salaried",
        applied_policy_ids=applied_policy_ids,
        lane_decision_refs=[window.non_salaried_rule_id, window.non_salaried_gate_rule_id],
        lane_policy_ids=["HCP-POL-203"],
        contribution_basis=_format_money(contribution_basis),
        employee_contribution=ZERO_STR,
        employer_contribution=ZERO_STR,
        household_contribution=_format_money(contribution_amount),
        total_contribution=_format_money(contribution_amount),
        active_domain_key="sha_shif_non_salaried",
        contributor_kind="household",
        contribution_summary_employee=ZERO_STR,
        contribution_summary_household=_format_money(contribution_amount),
        operational_decision_refs=["HCP-POL-206"],
    )


def _build_result_payload(
    *,
    payload: SupportedShaShifExecutionInput,
    prepared_input: PreparedExecutionInput,
    window: _SupportedWindow,
    resolved_domain_path: str,
    applied_policy_ids: list[str],
    lane_decision_refs: list[str],
    lane_policy_ids: list[str],
    contribution_basis: str,
    employee_contribution: str,
    employer_contribution: str,
    household_contribution: str,
    total_contribution: str,
    active_domain_key: str,
    contributor_kind: str,
    contribution_summary_employee: str,
    contribution_summary_household: str,
    operational_decision_refs: list[str],
) -> dict[str, object]:
    version_identity = {
        "historical_version_id": window.historical_version_id,
        "tax_year": prepared_input.tax_year,
        "rule_version": prepared_input.rule_version,
        "regime_identifier": "sha_shif",
        "effective_start": window.effective_start.isoformat(),
        "effective_end": (
            None if window.effective_end is None else window.effective_end.isoformat()
        ),
        "version_selection_basis": payload.version_context.version_selection_basis,
        "governing_change_ids": [window.governing_change_id],
        "source_anchor_ids": list(window.source_anchor_ids),
    }
    validation_focus_domains = VALIDATION_FOCUS_DOMAINS_BY_PATH[resolved_domain_path]

    active_domain_outcome = _domain_outcome(
        status="computed",
        decision_refs=lane_decision_refs,
        applied_policy_ids=lane_policy_ids,
        source_anchor_ids=list(window.source_anchor_ids),
        contribution_basis_kes=contribution_basis,
        employee_contribution_kes=employee_contribution,
        employer_contribution_kes=employer_contribution,
        household_contribution_kes=household_contribution,
        total_contribution_kes=total_contribution,
    )
    inactive_policy_id = (
        "HCP-POL-203" if active_domain_key == "sha_shif_salaried" else "HCP-POL-204"
    )
    non_active_domain_outcome = _domain_outcome(
        status="not_applicable",
        decision_refs=[inactive_policy_id],
        applied_policy_ids=[inactive_policy_id],
        source_anchor_ids=list(window.source_anchor_ids),
    )

    return {
        "version_identity": version_identity,
        "contributor_outcome": {
            "contributor_kind": contributor_kind,
            "resolved_domain_path": resolved_domain_path,
            "regime_family": "sha_shif",
            "classification_outcome": "fully_classified",
        },
        "domain_outcomes": {
            "contributor_classification": _domain_outcome(
                status="computed",
                decision_refs=["HC-SHI-NPOL-0002"],
                applied_policy_ids=["HCP-POL-003", *lane_policy_ids],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "nhif_legacy": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-101"],
                applied_policy_ids=["HCP-POL-101"],
                source_anchor_ids=["HC-NHIF-REPEAL-2023-SHI-ACT"],
            ),
            "sha_shif_salaried": (
                active_domain_outcome
                if active_domain_key == "sha_shif_salaried"
                else non_active_domain_outcome
            ),
            "sha_shif_non_salaried": (
                active_domain_outcome
                if active_domain_key == "sha_shif_non_salaried"
                else non_active_domain_outcome
            ),
            "regime_selection": _domain_outcome(
                status="computed",
                decision_refs=["HCP-POL-002"],
                applied_policy_ids=["HCP-POL-001", "HCP-POL-002", "HCP-POL-201"],
                source_anchor_ids=["HC-NHIF-REPEAL-2023-SHI-ACT", "HC-SHI-ACT-2023-11-24"],
            ),
            "sha_staged_activation": _domain_outcome(
                status="computed",
                decision_refs=["HCP-POL-202"],
                applied_policy_ids=["HCP-POL-202"],
                source_anchor_ids=["HC-SHI-REG-2024-03-08"],
            ),
            "consolidated_state_binding": _domain_outcome(
                status="computed",
                decision_refs=["HCP-POL-205", window.consolidated_policy_id],
                applied_policy_ids=["HCP-POL-205", window.consolidated_policy_id],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "exemptions_and_special_cases": _domain_outcome(
                status="not_applicable",
                decision_refs=[UNRESOLVED_SPECIAL_CASE_POLICY_ID],
                applied_policy_ids=[UNRESOLVED_SPECIAL_CASE_POLICY_ID],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "mixed_context_paths": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-304"],
                applied_policy_ids=["HCP-POL-304"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "operational_interaction": _domain_outcome(
                status="computed",
                decision_refs=operational_decision_refs,
                applied_policy_ids=["HCP-POL-206"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "validation_evidence": _domain_outcome(
                status="computed",
                decision_refs=["HCP-POL-005"],
                applied_policy_ids=["HCP-POL-005"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "version_selection": _domain_outcome(
                status="computed",
                decision_refs=["HC-SHI-NPOL-0001"],
                applied_policy_ids=["HCP-POL-001", "HCP-POL-202", "HCP-POL-205"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
        },
        "contribution_summary": {
            "regime_family": "sha_shif",
            "coverage_status": "implementation_ready",
            "summary_status": "computed",
            "contribution_basis_kes": contribution_basis,
            "employee_contribution_kes": contribution_summary_employee,
            "employer_contribution_kes": ZERO_STR,
            "household_contribution_kes": contribution_summary_household,
            "total_contribution_kes": total_contribution,
            "currency": "KES",
        },
        "unsupported_or_unresolved": [],
        "traceability": {
            "input_hash": prepared_input.input_hash,
            "applied_policy_ids": applied_policy_ids,
            "source_anchor_ids": list(window.source_anchor_ids),
            "governing_change_ids": [window.governing_change_id],
            "validation_focus_domains": validation_focus_domains,
            "computation_status": "complete",
            "replay_safe": True,
        },
    }


def _date_within_window(event_date: date, window: _SupportedWindow) -> bool:
    if event_date < window.effective_start:
        return False
    if window.effective_end is not None and event_date > window.effective_end:
        return False
    return True


def _domain_outcome(
    *,
    status: str,
    decision_refs: list[str],
    applied_policy_ids: list[str],
    source_anchor_ids: list[str],
    contribution_basis_kes: str | None = None,
    employee_contribution_kes: str | None = None,
    employer_contribution_kes: str | None = None,
    household_contribution_kes: str | None = None,
    total_contribution_kes: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "contribution_basis_kes": contribution_basis_kes,
        "employee_contribution_kes": employee_contribution_kes,
        "employer_contribution_kes": employer_contribution_kes,
        "household_contribution_kes": household_contribution_kes,
        "total_contribution_kes": total_contribution_kes,
        "decision_refs": decision_refs,
        "applied_policy_ids": applied_policy_ids,
        "source_anchor_ids": source_anchor_ids,
    }


def _parse_money(value: str, path: str) -> Decimal:
    if not MONEY_PATTERN.fullmatch(value):
        _raise_rule_input_error(
            reason="invalid_money_amount",
            message="Money amounts must use fixed two-decimal string formatting.",
            path=path,
        )
    return Decimal(value)


def _format_money(value: Decimal) -> str:
    return str(value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def _raise_rule_input_error(
    *, reason: str, message: str, path: str, details: str | None = None
) -> NoReturn:
    if details is not None:
        message = f"{message} Details: {details}"
    raise InputHashError(reason=reason, message=message, path=path)
