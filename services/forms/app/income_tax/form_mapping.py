"""Map finalized governed income-tax outputs into form-ready structures."""

from __future__ import annotations

from typing import cast
from collections.abc import Mapping

from services.forms.app.income_tax.form_audit_coverage import (
    build_income_tax_form_mapping_audit_evidence,
)

SUPPORTED_HISTORICAL_VERSION_IDS = {
    "KIT-VER-20210101-A",
    "KIT-VER-20230701-A",
}
FORM_TYPE = "income_tax_return"
FORM_VERSION = "income_tax_vertical_slice_v1"


class IncomeTaxFormMappingError(RuntimeError):
    """Represent deterministic form-mapping failures for supported income-tax lanes."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured error details."""

        return {"reason": self.reason, **self._details}


def map_finalized_income_tax_output_to_form_ready(
    finalized_output: Mapping[str, object],
) -> dict[str, object]:
    """Map one finalized governed income-tax output into a form-ready structure."""

    source = _as_object(finalized_output, reason="invalid_mapping_input")
    tax_type = _require_string(source, "tax_type")
    regime_type = _require_string(source, "regime_type")
    tax_year = _require_int(source, "tax_year")
    rule_version = _require_string(source, "rule_version")
    input_hash = _require_string(source, "input_hash")
    computation_id = _require_string(source, "computation_id")
    finalization_status = _require_string(source, "finalization_status")
    finalized_at = _require_string(source, "finalized_at")
    finalized_audit_event_id = _require_string(source, "finalized_audit_event_id")

    if tax_type != "income_tax":
        raise IncomeTaxFormMappingError(
            reason="unsupported_tax_type",
            message="Form mapping supports governed income-tax outputs only.",
            details={"tax_type": tax_type},
        )
    if regime_type != "income_tax":
        raise IncomeTaxFormMappingError(
            reason="unsupported_regime_type",
            message="Form mapping supports governed income-tax regime outputs only.",
            details={"regime_type": regime_type},
        )
    if finalization_status != "finalized":
        raise IncomeTaxFormMappingError(
            reason="computation_not_finalized",
            message="Form mapping requires a finalized computation output.",
            details={"finalization_status": finalization_status},
        )

    result_payload = _require_object(source, "result_payload")
    unsupported_or_unresolved = _require_list(result_payload, "unsupported_or_unresolved")
    if unsupported_or_unresolved:
        raise IncomeTaxFormMappingError(
            reason="unsupported_result_scope",
            message="Form mapping does not accept unresolved or unsupported computation outputs.",
            details={"unsupported_or_unresolved": unsupported_or_unresolved},
        )

    version_identity = _require_object(result_payload, "version_identity")
    historical_version_id = _require_string(version_identity, "historical_version_id")
    if historical_version_id not in SUPPORTED_HISTORICAL_VERSION_IDS:
        raise IncomeTaxFormMappingError(
            reason="unsupported_historical_version",
            message="Form mapping supports governed historical windows only.",
            details={"historical_version_id": historical_version_id},
        )

    taxpayer_outcome = _require_object(result_payload, "taxpayer_outcome")
    taxpayer_kind = _require_string(taxpayer_outcome, "taxpayer_kind")
    resident_status = _require_string(taxpayer_outcome, "resident_status")
    classification_outcome = _require_string(taxpayer_outcome, "classification_outcome")

    liability_summary = _require_object(result_payload, "liability_summary")
    impact_summary = _require_object(result_payload, "impact_summary")
    domain_outcomes = _require_object(result_payload, "domain_outcomes")
    treatment_decisions = _require_object(result_payload, "treatment_decisions")
    traceability = _require_object(result_payload, "traceability")

    employment_domain = _require_object(domain_outcomes, "employment")
    investment_domain = _require_object(domain_outcomes, "investment")
    deductions_domain = _require_object(domain_outcomes, "deductions_and_exemptions")
    reliefs_domain = _require_object(domain_outcomes, "reliefs")
    withholding_domain = _require_object(domain_outcomes, "withholding")
    _reject_unsupported_domain_activity(domain_outcomes)

    lane_id = _resolve_supported_lane(
        historical_version_id=historical_version_id,
        resident_status=resident_status,
        employment_domain=employment_domain,
        investment_domain=investment_domain,
        deductions_domain=deductions_domain,
        reliefs_domain=reliefs_domain,
        withholding_domain=withholding_domain,
        liability_summary=liability_summary,
        impact_summary=impact_summary,
        treatment_decisions=treatment_decisions,
        taxpayer_kind=taxpayer_kind,
    )

    source_anchor_ids = _list_of_strings(version_identity, "source_anchor_ids")
    applied_policy_ids = _list_of_strings(traceability, "applied_policy_ids")
    validation_focus_domains = _list_of_strings(traceability, "validation_focus_domains")
    withholding_treatments = _list_of_objects(treatment_decisions, "withholding_treatments")
    adjacent_regime_flags = _list_of_strings(treatment_decisions, "adjacent_regime_flags")
    relief_impacts = _list_of_objects(impact_summary, "relief_impacts")
    deduction_impacts = _list_of_objects(impact_summary, "deduction_impacts")
    exemption_impacts = _list_of_objects(impact_summary, "exemption_impacts")

    mapped_output: dict[str, object] = {
        "mapping_status": "ok",
        "form_type": FORM_TYPE,
        "form_version": FORM_VERSION,
        "supported_lane_id": lane_id,
        "computation_identity": {
            "computation_id": computation_id,
            "tax_type": tax_type,
            "regime_type": regime_type,
            "tax_year": tax_year,
            "rule_version": rule_version,
            "input_hash": input_hash,
            "finalization_status": finalization_status,
            "finalized_at": finalized_at,
            "finalized_audit_event_id": finalized_audit_event_id,
        },
        "version_identity": {
            "historical_version_id": historical_version_id,
            "effective_start": _require_string(version_identity, "effective_start"),
            "effective_end": _require_string(version_identity, "effective_end"),
            "version_selection_basis": _require_string(version_identity, "version_selection_basis"),
            "source_anchor_ids": source_anchor_ids,
        },
        "taxpayer": {
            "taxpayer_kind": taxpayer_kind,
            "resident_status": resident_status,
            "classification_outcome": classification_outcome,
        },
        "liability_fields": {
            "assessable_income_kes": _require_string(liability_summary, "assessable_income_kes"),
            "chargeable_income_kes": _require_string(liability_summary, "chargeable_income_kes"),
            "gross_tax_kes": _require_string(liability_summary, "gross_tax_kes"),
            "total_reliefs_kes": _require_string(liability_summary, "total_reliefs_kes"),
            "creditable_withholding_kes": _require_string(
                liability_summary, "creditable_withholding_kes"
            ),
            "final_tax_excluded_income_kes": _require_string(
                liability_summary, "final_tax_excluded_income_kes"
            ),
            "installment_tax_credit_kes": _require_string(
                liability_summary, "installment_tax_credit_kes"
            ),
            "advance_tax_credit_kes": _require_string(liability_summary, "advance_tax_credit_kes"),
            "net_income_tax_due_kes": _require_string(liability_summary, "net_income_tax_due_kes"),
            "refund_due_kes": _require_string(liability_summary, "refund_due_kes"),
        },
        "domain_fields": {
            "employment": _build_domain_field_block(employment_domain),
            "investment": _build_domain_field_block(investment_domain),
            "deductions_and_exemptions": _build_domain_field_block(deductions_domain),
            "reliefs": _build_domain_field_block(reliefs_domain),
            "withholding": _build_domain_field_block(withholding_domain),
        },
        "form_fields": {
            "employment_income_kes": _money_or_zero(employment_domain.get("taxable_base_kes")),
            "employment_gross_tax_kes": _money_or_zero(employment_domain.get("gross_tax_kes")),
            "employment_final_tax_amount_kes": _money_or_zero(
                employment_domain.get("final_tax_amount_kes")
            ),
            "investment_income_kes": _money_or_zero(investment_domain.get("taxable_base_kes")),
            "investment_gross_tax_kes": _money_or_zero(investment_domain.get("gross_tax_kes")),
            "investment_final_tax_amount_kes": _money_or_zero(
                investment_domain.get("final_tax_amount_kes")
            ),
            "chargeable_income_kes": _require_string(liability_summary, "chargeable_income_kes"),
            "total_reliefs_kes": _require_string(liability_summary, "total_reliefs_kes"),
            "final_tax_excluded_income_kes": _require_string(
                liability_summary, "final_tax_excluded_income_kes"
            ),
            "net_income_tax_due_kes": _require_string(liability_summary, "net_income_tax_due_kes"),
            "refund_due_kes": _require_string(liability_summary, "refund_due_kes"),
        },
        "impact_fields": {
            "relief_impacts": relief_impacts,
            "deduction_impacts": deduction_impacts,
            "exemption_impacts": exemption_impacts,
        },
        "treatment_fields": {
            "withholding_treatments": withholding_treatments,
            "adjacent_regime_flags": adjacent_regime_flags,
        },
        "lineage": {
            "source_anchor_ids": source_anchor_ids,
            "applied_policy_ids": applied_policy_ids,
            "validation_focus_domains": validation_focus_domains,
            "replay_safe": _require_bool(traceability, "replay_safe"),
            "computation_status": _require_string(traceability, "computation_status"),
        },
        "unsupported_fields": [],
    }
    mapped_output["audit_evidence"] = build_income_tax_form_mapping_audit_evidence(mapped_output)
    return mapped_output


def _resolve_supported_lane(
    *,
    historical_version_id: str,
    resident_status: str,
    employment_domain: Mapping[str, object],
    investment_domain: Mapping[str, object],
    deductions_domain: Mapping[str, object],
    reliefs_domain: Mapping[str, object],
    withholding_domain: Mapping[str, object],
    liability_summary: Mapping[str, object],
    impact_summary: Mapping[str, object],
    treatment_decisions: Mapping[str, object],
    taxpayer_kind: str,
) -> str:
    if taxpayer_kind != "individual":
        raise IncomeTaxFormMappingError(
            reason="unsupported_taxpayer_kind",
            message="Form mapping supports governed individual income-tax outputs only.",
            details={"taxpayer_kind": taxpayer_kind},
        )

    employment_status = _require_string(employment_domain, "status")
    investment_status = _require_string(investment_domain, "status")
    deductions_status = _require_string(deductions_domain, "status")
    reliefs_status = _require_string(reliefs_domain, "status")
    withholding_status = _require_string(withholding_domain, "status")

    if employment_status != "computed":
        raise IncomeTaxFormMappingError(
            reason="unsupported_income_domain",
            message="Form mapping requires computed employment-domain output.",
            details={"employment_status": employment_status},
        )
    if deductions_status != "computed" or reliefs_status != "computed":
        raise IncomeTaxFormMappingError(
            reason="missing_required_domain_outcome",
            message="Supported income-tax lanes require computed deductions and relief outputs.",
            details={
                "deductions_status": deductions_status,
                "reliefs_status": reliefs_status,
            },
        )

    if resident_status == "non_resident":
        if _require_string(liability_summary, "total_reliefs_kes") != "0.00":
            raise IncomeTaxFormMappingError(
                reason="resident_status_relief_mismatch",
                message="Non-resident supported lanes cannot map resident-only relief totals.",
                details={"resident_status": resident_status},
            )
        if _list_of_objects(impact_summary, "relief_impacts"):
            raise IncomeTaxFormMappingError(
                reason="resident_status_relief_mismatch",
                message="Non-resident supported lanes cannot map resident-only relief impacts.",
                details={"resident_status": resident_status},
            )

    if investment_status == "not_applicable" and withholding_status == "not_applicable":
        if historical_version_id == "KIT-VER-20210101-A" and resident_status == "resident":
            return "resident_employment_income_2021_01_01"
        if historical_version_id == "KIT-VER-20210101-A" and resident_status == "non_resident":
            return "non_resident_employment_income_2021_01_01"
        if historical_version_id == "KIT-VER-20230701-A" and resident_status == "resident":
            return "resident_employment_income_2023_07_01"
        if historical_version_id == "KIT-VER-20230701-A" and resident_status == "non_resident":
            return "non_resident_employment_income_2023_07_01"

    if investment_status == "computed":
        if historical_version_id != "KIT-VER-20230701-A" or resident_status != "resident":
            raise IncomeTaxFormMappingError(
                reason="unsupported_mixed_income_lane",
                message="Mixed-income mapping supports the governed resident 2023 lane only.",
                details={
                    "historical_version_id": historical_version_id,
                    "resident_status": resident_status,
                },
            )
        if withholding_status != "computed":
            raise IncomeTaxFormMappingError(
                reason="missing_required_domain_outcome",
                message="Supported mixed-income mapping requires computed withholding treatment.",
                details={"withholding_status": withholding_status},
            )

        withholding_treatments = _list_of_objects(treatment_decisions, "withholding_treatments")
        if not withholding_treatments:
            raise IncomeTaxFormMappingError(
                reason="missing_required_treatment_decision",
                message="Supported mixed-income mapping requires withholding treatment decisions.",
            )
        for treatment in withholding_treatments:
            if _require_string(treatment, "treatment") != "final_tax":
                raise IncomeTaxFormMappingError(
                    reason="unsupported_mixed_income_treatment",
                    message=(
                        "Supported mixed-income mapping only accepts final-tax qualifying interest."
                    ),
                    details={"withholding_treatments": withholding_treatments},
                )

        excluded_income = _require_string(liability_summary, "final_tax_excluded_income_kes")
        investment_taxable_base = _money_or_zero(investment_domain.get("taxable_base_kes"))
        if excluded_income != investment_taxable_base:
            raise IncomeTaxFormMappingError(
                reason="mixed_income_liability_mismatch",
                message=(
                    "Supported mixed-income mapping requires final-tax excluded "
                    "income to equal the qualifying-interest base."
                ),
                details={
                    "final_tax_excluded_income_kes": excluded_income,
                    "investment_taxable_base_kes": investment_taxable_base,
                },
            )
        return "resident_employment_plus_qualifying_interest_2023_07_01"

    raise IncomeTaxFormMappingError(
        reason="unsupported_income_lane",
        message="Form mapping does not support this income-tax lane.",
        details={
            "historical_version_id": historical_version_id,
            "resident_status": resident_status,
            "investment_status": investment_status,
            "withholding_status": withholding_status,
        },
    )


def _build_domain_field_block(domain: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": _require_string(domain, "status"),
        "taxable_base_kes": _money_or_zero(domain.get("taxable_base_kes")),
        "gross_tax_kes": _money_or_zero(domain.get("gross_tax_kes")),
        "final_tax_amount_kes": _money_or_zero(domain.get("final_tax_amount_kes")),
        "creditable_amount_kes": _money_or_zero(domain.get("creditable_amount_kes")),
        "decision_refs": _list_of_strings(domain, "decision_refs"),
    }


def _reject_unsupported_domain_activity(domain_outcomes: Mapping[str, object]) -> None:
    expected_not_applicable_domains = (
        "business",
        "rental",
        "advance_tax",
        "installment_tax",
        "adjacent_regime_interactions",
        "prescribed_rate_resolution",
    )
    for domain_id in expected_not_applicable_domains:
        domain = _require_object(domain_outcomes, domain_id)
        if _require_string(domain, "status") != "not_applicable":
            raise IncomeTaxFormMappingError(
                reason="unsupported_income_lane",
                message=(
                    "Form mapping does not support computed activity in this income-tax domain."
                ),
                details={"domain_id": domain_id, "status": _require_string(domain, "status")},
            )


def _money_or_zero(value: object) -> str:
    if value is None:
        return "0.00"
    if isinstance(value, str) and value.strip():
        return value
    raise IncomeTaxFormMappingError(
        reason="invalid_money_field",
        message="Expected money field to be a non-empty string or null.",
    )


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxFormMappingError(
            reason=reason,
            message="Expected JSON object input for deterministic form mapping.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxFormMappingError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxFormMappingError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxFormMappingError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_bool(source: Mapping[str, object], field_name: str) -> bool:
    value = source.get(field_name)
    if not isinstance(value, bool):
        raise IncomeTaxFormMappingError(
            reason="missing_required_field",
            message=f"Required boolean field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_list(source: Mapping[str, object], field_name: str) -> list[object]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxFormMappingError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return cast(list[object], value)


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    values = _require_list(source, field_name)
    strings: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise IncomeTaxFormMappingError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(value)
    return strings


def _list_of_objects(source: Mapping[str, object], field_name: str) -> list[dict[str, object]]:
    values = _require_list(source, field_name)
    objects: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise IncomeTaxFormMappingError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only objects.",
                details={"field_name": field_name},
            )
        typed_value = cast(Mapping[object, object], value)
        objects.append({str(key): typed_value[key] for key in typed_value})
    return objects
