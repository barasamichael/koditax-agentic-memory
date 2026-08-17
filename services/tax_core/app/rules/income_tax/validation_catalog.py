"""Implement governed validation checks for supported income-tax lanes."""

from __future__ import annotations

from typing import cast
from typing import Literal
from decimal import Decimal
from dataclasses import dataclass
from collections.abc import Mapping

from services.tax_core.app.engine.execution_contract import ValidationFinding
from services.tax_core.app.engine.execution_contract import PersistedValidationSource

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class _SupportedLaneSpec:
    lane_id: str
    historical_version_id: str
    tax_year: int
    rule_version: str
    effective_start: str
    effective_end: str
    resident_status: str
    income_category_signature: str
    relief_amount: Decimal
    mixed_income: bool


_SUPPORTED_LANES: tuple[_SupportedLaneSpec, ...] = (
    _SupportedLaneSpec(
        lane_id="resident_employment_2021_01_01",
        historical_version_id="KIT-VER-20210101-A",
        tax_year=2021,
        rule_version="v1",
        effective_start="2021-01-01",
        effective_end="2021-06-30",
        resident_status="resident",
        income_category_signature="employment",
        relief_amount=Decimal("28800.00"),
        mixed_income=False,
    ),
    _SupportedLaneSpec(
        lane_id="non_resident_employment_2021_01_01",
        historical_version_id="KIT-VER-20210101-A",
        tax_year=2021,
        rule_version="v1",
        effective_start="2021-01-01",
        effective_end="2021-06-30",
        resident_status="non_resident",
        income_category_signature="employment",
        relief_amount=ZERO,
        mixed_income=False,
    ),
    _SupportedLaneSpec(
        lane_id="resident_employment_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        rule_version="v1",
        effective_start="2023-07-01",
        effective_end="2023-08-31",
        resident_status="resident",
        income_category_signature="employment",
        relief_amount=Decimal("28800.00"),
        mixed_income=False,
    ),
    _SupportedLaneSpec(
        lane_id="resident_employment_plus_qualifying_interest_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        rule_version="v1",
        effective_start="2023-07-01",
        effective_end="2023-08-31",
        resident_status="resident",
        income_category_signature="employment+investment",
        relief_amount=Decimal("28800.00"),
        mixed_income=True,
    ),
    _SupportedLaneSpec(
        lane_id="non_resident_employment_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        rule_version="v1",
        effective_start="2023-07-01",
        effective_end="2023-08-31",
        resident_status="non_resident",
        income_category_signature="employment",
        relief_amount=ZERO,
        mixed_income=False,
    ),
)


def derive_income_tax_validation_findings(
    persisted_source: PersistedValidationSource,
) -> list[ValidationFinding]:
    """Return deterministic income-tax validation findings for supported lanes."""

    if persisted_source.tax_type != "income_tax" or persisted_source.regime_type != "income_tax":
        return [_unsupported_scope_finding(persisted_source, "non_income_tax_computation")]

    result_payload = persisted_source.stored_result_payload
    if "version_identity" not in result_payload:
        return [_unsupported_scope_finding(persisted_source, "non_governed_income_tax_result")]

    parse_result = _parse_supported_context(persisted_source)
    if isinstance(parse_result, ValidationFinding):
        return [parse_result]

    lane_spec, payload = parse_result
    findings = [
        _build_finding(
            code="income_tax_supported_lane_detected",
            severity="info",
            domain_id="ITD-GOV-SCOPE",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message="Validation matched a supported governed income-tax lane.",
            details={
                "income_category_signature": lane_spec.income_category_signature,
                "resident_status": lane_spec.resident_status,
            },
        ),
    ]
    findings.append(_validate_version_identity(persisted_source, lane_spec, payload))
    findings.append(_validate_relief_treatment(persisted_source, lane_spec, payload))
    findings.append(_validate_liability_summary(persisted_source, lane_spec, payload))
    if lane_spec.mixed_income:
        findings.append(_validate_mixed_income_treatment(persisted_source, lane_spec, payload))
    return findings


@dataclass(frozen=True)
class _ParsedPayload:
    version_identity: dict[str, object]
    taxpayer_outcome: dict[str, object]
    domain_outcomes: dict[str, object]
    liability_summary: dict[str, object]
    impact_summary: dict[str, object]
    treatment_decisions: dict[str, object]


def _parse_supported_context(
    persisted_source: PersistedValidationSource,
) -> tuple[_SupportedLaneSpec, _ParsedPayload] | ValidationFinding:
    payload = persisted_source.stored_result_payload
    try:
        version_identity = _require_mapping(payload, "version_identity")
        taxpayer_outcome = _require_mapping(payload, "taxpayer_outcome")
        domain_outcomes = _require_mapping(payload, "domain_outcomes")
        liability_summary = _require_mapping(payload, "liability_summary")
        impact_summary = _require_mapping(payload, "impact_summary")
        treatment_decisions = _require_mapping(payload, "treatment_decisions")
        historical_version_id = _require_string(version_identity, "historical_version_id")
        resident_status = _require_string(taxpayer_outcome, "resident_status")
        investment_domain = _require_mapping(domain_outcomes, "investment")
        investment_status = _require_string(investment_domain, "status")
    except _PayloadShapeError as error:
        return _build_shape_error_finding(persisted_source, str(error))

    income_category_signature = (
        "employment+investment" if investment_status == "computed" else "employment"
    )
    lane_spec = _find_lane_spec(
        historical_version_id=historical_version_id,
        resident_status=resident_status,
        income_category_signature=income_category_signature,
    )
    if lane_spec is None:
        return _unsupported_scope_finding(
            persisted_source,
            "unsupported_governed_income_tax_lane",
            historical_version_id=historical_version_id,
            resident_status=resident_status,
            income_category_signature=income_category_signature,
        )

    return (
        lane_spec,
        _ParsedPayload(
            version_identity=version_identity,
            taxpayer_outcome=taxpayer_outcome,
            domain_outcomes=domain_outcomes,
            liability_summary=liability_summary,
            impact_summary=impact_summary,
            treatment_decisions=treatment_decisions,
        ),
    )


def _validate_version_identity(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        tax_year = _require_int(payload.version_identity, "tax_year")
        rule_version = _require_string(payload.version_identity, "rule_version")
        effective_start = _require_string(payload.version_identity, "effective_start")
        effective_end = _require_string(payload.version_identity, "effective_end")
        historical_version_id = _require_string(payload.version_identity, "historical_version_id")
        resident_status = _require_string(payload.taxpayer_outcome, "resident_status")
        classification_outcome = _require_string(
            payload.taxpayer_outcome,
            "classification_outcome",
        )
    except _PayloadShapeError as error:
        return _build_shape_error_finding(persisted_source, str(error))

    is_consistent = (
        tax_year == persisted_source.tax_year
        and tax_year == lane_spec.tax_year
        and rule_version == persisted_source.rule_version
        and rule_version == lane_spec.rule_version
        and historical_version_id == lane_spec.historical_version_id
        and effective_start == lane_spec.effective_start
        and effective_end == lane_spec.effective_end
        and resident_status == lane_spec.resident_status
        and classification_outcome == "fully_classified"
    )
    if is_consistent:
        return _build_finding(
            code="income_tax_version_binding_consistent",
            severity="info",
            domain_id="ITD-GOV-VERSION",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message=(
                "Income-tax version identity and lane classification are internally consistent."
            ),
            details={
                "historical_version_id": historical_version_id,
                "effective_start": effective_start,
                "effective_end": effective_end,
            },
        )
    return _build_finding(
        code="income_tax_version_binding_inconsistent",
        severity="error",
        domain_id="ITD-GOV-VERSION",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Income-tax version identity does not match the persisted governed lane.",
        details={
            "expected_tax_year": lane_spec.tax_year,
            "actual_tax_year": tax_year,
            "expected_rule_version": lane_spec.rule_version,
            "actual_rule_version": rule_version,
            "expected_historical_version_id": lane_spec.historical_version_id,
            "actual_historical_version_id": historical_version_id,
            "expected_resident_status": lane_spec.resident_status,
            "actual_resident_status": resident_status,
        },
    )


def _validate_relief_treatment(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        relief_domain = _require_mapping(payload.domain_outcomes, "reliefs")
        relief_amount = _require_money(relief_domain, "creditable_amount_kes")
        total_reliefs = _require_money(payload.liability_summary, "total_reliefs_kes")
        relief_impacts = _require_list(payload.impact_summary, "relief_impacts")
    except _PayloadShapeError as error:
        return _build_shape_error_finding(persisted_source, str(error))

    if lane_spec.relief_amount == ZERO:
        is_consistent = relief_amount == ZERO and total_reliefs == ZERO and len(relief_impacts) == 0
    else:
        is_consistent = (
            relief_amount == lane_spec.relief_amount
            and total_reliefs == lane_spec.relief_amount
            and len(relief_impacts) == 1
            and _is_personal_relief_impact(relief_impacts[0], lane_spec.relief_amount)
        )

    if is_consistent:
        return _build_finding(
            code="income_tax_relief_treatment_consistent",
            severity="info",
            domain_id="ITD-CORE-RELIEFS",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message="Income-tax relief treatment is consistent with the governed taxpayer lane.",
            details={"expected_relief_amount_kes": _format_decimal(lane_spec.relief_amount)},
        )
    return _build_finding(
        code="income_tax_relief_treatment_inconsistent",
        severity="error",
        domain_id="ITD-CORE-RELIEFS",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Income-tax relief treatment does not match the governed taxpayer lane.",
        details={
            "expected_relief_amount_kes": _format_decimal(lane_spec.relief_amount),
            "actual_relief_amount_kes": _format_decimal(relief_amount),
            "actual_total_reliefs_kes": _format_decimal(total_reliefs),
            "relief_impact_count": len(relief_impacts),
        },
    )


def _validate_liability_summary(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        employment = _require_mapping(payload.domain_outcomes, "employment")
        employment_taxable_base = _require_money(employment, "taxable_base_kes")
        employment_gross_tax = _require_money(employment, "gross_tax_kes")
        deductions = _require_mapping(payload.domain_outcomes, "deductions_and_exemptions")
        chargeable_income = _require_money(deductions, "taxable_base_kes")
        assessable_income = _require_money(payload.liability_summary, "assessable_income_kes")
        liability_chargeable = _require_money(payload.liability_summary, "chargeable_income_kes")
        gross_tax = _require_money(payload.liability_summary, "gross_tax_kes")
        total_reliefs = _require_money(payload.liability_summary, "total_reliefs_kes")
        creditable_withholding = _require_money(
            payload.liability_summary,
            "creditable_withholding_kes",
        )
        installment_credit = _require_money(
            payload.liability_summary,
            "installment_tax_credit_kes",
        )
        advance_credit = _require_money(payload.liability_summary, "advance_tax_credit_kes")
        net_tax_due = _require_money(payload.liability_summary, "net_income_tax_due_kes")
        refund_due = _require_money(payload.liability_summary, "refund_due_kes")
        final_tax_excluded_income = _require_money(
            payload.liability_summary,
            "final_tax_excluded_income_kes",
        )
    except _PayloadShapeError as error:
        return _build_shape_error_finding(persisted_source, str(error))

    investment_taxable_base = ZERO
    investment_gross_tax = ZERO
    if lane_spec.mixed_income:
        try:
            investment = _require_mapping(payload.domain_outcomes, "investment")
            investment_taxable_base = _require_money(investment, "taxable_base_kes")
            investment_gross_tax = _require_money(investment, "gross_tax_kes")
        except _PayloadShapeError as error:
            return _build_shape_error_finding(persisted_source, str(error))

    expected_assessable_income = employment_taxable_base + investment_taxable_base
    expected_gross_tax = employment_gross_tax + investment_gross_tax
    expected_net_tax_due = (
        expected_gross_tax
        - total_reliefs
        - creditable_withholding
        - installment_credit
        - advance_credit
    )
    if expected_net_tax_due < ZERO:
        expected_net_tax_due = ZERO
    expected_final_tax_excluded = investment_taxable_base if lane_spec.mixed_income else ZERO

    is_consistent = (
        assessable_income == expected_assessable_income
        and chargeable_income == employment_taxable_base
        and liability_chargeable == chargeable_income
        and gross_tax == expected_gross_tax
        and net_tax_due == expected_net_tax_due
        and refund_due == ZERO
        and final_tax_excluded_income == expected_final_tax_excluded
    )

    if is_consistent:
        return _build_finding(
            code="income_tax_liability_summary_consistent",
            severity="info",
            domain_id="ITD-CORE-LIABILITY",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message="Income-tax liability summary is internally consistent for the governed lane.",
            details={
                "expected_assessable_income_kes": _format_decimal(expected_assessable_income),
                "expected_gross_tax_kes": _format_decimal(expected_gross_tax),
                "expected_net_income_tax_due_kes": _format_decimal(expected_net_tax_due),
            },
        )
    return _build_finding(
        code="income_tax_liability_summary_inconsistent",
        severity="error",
        domain_id="ITD-CORE-LIABILITY",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Income-tax liability summary is inconsistent with the governed domain outcomes.",
        details={
            "expected_assessable_income_kes": _format_decimal(expected_assessable_income),
            "actual_assessable_income_kes": _format_decimal(assessable_income),
            "expected_gross_tax_kes": _format_decimal(expected_gross_tax),
            "actual_gross_tax_kes": _format_decimal(gross_tax),
            "expected_net_income_tax_due_kes": _format_decimal(expected_net_tax_due),
            "actual_net_income_tax_due_kes": _format_decimal(net_tax_due),
        },
    )


def _validate_mixed_income_treatment(
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    payload: _ParsedPayload,
) -> ValidationFinding:
    try:
        investment = _require_mapping(payload.domain_outcomes, "investment")
        withholding = _require_mapping(payload.domain_outcomes, "withholding")
        investment_status = _require_string(investment, "status")
        withholding_status = _require_string(withholding, "status")
        investment_taxable_base = _require_money(investment, "taxable_base_kes")
        investment_final_tax = _require_money(investment, "final_tax_amount_kes")
        withholding_creditable = _require_money(withholding, "creditable_amount_kes")
        withholding_final_tax = _require_money(withholding, "final_tax_amount_kes")
        final_tax_excluded_income = _require_money(
            payload.liability_summary,
            "final_tax_excluded_income_kes",
        )
        withholding_treatments = _require_list(
            payload.treatment_decisions,
            "withholding_treatments",
        )
    except _PayloadShapeError as error:
        return _build_shape_error_finding(persisted_source, str(error))

    all_final_tax = all(_is_final_tax_treatment(item) for item in withholding_treatments)
    is_consistent = (
        investment_status == "computed"
        and withholding_status == "computed"
        and withholding_creditable == ZERO
        and investment_final_tax == withholding_final_tax
        and final_tax_excluded_income == investment_taxable_base
        and len(withholding_treatments) > 0
        and all_final_tax
    )

    if is_consistent:
        return _build_finding(
            code="income_tax_mixed_income_treatment_consistent",
            severity="info",
            domain_id="ITD-CORE-WHT",
            persisted_source=persisted_source,
            lane_spec=lane_spec,
            message="Mixed-income final-tax treatment is consistent with the governed lane.",
            details={
                "final_tax_excluded_income_kes": _format_decimal(final_tax_excluded_income),
                "withholding_treatment_count": len(withholding_treatments),
            },
        )
    return _build_finding(
        code="income_tax_mixed_income_treatment_inconsistent",
        severity="error",
        domain_id="ITD-CORE-WHT",
        persisted_source=persisted_source,
        lane_spec=lane_spec,
        message="Mixed-income final-tax treatment does not match the governed lane.",
        details={
            "investment_final_tax_amount_kes": _format_decimal(investment_final_tax),
            "withholding_final_tax_amount_kes": _format_decimal(withholding_final_tax),
            "final_tax_excluded_income_kes": _format_decimal(final_tax_excluded_income),
            "withholding_treatment_count": len(withholding_treatments),
        },
    )


def _build_shape_error_finding(
    persisted_source: PersistedValidationSource,
    message: str,
) -> ValidationFinding:
    return ValidationFinding(
        code="income_tax_result_payload_shape_invalid",
        severity="error",
        message="Income-tax result payload is missing governed validation fields.",
        details={
            "domain_id": "ITD-GOV-SCOPE",
            "tax_type": persisted_source.tax_type,
            "regime_type": persisted_source.regime_type,
            "tax_year": persisted_source.tax_year,
            "rule_version": persisted_source.rule_version,
            "input_hash": persisted_source.input_hash,
            "shape_error": message,
        },
    )


def _unsupported_scope_finding(
    persisted_source: PersistedValidationSource,
    reason: str,
    historical_version_id: str | None = None,
    resident_status: str | None = None,
    income_category_signature: str | None = None,
) -> ValidationFinding:
    return ValidationFinding(
        code="income_tax_validation_scope_unsupported",
        severity="error",
        message="Income-tax validation catalog does not support this persisted computation lane.",
        details={
            "domain_id": "ITD-GOV-SCOPE",
            "tax_type": persisted_source.tax_type,
            "regime_type": persisted_source.regime_type,
            "tax_year": persisted_source.tax_year,
            "rule_version": persisted_source.rule_version,
            "input_hash": persisted_source.input_hash,
            "reason": reason,
            "historical_version_id": historical_version_id,
            "resident_status": resident_status,
            "income_category_signature": income_category_signature,
        },
    )


def _build_finding(
    code: str,
    severity: Literal["info", "warning", "error"],
    domain_id: str,
    persisted_source: PersistedValidationSource,
    lane_spec: _SupportedLaneSpec,
    message: str,
    details: dict[str, object] | None = None,
) -> ValidationFinding:
    finding_details: dict[str, object] = {
        "domain_id": domain_id,
        "lane_id": lane_spec.lane_id,
        "historical_version_id": lane_spec.historical_version_id,
        "tax_year": persisted_source.tax_year,
        "rule_version": persisted_source.rule_version,
        "input_hash": persisted_source.input_hash,
    }
    if details is not None:
        finding_details.update(details)
    return ValidationFinding(
        code=code,
        severity=severity,
        message=message,
        details=finding_details,
    )


def _find_lane_spec(
    historical_version_id: str,
    resident_status: str,
    income_category_signature: str,
) -> _SupportedLaneSpec | None:
    for lane_spec in _SUPPORTED_LANES:
        if (
            lane_spec.historical_version_id == historical_version_id
            and lane_spec.resident_status == resident_status
            and lane_spec.income_category_signature == income_category_signature
        ):
            return lane_spec
    return None


class _PayloadShapeError(ValueError):
    pass


def _require_mapping(container: Mapping[str, object], key: str) -> dict[str, object]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        raise _PayloadShapeError(f"{key} must be a JSON object")
    return dict(cast(Mapping[str, object], value))


def _require_list(container: Mapping[str, object], key: str) -> list[object]:
    value = container.get(key)
    if not isinstance(value, list):
        raise _PayloadShapeError(f"{key} must be a JSON array")
    return cast(list[object], value)


def _require_string(container: Mapping[str, object], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise _PayloadShapeError(f"{key} must be a non-empty string")
    return value


def _require_int(container: Mapping[str, object], key: str) -> int:
    value = container.get(key)
    if not isinstance(value, int):
        raise _PayloadShapeError(f"{key} must be an integer")
    return value


def _require_money(container: Mapping[str, object], key: str) -> Decimal:
    value = container.get(key)
    if not isinstance(value, str):
        raise _PayloadShapeError(f"{key} must be a money string")
    try:
        return Decimal(value)
    except Exception as error:  # pragma: no cover - Decimal gives deterministic types here
        raise _PayloadShapeError(f"{key} must be a money string") from error


def _is_personal_relief_impact(item: object, expected_amount: Decimal) -> bool:
    if not isinstance(item, Mapping):
        return False
    typed_item = cast(Mapping[str, object], item)
    return (
        typed_item.get("impact_type") == "personal_relief"
        and typed_item.get("status") == "applied"
        and typed_item.get("impact_amount_kes") == _format_decimal(expected_amount)
    )


def _format_decimal(value: Decimal) -> str:
    return format(value, ".2f")


def _is_final_tax_treatment(item: object) -> bool:
    if not isinstance(item, Mapping):
        return False
    typed_item = cast(Mapping[str, object], item)
    return (
        typed_item.get("treatment") == "final_tax"
        and typed_item.get("decision_ref") == "MIX-REMP-QINT-20230701-010"
    )
