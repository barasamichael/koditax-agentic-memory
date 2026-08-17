"""Implement the first governed NHIF legacy health-contribution rule pack."""

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
    resolve_nhif_special_member,
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
SUPPORTED_BINDINGS = {
    "health_contribution_nhif_legacy_v1_2010_07_16",
    "health_contribution_nhif_legacy_v1_2015_04_01",
    "health_contribution_nhif_legacy_v1_2021_05_28",
    "health_contribution_nhif_legacy_v1_2022_12_31_reg",
}
BASE_POLICY_IDS = [
    "HCP-POL-001",
    "HCP-POL-002",
    "HCP-POL-003",
    "HCP-POL-101",
    "HCP-POL-102",
    "HCP-POL-103",
    "HCP-POL-110",
]
VALIDATION_FOCUS_DOMAINS = [
    "HCD-CORE-CONTRIBUTOR-CLASSIFICATION",
    "HCD-CORE-NHIF-LEGACY",
    "HCD-XCUT-VERSION-SELECTION",
    "HCD-XCUT-VALIDATION-EVIDENCE",
]


@dataclass(frozen=True)
class _ContributionBand:
    minimum: Decimal | None
    maximum: Decimal | None
    contribution: Decimal
    include_minimum: bool = True
    include_maximum: bool = True


@dataclass(frozen=True)
class _SupportedWindow:
    binding_id: str
    historical_version_id: str
    effective_start: date
    effective_end: date
    governing_change_id: str
    source_anchor_ids: tuple[str, ...]
    schedule_rule_id: str
    remittance_rule_id: str
    applied_policy_id: str
    bands: tuple[_ContributionBand, ...]


WINDOWS_BY_BINDING_ID: dict[str, _SupportedWindow] = {
    "health_contribution_nhif_legacy_v1_2010_07_16": _SupportedWindow(
        binding_id="health_contribution_nhif_legacy_v1_2010_07_16",
        historical_version_id="HCH-VER-20100716-A",
        effective_start=date(2010, 7, 16),
        effective_end=date(2014, 12, 7),
        governing_change_id="HC-CHG-2010-07-16-A",
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2010-07-16",),
        schedule_rule_id="HC-NHIF-NPOL-2010-001",
        remittance_rule_id="HC-NHIF-NPOL-2010-002",
        applied_policy_id="HCP-POL-106",
        bands=(
            _ContributionBand(None, Decimal("5999.00"), Decimal("150.00"), include_maximum=False),
            _ContributionBand(Decimal("6000.00"), Decimal("7999.00"), Decimal("300.00")),
            _ContributionBand(Decimal("8000.00"), Decimal("11999.00"), Decimal("400.00")),
            _ContributionBand(Decimal("12000.00"), Decimal("14999.00"), Decimal("500.00")),
            _ContributionBand(Decimal("15000.00"), Decimal("19999.00"), Decimal("600.00")),
            _ContributionBand(Decimal("20000.00"), Decimal("24999.00"), Decimal("750.00")),
            _ContributionBand(Decimal("25000.00"), Decimal("29999.00"), Decimal("850.00")),
            _ContributionBand(Decimal("30000.00"), Decimal("49999.00"), Decimal("1000.00")),
            _ContributionBand(Decimal("50000.00"), Decimal("99999.00"), Decimal("1500.00")),
            _ContributionBand(
                Decimal("100000.00"), None, Decimal("2000.00"), include_minimum=False
            ),
        ),
    ),
    "health_contribution_nhif_legacy_v1_2015_04_01": _SupportedWindow(
        binding_id="health_contribution_nhif_legacy_v1_2015_04_01",
        historical_version_id="HCH-VER-20150401-A",
        effective_start=date(2015, 4, 1),
        effective_end=date(2021, 3, 29),
        governing_change_id="HC-CHG-2015-04-01-A",
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2015-04-01",),
        schedule_rule_id="HC-NHIF-NPOL-2015-001",
        remittance_rule_id="HC-NHIF-NPOL-2015-002",
        applied_policy_id="HCP-POL-107",
        bands=(
            _ContributionBand(None, Decimal("5999.00"), Decimal("150.00")),
            _ContributionBand(Decimal("6000.00"), Decimal("7999.00"), Decimal("300.00")),
            _ContributionBand(Decimal("8000.00"), Decimal("11999.00"), Decimal("400.00")),
            _ContributionBand(Decimal("12000.00"), Decimal("14999.00"), Decimal("500.00")),
            _ContributionBand(Decimal("15000.00"), Decimal("19999.00"), Decimal("600.00")),
            _ContributionBand(Decimal("20000.00"), Decimal("24999.00"), Decimal("750.00")),
            _ContributionBand(Decimal("25000.00"), Decimal("29999.00"), Decimal("850.00")),
            _ContributionBand(Decimal("30000.00"), Decimal("34999.00"), Decimal("900.00")),
            _ContributionBand(Decimal("35000.00"), Decimal("39999.00"), Decimal("950.00")),
            _ContributionBand(Decimal("40000.00"), Decimal("44999.00"), Decimal("1000.00")),
            _ContributionBand(Decimal("45000.00"), Decimal("49999.00"), Decimal("1100.00")),
            _ContributionBand(Decimal("50000.00"), Decimal("59999.00"), Decimal("1200.00")),
            _ContributionBand(Decimal("60000.00"), Decimal("69000.00"), Decimal("1300.00")),
            _ContributionBand(Decimal("70000.00"), Decimal("79999.00"), Decimal("1400.00")),
            _ContributionBand(Decimal("80000.00"), Decimal("89000.00"), Decimal("1500.00")),
            _ContributionBand(Decimal("90000.00"), Decimal("99000.00"), Decimal("1600.00")),
            _ContributionBand(Decimal("100000.00"), None, Decimal("1700.00")),
        ),
    ),
    "health_contribution_nhif_legacy_v1_2021_05_28": _SupportedWindow(
        binding_id="health_contribution_nhif_legacy_v1_2021_05_28",
        historical_version_id="HCH-VER-20210528-A",
        effective_start=date(2021, 5, 28),
        effective_end=date(2022, 12, 30),
        governing_change_id="HC-CHG-2021-05-28-A",
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2021-05-28",),
        schedule_rule_id="HC-NHIF-NPOL-2021-001",
        remittance_rule_id="HC-NHIF-NPOL-2021-002",
        applied_policy_id="HCP-POL-108",
        bands=(
            _ContributionBand(None, Decimal("5999.00"), Decimal("150.00")),
            _ContributionBand(Decimal("6000.00"), Decimal("7999.00"), Decimal("300.00")),
            _ContributionBand(Decimal("8000.00"), Decimal("11999.00"), Decimal("400.00")),
            _ContributionBand(Decimal("12000.00"), Decimal("14999.00"), Decimal("500.00")),
            _ContributionBand(Decimal("15000.00"), Decimal("19999.00"), Decimal("600.00")),
            _ContributionBand(Decimal("20000.00"), Decimal("24999.00"), Decimal("750.00")),
            _ContributionBand(Decimal("25000.00"), Decimal("29999.00"), Decimal("850.00")),
            _ContributionBand(Decimal("30000.00"), Decimal("34999.00"), Decimal("900.00")),
            _ContributionBand(Decimal("35000.00"), Decimal("39999.00"), Decimal("950.00")),
            _ContributionBand(Decimal("40000.00"), Decimal("44999.00"), Decimal("1000.00")),
            _ContributionBand(Decimal("45000.00"), Decimal("49999.00"), Decimal("1100.00")),
            _ContributionBand(Decimal("50000.00"), Decimal("59999.00"), Decimal("1200.00")),
            _ContributionBand(Decimal("60000.00"), Decimal("69000.00"), Decimal("1300.00")),
            _ContributionBand(Decimal("70000.00"), Decimal("79999.00"), Decimal("1400.00")),
            _ContributionBand(Decimal("80000.00"), Decimal("89000.00"), Decimal("1500.00")),
            _ContributionBand(Decimal("90000.00"), Decimal("99000.00"), Decimal("1600.00")),
            _ContributionBand(Decimal("100000.00"), None, Decimal("1700.00")),
        ),
    ),
    "health_contribution_nhif_legacy_v1_2022_12_31_reg": _SupportedWindow(
        binding_id="health_contribution_nhif_legacy_v1_2022_12_31_reg",
        historical_version_id="HCH-VER-20221231-REG",
        effective_start=date(2022, 12, 31),
        effective_end=date(2023, 11, 21),
        governing_change_id="HC-CHG-2022-12-31-B",
        source_anchor_ids=("HC-NHIF-CONTRIB-REG-2022-12-31",),
        schedule_rule_id="HC-NHIF-NPOL-2022-001",
        remittance_rule_id="HC-NHIF-NPOL-2022-002",
        applied_policy_id="HCP-POL-109",
        bands=(
            _ContributionBand(None, Decimal("5999.00"), Decimal("150.00")),
            _ContributionBand(Decimal("6000.00"), Decimal("7999.00"), Decimal("300.00")),
            _ContributionBand(Decimal("8000.00"), Decimal("11999.00"), Decimal("400.00")),
            _ContributionBand(Decimal("12000.00"), Decimal("14999.00"), Decimal("500.00")),
            _ContributionBand(Decimal("15000.00"), Decimal("19999.00"), Decimal("600.00")),
            _ContributionBand(Decimal("20000.00"), Decimal("24999.00"), Decimal("750.00")),
            _ContributionBand(Decimal("25000.00"), Decimal("29999.00"), Decimal("850.00")),
            _ContributionBand(Decimal("30000.00"), Decimal("34999.00"), Decimal("900.00")),
            _ContributionBand(Decimal("35000.00"), Decimal("39999.00"), Decimal("950.00")),
            _ContributionBand(Decimal("40000.00"), Decimal("44999.00"), Decimal("1000.00")),
            _ContributionBand(Decimal("45000.00"), Decimal("49999.00"), Decimal("1100.00")),
            _ContributionBand(Decimal("50000.00"), Decimal("59999.00"), Decimal("1200.00")),
            _ContributionBand(Decimal("60000.00"), Decimal("69000.00"), Decimal("1300.00")),
            _ContributionBand(Decimal("70000.00"), Decimal("79999.00"), Decimal("1400.00")),
            _ContributionBand(Decimal("80000.00"), Decimal("89000.00"), Decimal("1500.00")),
            _ContributionBand(Decimal("90000.00"), Decimal("99000.00"), Decimal("1600.00")),
            _ContributionBand(Decimal("100000.00"), None, Decimal("1700.00")),
        ),
    ),
}


class SupportedNhifLegacyExecutionInput(BaseModel):
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

    earning_items: list[SupportedNhifLegacyEarningItem]
    member_class_assertions: list[SupportedMemberClassAssertion]
    deduction_reference_ids: list[str]


class SupportedNhifLegacyEarningItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    income_basis_type: str
    amount_kes: str
    event_date: date
    reference_id: str | None = None


class SupportedMemberClassAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_type: str
    assertion_status: str
    source_reference_id: str | None = None


class SupportedShaShifSalariedInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payroll_items: list[object]
    employer_assertions: list[object]
    remittance_reference_ids: list[str]


class SupportedShaShifNonSalariedInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    household_income_items: list[object]
    means_testing_assertions: list[object]
    household_member_reference_ids: list[str]


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


def execute_nhif_legacy_rule_pack(
    prepared_input: PreparedExecutionInput,
    bound_rule: BoundRule,
) -> dict[str, object]:
    """Compute governed NHIF legacy contribution outcome for supported windows only."""

    window = _resolve_window(bound_rule)
    payload = _parse_supported_payload(prepared_input)
    _validate_supported_payload(payload=payload, prepared_input=prepared_input, window=window)

    member_class = payload.nhif_legacy_inputs.member_class_assertions[0].assertion_type
    contributor_kind = payload.contributor_context.contributor_kind
    earning_item = payload.nhif_legacy_inputs.earning_items[0]
    amount = _parse_money(
        earning_item.amount_kes,
        "$.input_payload.nhif_legacy_inputs.earning_items[0].amount_kes",
    )
    special_case_resolution = resolve_nhif_special_member(
        member_class=member_class,
        contributor_kind=contributor_kind,
        income_basis_type=earning_item.income_basis_type,
        amount=amount,
        schedule_rule_id=window.schedule_rule_id,
        window_policy_id=window.applied_policy_id,
        contributor_kind_path="$.input_payload.contributor_context.contributor_kind",
        income_basis_type_path="$.input_payload.nhif_legacy_inputs.earning_items[0].income_basis_type",
        amount_path="$.input_payload.nhif_legacy_inputs.earning_items[0].amount_kes",
    )

    if special_case_resolution is None:
        contribution_basis = amount
        contribution_amount = _resolve_standard_member_contribution(amount=amount, window=window)
        window_decision_refs = [window.schedule_rule_id, window.remittance_rule_id]
        exemptions_domain_outcome = _domain_outcome(
            status="not_applicable",
            decision_refs=[UNRESOLVED_SPECIAL_CASE_POLICY_ID],
            applied_policy_ids=[UNRESOLVED_SPECIAL_CASE_POLICY_ID],
            source_anchor_ids=list(window.source_anchor_ids),
        )
    else:
        contribution_basis = special_case_resolution.contribution_basis
        contribution_amount = special_case_resolution.contribution_amount
        window_decision_refs = [window.schedule_rule_id]
        exemptions_domain_outcome = _domain_outcome(
            status="computed",
            decision_refs=list(special_case_resolution.decision_refs),
            applied_policy_ids=list(special_case_resolution.applied_policy_ids),
            source_anchor_ids=list(window.source_anchor_ids),
            contribution_basis_kes=_format_money(contribution_basis),
            employee_contribution_kes=_format_money(contribution_amount),
            employer_contribution_kes=ZERO_STR,
            household_contribution_kes=ZERO_STR,
            total_contribution_kes=_format_money(contribution_amount),
        )

    applied_policy_ids = sorted([*BASE_POLICY_IDS, window.applied_policy_id])
    version_identity = {
        "historical_version_id": window.historical_version_id,
        "tax_year": prepared_input.tax_year,
        "rule_version": prepared_input.rule_version,
        "regime_identifier": "nhif_legacy",
        "effective_start": window.effective_start.isoformat(),
        "effective_end": window.effective_end.isoformat(),
        "version_selection_basis": payload.version_context.version_selection_basis,
        "governing_change_ids": [window.governing_change_id],
        "source_anchor_ids": list(window.source_anchor_ids),
    }

    return {
        "version_identity": version_identity,
        "contributor_outcome": {
            "contributor_kind": contributor_kind,
            "resolved_domain_path": "nhif_legacy",
            "regime_family": "nhif_legacy",
            "classification_outcome": "fully_classified",
        },
        "domain_outcomes": {
            "contributor_classification": _domain_outcome(
                status="computed",
                decision_refs=["HC-NHIF-NPOL-0002"],
                applied_policy_ids=["HCP-POL-003", "HCP-POL-110"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "nhif_legacy": _domain_outcome(
                status="computed",
                decision_refs=window_decision_refs,
                applied_policy_ids=applied_policy_ids,
                source_anchor_ids=list(window.source_anchor_ids),
                contribution_basis_kes=_format_money(contribution_basis),
                employee_contribution_kes=_format_money(contribution_amount),
                employer_contribution_kes=ZERO_STR,
                household_contribution_kes=ZERO_STR,
                total_contribution_kes=_format_money(contribution_amount),
            ),
            "sha_shif_salaried": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-201"],
                applied_policy_ids=["HCP-POL-201"],
                source_anchor_ids=["HC-SHI-ACT-2023-11-24"],
            ),
            "sha_shif_non_salaried": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-203"],
                applied_policy_ids=["HCP-POL-203"],
                source_anchor_ids=["HC-SHI-REG-2024-03-08"],
            ),
            "regime_selection": _domain_outcome(
                status="computed",
                decision_refs=["HCP-POL-002"],
                applied_policy_ids=["HCP-POL-001", "HCP-POL-002", "HCP-POL-101"],
                source_anchor_ids=["HC-NHIF-ACT-1999-01-08", "HC-NHIF-REPEAL-2023-SHI-ACT"],
            ),
            "sha_staged_activation": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-202"],
                applied_policy_ids=["HCP-POL-202"],
                source_anchor_ids=["HC-SHI-REG-2024-03-08"],
            ),
            "consolidated_state_binding": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-004"],
                applied_policy_ids=["HCP-POL-004"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "exemptions_and_special_cases": exemptions_domain_outcome,
            "mixed_context_paths": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-304"],
                applied_policy_ids=["HCP-POL-304"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "operational_interaction": _domain_outcome(
                status="not_applicable",
                decision_refs=["HCP-POL-105"],
                applied_policy_ids=["HCP-POL-105"],
                source_anchor_ids=["HC-NHIF-OPS-EMPLOYERS-PAGE"],
            ),
            "validation_evidence": _domain_outcome(
                status="computed",
                decision_refs=["HCP-POL-005"],
                applied_policy_ids=["HCP-POL-005"],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
            "version_selection": _domain_outcome(
                status="computed",
                decision_refs=["HC-NHIF-NPOL-0001"],
                applied_policy_ids=["HCP-POL-001", "HCP-POL-103", window.applied_policy_id],
                source_anchor_ids=list(window.source_anchor_ids),
            ),
        },
        "contribution_summary": {
            "regime_family": "nhif_legacy",
            "coverage_status": "implementation_ready",
            "summary_status": "computed",
            "contribution_basis_kes": _format_money(contribution_basis),
            "employee_contribution_kes": _format_money(contribution_amount),
            "employer_contribution_kes": ZERO_STR,
            "household_contribution_kes": ZERO_STR,
            "total_contribution_kes": _format_money(contribution_amount),
            "currency": "KES",
        },
        "unsupported_or_unresolved": [],
        "traceability": {
            "input_hash": prepared_input.input_hash,
            "applied_policy_ids": applied_policy_ids,
            "source_anchor_ids": list(window.source_anchor_ids),
            "governing_change_ids": [window.governing_change_id],
            "validation_focus_domains": VALIDATION_FOCUS_DOMAINS,
            "computation_status": "complete",
            "replay_safe": True,
        },
    }


def _resolve_window(bound_rule: BoundRule) -> _SupportedWindow:
    if bound_rule.binding_id not in SUPPORTED_BINDINGS:
        _raise_rule_input_error(
            reason="invalid_nhif_legacy_binding",
            message="NHIF legacy rule pack received an unexpected binding.",
            path="$.binding_id",
        )
    return WINDOWS_BY_BINDING_ID[bound_rule.binding_id]


def _parse_supported_payload(
    prepared_input: PreparedExecutionInput,
) -> SupportedNhifLegacyExecutionInput:
    try:
        return SupportedNhifLegacyExecutionInput.model_validate(
            prepared_input.canonical_input_payload
        )
    except PydanticValidationError as error:
        _raise_rule_input_error(
            reason="unsupported_nhif_legacy_request_shape",
            message="NHIF legacy request does not match the supported governed shape.",
            path="$.input_payload",
            details=str(error),
        )


def _validate_supported_payload(
    payload: SupportedNhifLegacyExecutionInput,
    prepared_input: PreparedExecutionInput,
    window: _SupportedWindow,
) -> None:
    if prepared_input.tax_type != "health_contribution":
        _raise_rule_input_error(
            reason="unsupported_tax_type",
            message="NHIF legacy rule pack supports health_contribution tax_type only.",
            path="$.tax_type",
        )
    if prepared_input.regime_type != "health_contribution":
        _raise_rule_input_error(
            reason="unsupported_regime_type",
            message="NHIF legacy rule pack supports health_contribution regime_type only.",
            path="$.regime_type",
        )
    if prepared_input.regime_identifier != "nhif_legacy":
        _raise_rule_input_error(
            reason="unsupported_regime_identifier",
            message="NHIF legacy rule pack supports regime_identifier=nhif_legacy only.",
            path="$.regime_identifier",
        )
    if prepared_input.tax_year != payload.version_context.primary_effective_date.year:
        _raise_rule_input_error(
            reason="tax_year_primary_effective_date_mismatch",
            message=(
                "tax_year must match the year of primary_effective_date for NHIF legacy execution."
            ),
            path="$.tax_year",
        )
    if payload.version_context.version_selection_basis not in {
        "specific_event_date",
        "payroll_period_end",
    }:
        _raise_rule_input_error(
            reason="unsupported_version_selection_basis",
            message=(
                "NHIF legacy rule pack supports specific_event_date or payroll_period_end only."
            ),
            path="$.input_payload.version_context.version_selection_basis",
        )
    if not (
        window.effective_start
        <= payload.version_context.primary_effective_date
        <= window.effective_end
    ):
        _raise_rule_input_error(
            reason="unsupported_effective_date_window",
            message="primary_effective_date is outside the supported NHIF legacy window.",
            path="$.input_payload.version_context.primary_effective_date",
        )
    if payload.version_context.historical_version_id not in (None, window.historical_version_id):
        _raise_rule_input_error(
            reason="unsupported_historical_version_id",
            message="historical_version_id does not match the bound NHIF legacy window.",
            path="$.input_payload.version_context.historical_version_id",
        )
    if payload.version_context.governing_change_ids not in ([], [window.governing_change_id]):
        _raise_rule_input_error(
            reason="unsupported_governing_change_ids",
            message="governing_change_ids must be empty or match the bound NHIF change anchor.",
            path="$.input_payload.version_context.governing_change_ids",
        )
    if payload.version_context.source_anchor_ids not in ([], list(window.source_anchor_ids)):
        _raise_rule_input_error(
            reason="unsupported_source_anchor_ids",
            message="source_anchor_ids must be empty or match the bound NHIF source anchors.",
            path="$.input_payload.version_context.source_anchor_ids",
        )
    if payload.contributor_context.asserted_domain_path != "nhif_legacy":
        _raise_rule_input_error(
            reason="unsupported_asserted_domain_path",
            message="NHIF legacy rule pack supports asserted_domain_path=nhif_legacy only.",
            path="$.input_payload.contributor_context.asserted_domain_path",
        )
    reject_governed_mixed_context_request(prepared_input)
    reject_unresolved_special_case_assertions(
        payload.special_case_assertions.assertion_items,
        path="$.input_payload.special_case_assertions.assertion_items",
    )
    if (
        payload.sha_shif_salaried_inputs.payroll_items
        or payload.sha_shif_salaried_inputs.employer_assertions
        or payload.sha_shif_salaried_inputs.remittance_reference_ids
    ):
        _raise_rule_input_error(
            reason="unsupported_sha_shif_salaried_inputs",
            message="SHA/SHIF salaried inputs are outside the NHIF legacy runtime lane.",
            path="$.input_payload.sha_shif_salaried_inputs",
        )
    if (
        payload.sha_shif_non_salaried_inputs.household_income_items
        or payload.sha_shif_non_salaried_inputs.means_testing_assertions
        or payload.sha_shif_non_salaried_inputs.household_member_reference_ids
    ):
        _raise_rule_input_error(
            reason="unsupported_sha_shif_non_salaried_inputs",
            message="SHA/SHIF non-salaried inputs are outside the NHIF legacy runtime lane.",
            path="$.input_payload.sha_shif_non_salaried_inputs",
        )
    if (
        payload.operational_context.remittance_channel == "sha_portal"
        or "payment_and_access_live" in payload.operational_context.workflow_flags
    ):
        _raise_rule_input_error(
            reason="unsupported_operational_context",
            message="SHA operational context is outside the NHIF legacy runtime lane.",
            path="$.input_payload.operational_context",
        )
    if not payload.traceability_context.source_record_ids:
        _raise_rule_input_error(
            reason="missing_traceability_source_records",
            message="At least one source_record_id is required for NHIF legacy execution.",
            path="$.input_payload.traceability_context.source_record_ids",
        )
    if len(payload.nhif_legacy_inputs.earning_items) != 1:
        _raise_rule_input_error(
            reason="unsupported_nhif_legacy_earning_item_count",
            message="NHIF legacy rule pack supports exactly one earning item.",
            path="$.input_payload.nhif_legacy_inputs.earning_items",
        )
    if len(payload.nhif_legacy_inputs.member_class_assertions) != 1:
        _raise_rule_input_error(
            reason="unsupported_member_class_assertion_count",
            message="NHIF legacy rule pack supports exactly one member class assertion.",
            path="$.input_payload.nhif_legacy_inputs.member_class_assertions",
        )

    earning_item = payload.nhif_legacy_inputs.earning_items[0]
    member_class = payload.nhif_legacy_inputs.member_class_assertions[0]
    if not (window.effective_start <= earning_item.event_date <= window.effective_end):
        _raise_rule_input_error(
            reason="unsupported_earning_item_event_date",
            message="NHIF earning item event_date must fall inside the bound version window.",
            path="$.input_payload.nhif_legacy_inputs.earning_items[0].event_date",
        )
    if member_class.assertion_status not in {"asserted", "confirmed_by_evidence"}:
        _raise_rule_input_error(
            reason="unsupported_member_class_assertion_status",
            message=(
                "NHIF legacy rule pack requires an asserted or evidence-confirmed member class."
            ),
            path="$.input_payload.nhif_legacy_inputs.member_class_assertions[0].assertion_status",
        )

    amount = _parse_money(
        earning_item.amount_kes, "$.input_payload.nhif_legacy_inputs.earning_items[0].amount_kes"
    )
    if amount <= ZERO:
        _raise_rule_input_error(
            reason="unsupported_non_positive_contribution_basis",
            message="NHIF legacy contribution basis must be greater than zero.",
            path="$.input_payload.nhif_legacy_inputs.earning_items[0].amount_kes",
        )

    if member_class.assertion_type == "standard_member":
        if payload.contributor_context.contributor_kind != "employee":
            _raise_rule_input_error(
                reason="unsupported_contributor_kind",
                message="standard_member requests require contributor_kind=employee.",
                path="$.input_payload.contributor_context.contributor_kind",
            )
        if earning_item.income_basis_type != "salary_band_basis":
            _raise_rule_input_error(
                reason="unsupported_nhif_income_basis_type",
                message="standard_member requests require income_basis_type=salary_band_basis.",
                path="$.input_payload.nhif_legacy_inputs.earning_items[0].income_basis_type",
            )
        return

    if member_class.assertion_type == "special_member":
        resolve_nhif_special_member(
            member_class=member_class.assertion_type,
            contributor_kind=payload.contributor_context.contributor_kind,
            income_basis_type=earning_item.income_basis_type,
            amount=amount,
            schedule_rule_id=window.schedule_rule_id,
            window_policy_id=window.applied_policy_id,
            contributor_kind_path="$.input_payload.contributor_context.contributor_kind",
            income_basis_type_path="$.input_payload.nhif_legacy_inputs.earning_items[0].income_basis_type",
            amount_path="$.input_payload.nhif_legacy_inputs.earning_items[0].amount_kes",
        )
        return

    _raise_rule_input_error(
        reason="unsupported_member_class_assertion_type",
        message="NHIF legacy rule pack supports standard_member or special_member only.",
        path="$.input_payload.nhif_legacy_inputs.member_class_assertions[0].assertion_type",
    )


def _resolve_standard_member_contribution(amount: Decimal, window: _SupportedWindow) -> Decimal:
    for band in window.bands:
        if _matches_band(amount=amount, band=band):
            return band.contribution
    _raise_rule_input_error(
        reason="unsupported_nhif_legacy_amount_gap",
        message=(
            "The asserted NHIF contribution basis falls into "
            "an uncovered published interval for this window."
        ),
        path="$.input_payload.nhif_legacy_inputs.earning_items[0].amount_kes",
    )


def _matches_band(amount: Decimal, band: _ContributionBand) -> bool:
    if band.minimum is not None:
        if band.include_minimum:
            if amount < band.minimum:
                return False
        elif amount <= band.minimum:
            return False
    if band.maximum is not None:
        if band.include_maximum:
            if amount > band.maximum:
                return False
        elif amount >= band.maximum:
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
