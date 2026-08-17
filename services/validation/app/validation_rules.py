"""Deterministic governed rule evaluation for the validation service."""

from __future__ import annotations

import re
from typing import cast
from typing import Final
from decimal import Decimal
from decimal import InvalidOperation
from datetime import date
from dataclasses import dataclass
from collections.abc import Mapping

from services.validation.app.validation_outcomes import build_summary
from services.validation.app.validation_outcomes import ValidationMode
from services.validation.app.validation_outcomes import ValidationIssue
from services.validation.app.validation_outcomes import ValidationStatus
from services.validation.app.validation_outcomes import ValidationRuleResult
from services.validation.app.validation_outcomes import GovernedValidationEnvelope
from services.validation.app.validation_outcomes import build_governed_validation_envelope

KRA_PIN_PATTERN = re.compile(r"^[A-Z]\d{9}[A-Z]$")
SUPPORTED_TAX_DOMAINS: frozenset[str] = frozenset({"income_tax", "health_contribution"})
SUPPORTED_MODES: frozenset[str] = frozenset(
    {"draft", "pre_submission", "post_submission_integrity"}
)
SUPPORTED_MODES_BY_DOMAIN: Final[Mapping[str, frozenset[str]]] = {
    "income_tax": SUPPORTED_MODES,
    "health_contribution": frozenset({"draft", "pre_submission"}),
}
IMPLEMENTATION_READY_WINDOW = "implementation_ready"


@dataclass(frozen=True)
class _HealthLaneSpec:
    regime_identifier: str
    resolved_domain_path: str
    historical_version_id: str
    effective_start: str
    effective_end: str


_SUPPORTED_HEALTH_LANES: Final[tuple[_HealthLaneSpec, ...]] = (
    _HealthLaneSpec(
        regime_identifier="nhif_legacy",
        resolved_domain_path="nhif_legacy",
        historical_version_id="HCH-VER-20100716-A",
        effective_start="2010-07-16",
        effective_end="2014-12-07",
    ),
    _HealthLaneSpec(
        regime_identifier="nhif_legacy",
        resolved_domain_path="nhif_legacy",
        historical_version_id="HCH-VER-20150401-A",
        effective_start="2015-04-01",
        effective_end="2021-03-29",
    ),
    _HealthLaneSpec(
        regime_identifier="nhif_legacy",
        resolved_domain_path="nhif_legacy",
        historical_version_id="HCH-VER-20210528-A",
        effective_start="2021-05-28",
        effective_end="2022-12-30",
    ),
    _HealthLaneSpec(
        regime_identifier="nhif_legacy",
        resolved_domain_path="nhif_legacy",
        historical_version_id="HCH-VER-20221231-REG",
        effective_start="2022-12-31",
        effective_end="2023-11-21",
    ),
    _HealthLaneSpec(
        regime_identifier="sha_shif",
        resolved_domain_path="sha_shif_salaried",
        historical_version_id="HCH-VER-20241001-A",
        effective_start="2024-10-01",
        effective_end="2025-02-27",
    ),
    _HealthLaneSpec(
        regime_identifier="sha_shif",
        resolved_domain_path="sha_shif_non_salaried",
        historical_version_id="HCH-VER-20241001-A",
        effective_start="2024-10-01",
        effective_end="2025-02-27",
    ),
    _HealthLaneSpec(
        regime_identifier="sha_shif",
        resolved_domain_path="sha_shif_salaried",
        historical_version_id="HCH-VER-20250228-PIT",
        effective_start="2025-02-28",
        effective_end="open",
    ),
    _HealthLaneSpec(
        regime_identifier="sha_shif",
        resolved_domain_path="sha_shif_non_salaried",
        historical_version_id="HCH-VER-20250228-PIT",
        effective_start="2025-02-28",
        effective_end="open",
    ),
)

_HEALTH_WINDOW_STATUS_BY_ID: Final[dict[str, str]] = {
    "HCH-VER-19990215-A": "governed_boundary_only",
    "HCH-VER-20031205-A": "partially_specified",
    "HCH-VER-20100716-A": IMPLEMENTATION_READY_WINDOW,
    "HCH-VER-20141208-A": "governed_boundary_only",
    "HCH-VER-20150401-A": IMPLEMENTATION_READY_WINDOW,
    "HCH-VER-20210330-A": "governed_boundary_only",
    "HCH-VER-20210528-A": IMPLEMENTATION_READY_WINDOW,
    "HCH-VER-20221231-ACT": "governed_boundary_only",
    "HCH-VER-20221231-REG": IMPLEMENTATION_READY_WINDOW,
    "HCH-VER-20231122-REPEAL": "governed_boundary_only",
    "HCH-VER-20231122-SHIACT": "governed_boundary_only",
    "HCH-VER-20240308-A": "governed_boundary_only",
    "HCH-VER-20240701-A": "governed_boundary_only",
    "HCH-VER-20240920-AMD": "governed_boundary_only",
    "HCH-VER-20240920-PIT": "governed_boundary_only",
    "HCH-VER-20241001-A": IMPLEMENTATION_READY_WINDOW,
    "HCH-VER-20250228-AMD": "governed_boundary_only",
    "HCH-VER-20250228-PIT": IMPLEMENTATION_READY_WINDOW,
}


@dataclass(frozen=True)
class ValidationRequestModel:
    """Represent one parsed governed validation request."""

    return_id: str
    tax_domain: str
    mode: ValidationMode
    fields: Mapping[str, object]


@dataclass(frozen=True)
class ValidationEvaluation:
    """Represent one deterministic validation evaluation."""

    validation_status: ValidationStatus
    issues: tuple[ValidationIssue, ...]
    rule_results: tuple[ValidationRuleResult, ...]

    def summary_dict(self) -> dict[str, int]:
        return build_summary(self.issues).to_dict()


def supported_modes_for_domain(tax_domain: str) -> frozenset[str]:
    """Return the deterministic supported mode set for one tax domain."""

    return SUPPORTED_MODES_BY_DOMAIN.get(tax_domain, frozenset())


def evaluate_validation_request(request: ValidationRequestModel) -> ValidationEvaluation:
    """Evaluate the supported validation request deterministically."""

    if request.tax_domain == "income_tax":
        return _evaluate_income_tax_request(request)
    if request.tax_domain == "health_contribution":
        return _evaluate_health_contribution_request(request)
    raise AssertionError(f"Unsupported tax domain reached evaluator: {request.tax_domain}")


def evaluate_forms_workflow_validation(
    *,
    tax_domain: str,
    finalized_output: Mapping[str, object],
) -> GovernedValidationEnvelope:
    """Evaluate deterministic governed validation for forms workflow inputs."""

    source = _as_object(finalized_output)
    issues: list[ValidationIssue] = []
    rule_results: list[ValidationRuleResult] = []
    if tax_domain == "income_tax":
        _evaluate_income_tax_forms_payload(source=source, issues=issues, rule_results=rule_results)
    elif tax_domain == "health_contribution":
        _evaluate_health_forms_payload(source=source, issues=issues, rule_results=rule_results)
    else:
        raise AssertionError(f"Unsupported forms validation domain: {tax_domain}")
    return _build_workflow_envelope(
        workflow="forms_pre_generation",
        tax_domain=tax_domain,
        issues=issues,
        rule_results=rule_results,
    )


def evaluate_report_workflow_validation(
    *,
    tax_domain: str,
    payload: Mapping[str, object],
) -> GovernedValidationEnvelope:
    """Evaluate deterministic governed validation for report-generation requests."""

    source = _as_object(payload)
    issues: list[ValidationIssue] = []
    rule_results: list[ValidationRuleResult] = []
    _evaluate_report_request_payload(
        tax_domain=tax_domain,
        source=source,
        issues=issues,
        rule_results=rule_results,
    )
    return _build_workflow_envelope(
        workflow="reports_generation",
        tax_domain=tax_domain,
        issues=issues,
        rule_results=rule_results,
    )


def evaluate_orchestration_workflow_validation(
    *,
    target_service: str,
    tax_domain: str,
    result_payload: Mapping[str, object],
) -> GovernedValidationEnvelope | None:
    """Evaluate deterministic validation visibility for orchestration adapter payloads."""

    source = _as_object(result_payload)
    issues: list[ValidationIssue] = []
    rule_results: list[ValidationRuleResult] = []
    if target_service == "forms":
        if tax_domain == "income_tax":
            _evaluate_income_tax_form_result_payload(
                source=source,
                issues=issues,
                rule_results=rule_results,
            )
        elif tax_domain == "health_contribution":
            _evaluate_health_form_result_payload(
                source=source,
                issues=issues,
                rule_results=rule_results,
            )
        else:
            return None
        workflow = "orchestration_forms_result"
    elif target_service == "reports":
        _evaluate_report_result_payload(
            tax_domain=tax_domain,
            source=source,
            issues=issues,
            rule_results=rule_results,
        )
        workflow = "orchestration_reports_result"
    else:
        return None
    return _build_workflow_envelope(
        workflow=workflow,
        tax_domain=tax_domain,
        issues=issues,
        rule_results=rule_results,
    )


def _evaluate_income_tax_request(request: ValidationRequestModel) -> ValidationEvaluation:
    issues: list[ValidationIssue] = []
    rule_results: list[ValidationRuleResult] = []

    kra_pin = request.fields.get("kra_pin")
    if kra_pin is None or (isinstance(kra_pin, str) and kra_pin.strip() == ""):
        severity = "WARNING" if request.mode == "draft" else "ERROR"
        issue = ValidationIssue(
            severity=severity,
            code="missing_kra_pin",
            message="KRA PIN is required.",
            field="kra_pin",
        )
        issues.append(issue)
        rule_results.append(
            ValidationRuleResult(
                rule_code="kra_pin_presence",
                outcome="failed",
                severity=severity,
                message=issue.message,
                field=issue.field,
                linked_issue_codes=(issue.code,),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="kra_pin_presence",
                outcome="passed",
                severity="INFO",
                message="KRA PIN presence check passed.",
                field="kra_pin",
                linked_issue_codes=(),
            )
        )

    if kra_pin is None or (isinstance(kra_pin, str) and kra_pin.strip() == ""):
        rule_results.append(
            ValidationRuleResult(
                rule_code="kra_pin_format",
                outcome="not_applicable",
                severity="INFO",
                message="KRA PIN format check skipped because no KRA PIN was provided.",
                field="kra_pin",
                linked_issue_codes=(),
            )
        )
    elif not isinstance(kra_pin, str) or KRA_PIN_PATTERN.fullmatch(kra_pin) is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="invalid_kra_pin_format",
            message="KRA PIN format is invalid.",
            field="kra_pin",
        )
        issues.append(issue)
        rule_results.append(
            ValidationRuleResult(
                rule_code="kra_pin_format",
                outcome="failed",
                severity="ERROR",
                message=issue.message,
                field=issue.field,
                linked_issue_codes=(issue.code,),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="kra_pin_format",
                outcome="passed",
                severity="INFO",
                message="KRA PIN format check passed.",
                field="kra_pin",
                linked_issue_codes=(),
            )
        )

    start_date_raw = request.fields.get("period_start")
    end_date_raw = request.fields.get("period_end")
    start_date, start_error = _parse_optional_date(start_date_raw, "period_start")
    end_date, end_error = _parse_optional_date(end_date_raw, "period_end")
    if start_error is not None:
        issues.append(start_error)
    if end_error is not None:
        issues.append(end_error)

    date_issue_codes: list[str] = []
    for issue in (start_error, end_error):
        if issue is not None:
            date_issue_codes.append(issue.code)

    if start_error is None and end_error is None:
        if (start_date is None) != (end_date is None):
            issue = ValidationIssue(
                severity="ERROR",
                code="invalid_date_range",
                message="Both period_start and period_end are required for date-range checks.",
                field="period_start",
            )
            issues.append(issue)
            date_issue_codes.append(issue.code)
        elif start_date is not None and end_date is not None and end_date < start_date:
            issue = ValidationIssue(
                severity="ERROR",
                code="invalid_date_range",
                message="period_end cannot be earlier than period_start.",
                field="period_end",
            )
            issues.append(issue)
            date_issue_codes.append(issue.code)

    if date_issue_codes:
        rule_results.append(
            ValidationRuleResult(
                rule_code="period_range_consistency",
                outcome="failed",
                severity="ERROR",
                message="Period range validation failed.",
                field="period_start",
                linked_issue_codes=tuple(date_issue_codes),
            )
        )
    elif start_date is None and end_date is None:
        rule_results.append(
            ValidationRuleResult(
                rule_code="period_range_consistency",
                outcome="not_applicable",
                severity="INFO",
                message="Period range validation skipped because no date range was provided.",
                field="period_start",
                linked_issue_codes=(),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="period_range_consistency",
                outcome="passed",
                severity="INFO",
                message="Period range validation passed.",
                field="period_start",
                linked_issue_codes=(),
            )
        )

    amount_total = request.fields.get("amount_total")
    if amount_total is None:
        rule_results.append(
            ValidationRuleResult(
                rule_code="amount_total_precision",
                outcome="not_applicable",
                severity="INFO",
                message="Amount precision check skipped because amount_total was not provided.",
                field="amount_total",
                linked_issue_codes=(),
            )
        )
    elif not _has_two_decimal_precision(amount_total):
        issue = ValidationIssue(
            severity="ERROR",
            code="invalid_amount_precision",
            message="amount_total must be numeric with up to 2 decimal places.",
            field="amount_total",
        )
        issues.append(issue)
        rule_results.append(
            ValidationRuleResult(
                rule_code="amount_total_precision",
                outcome="failed",
                severity="ERROR",
                message=issue.message,
                field=issue.field,
                linked_issue_codes=(issue.code,),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="amount_total_precision",
                outcome="passed",
                severity="INFO",
                message="Amount precision validation passed.",
                field="amount_total",
                linked_issue_codes=(),
            )
        )

    return _finalize_evaluation(issues, rule_results)


def _evaluate_health_contribution_request(
    request: ValidationRequestModel,
) -> ValidationEvaluation:
    issues: list[ValidationIssue] = []
    rule_results: list[ValidationRuleResult] = []

    regime_identifier = _optional_non_empty_string(request.fields.get("regime_identifier"))
    resolved_domain_path = _optional_non_empty_string(request.fields.get("resolved_domain_path"))
    historical_version_id = _optional_non_empty_string(request.fields.get("historical_version_id"))
    primary_effective_date_raw = request.fields.get("primary_effective_date")
    contribution_basis_kes = request.fields.get("contribution_basis_kes")
    total_contribution_kes = request.fields.get("total_contribution_kes")

    lane_issue_codes: list[str] = []
    if regime_identifier is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="missing_health_regime_identifier",
            message="Health contribution regime_identifier is required.",
            field="regime_identifier",
        )
        issues.append(issue)
        lane_issue_codes.append(issue.code)
    if resolved_domain_path is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="missing_health_domain_path",
            message="Health contribution resolved_domain_path is required.",
            field="resolved_domain_path",
        )
        issues.append(issue)
        lane_issue_codes.append(issue.code)
    if historical_version_id is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="missing_health_historical_version_id",
            message="Health contribution historical_version_id is required.",
            field="historical_version_id",
        )
        issues.append(issue)
        lane_issue_codes.append(issue.code)

    lane_spec: _HealthLaneSpec | None = None
    if not lane_issue_codes:
        assert regime_identifier is not None
        assert resolved_domain_path is not None
        assert historical_version_id is not None
        lane_spec = _find_health_lane(
            regime_identifier=regime_identifier,
            resolved_domain_path=resolved_domain_path,
            historical_version_id=historical_version_id,
        )
        if lane_spec is None:
            issue = ValidationIssue(
                severity="ERROR",
                code="unsupported_health_contribution_lane",
                message="Health contribution lane is not supported for standalone validation.",
                field="resolved_domain_path",
            )
            issues.append(issue)
            lane_issue_codes.append(issue.code)

    if lane_issue_codes:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_supported_lane_detected",
                outcome="failed",
                severity="ERROR",
                message="Health contribution lane detection failed.",
                field="resolved_domain_path",
                linked_issue_codes=tuple(lane_issue_codes),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_supported_lane_detected",
                outcome="passed",
                severity="INFO",
                message="Health contribution lane detection passed.",
                field="resolved_domain_path",
                linked_issue_codes=(),
            )
        )

    version_issue_codes: list[str] = []
    if historical_version_id is None:
        version_issue_codes.append("missing_health_historical_version_id")
    else:
        window_status = _HEALTH_WINDOW_STATUS_BY_ID.get(historical_version_id)
        if window_status != IMPLEMENTATION_READY_WINDOW:
            issue = ValidationIssue(
                severity="ERROR",
                code="health_contribution_version_window_unsupported",
                message=(
                    "Health contribution historical version window is not implementation-ready."
                ),
                field="historical_version_id",
            )
            issues.append(issue)
            version_issue_codes.append(issue.code)
        elif (
            lane_spec is not None
            and regime_identifier is not None
            and regime_identifier != lane_spec.regime_identifier
        ):
            issue = ValidationIssue(
                severity="ERROR",
                code="health_contribution_version_binding_inconsistent",
                message=(
                    "Health contribution regime_identifier is inconsistent with the supported lane."
                ),
                field="regime_identifier",
            )
            issues.append(issue)
            version_issue_codes.append(issue.code)

    if version_issue_codes:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_version_binding_consistent",
                outcome="failed",
                severity="ERROR",
                message="Health contribution version binding validation failed.",
                field="historical_version_id",
                linked_issue_codes=tuple(version_issue_codes),
            )
        )
    elif lane_spec is None:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_version_binding_consistent",
                outcome="not_applicable",
                severity="INFO",
                message=(
                    "Health contribution version binding check skipped because "
                    "no supported lane was resolved."
                ),
                field="historical_version_id",
                linked_issue_codes=(),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_version_binding_consistent",
                outcome="passed",
                severity="INFO",
                message="Health contribution version binding validation passed.",
                field="historical_version_id",
                linked_issue_codes=(),
            )
        )

    effective_date, effective_date_issue = _parse_required_date(
        primary_effective_date_raw,
        "primary_effective_date",
        "invalid_health_primary_effective_date",
        "primary_effective_date must be an ISO date string in YYYY-MM-DD format.",
    )
    effective_issue_codes: list[str] = []
    if effective_date_issue is not None:
        issues.append(effective_date_issue)
        effective_issue_codes.append(effective_date_issue.code)
    elif (
        lane_spec is not None
        and effective_date is not None
        and not _date_within_window(
            effective_date=effective_date,
            effective_start=lane_spec.effective_start,
            effective_end=lane_spec.effective_end,
        )
    ):
        issue = ValidationIssue(
            severity="ERROR",
            code="health_contribution_effective_window_inconsistent",
            message=(
                "primary_effective_date is outside the supported health contribution lane window."
            ),
            field="primary_effective_date",
        )
        issues.append(issue)
        effective_issue_codes.append(issue.code)

    if effective_issue_codes:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_effective_window_consistent",
                outcome="failed",
                severity="ERROR",
                message="Health contribution effective window validation failed.",
                field="primary_effective_date",
                linked_issue_codes=tuple(effective_issue_codes),
            )
        )
    elif lane_spec is None:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_effective_window_consistent",
                outcome="not_applicable",
                severity="INFO",
                message=(
                    "Health contribution effective window check skipped because "
                    "no supported lane was resolved."
                ),
                field="primary_effective_date",
                linked_issue_codes=(),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_effective_window_consistent",
                outcome="passed",
                severity="INFO",
                message="Health contribution effective window validation passed.",
                field="primary_effective_date",
                linked_issue_codes=(),
            )
        )

    summary_issue_codes: list[str] = []
    if contribution_basis_kes is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="missing_health_contribution_basis",
            message="Health contribution contribution_basis_kes is required.",
            field="contribution_basis_kes",
        )
        issues.append(issue)
        summary_issue_codes.append(issue.code)
    elif not _has_two_decimal_precision(contribution_basis_kes):
        issue = ValidationIssue(
            severity="ERROR",
            code="invalid_health_amount_precision",
            message=(
                "Health contribution summary amounts must be numeric with up to 2 decimal places."
            ),
            field="contribution_basis_kes",
        )
        issues.append(issue)
        summary_issue_codes.append(issue.code)

    if total_contribution_kes is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="missing_health_total_contribution",
            message="Health contribution total_contribution_kes is required.",
            field="total_contribution_kes",
        )
        issues.append(issue)
        summary_issue_codes.append(issue.code)
    elif not _has_two_decimal_precision(total_contribution_kes):
        issue = ValidationIssue(
            severity="ERROR",
            code="invalid_health_amount_precision",
            message=(
                "Health contribution summary amounts must be numeric with up to 2 decimal places."
            ),
            field="total_contribution_kes",
        )
        issues.append(issue)
        summary_issue_codes.append(issue.code)
    elif contribution_basis_kes is not None and _has_two_decimal_precision(contribution_basis_kes):
        if _to_decimal(total_contribution_kes) > _to_decimal(contribution_basis_kes):
            issue = ValidationIssue(
                severity="ERROR",
                code="health_contribution_summary_inconsistent",
                message=(
                    "Health contribution total_contribution_kes cannot exceed "
                    "contribution_basis_kes."
                ),
                field="total_contribution_kes",
            )
            issues.append(issue)
            summary_issue_codes.append(issue.code)

    if summary_issue_codes:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_summary_consistent",
                outcome="failed",
                severity="ERROR",
                message="Health contribution summary validation failed.",
                field="total_contribution_kes",
                linked_issue_codes=tuple(summary_issue_codes),
            )
        )
    else:
        rule_results.append(
            ValidationRuleResult(
                rule_code="health_contribution_summary_consistent",
                outcome="passed",
                severity="INFO",
                message="Health contribution summary validation passed.",
                field="total_contribution_kes",
                linked_issue_codes=(),
            )
        )

    return _finalize_evaluation(issues, rule_results)


def _evaluate_income_tax_forms_payload(
    *,
    source: Mapping[str, object],
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> None:
    finalized_status = _optional_non_empty_string(source.get("finalization_status"))
    result_payload = _required_mapping_value(source.get("result_payload"))
    version_identity = _required_mapping_value(result_payload.get("version_identity"))
    liability_summary = _required_mapping_value(result_payload.get("liability_summary"))

    status_issue_codes: list[str] = []
    if finalized_status != "finalized":
        issue = ValidationIssue(
            severity="ERROR",
            code="forms_income_tax_finalization_incomplete",
            message="Income-tax forms generation requires finalized computation output.",
            field="finalization_status",
        )
        issues.append(issue)
        status_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="forms_income_tax_finalization_ready",
            field="finalization_status",
            message_on_pass="Income-tax finalized-output readiness validation passed.",
            message_on_fail="Income-tax finalized-output readiness validation failed.",
            issue_codes=status_issue_codes,
        )
    )

    version_issue_codes: list[str] = []
    historical_version_id = _optional_non_empty_string(
        version_identity.get("historical_version_id")
    )
    if historical_version_id is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="forms_income_tax_historical_version_missing",
            message="Income-tax forms generation requires historical_version_id.",
            field="result_payload.version_identity.historical_version_id",
        )
        issues.append(issue)
        version_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="forms_income_tax_version_binding_ready",
            field="result_payload.version_identity.historical_version_id",
            message_on_pass="Income-tax form version binding preconditions passed.",
            message_on_fail="Income-tax form version binding preconditions failed.",
            issue_codes=version_issue_codes,
        )
    )

    summary_issue_codes: list[str] = []
    for field_name in (
        "chargeable_income_kes",
        "net_income_tax_due_kes",
        "refund_due_kes",
    ):
        value = liability_summary.get(field_name)
        if value is None or not _has_two_decimal_precision(value):
            issue = ValidationIssue(
                severity="ERROR",
                code="forms_income_tax_liability_summary_inconsistent",
                message=(
                    "Income-tax liability summary amounts must be numeric with 2-decimal precision."
                ),
                field=f"result_payload.liability_summary.{field_name}",
            )
            issues.append(issue)
            summary_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="forms_income_tax_liability_summary_ready",
            field="result_payload.liability_summary",
            message_on_pass="Income-tax liability summary pre-generation validation passed.",
            message_on_fail="Income-tax liability summary pre-generation validation failed.",
            issue_codes=summary_issue_codes,
        )
    )


def _evaluate_health_forms_payload(
    *,
    source: Mapping[str, object],
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> None:
    finalized_status = _optional_non_empty_string(source.get("finalization_status"))
    result_payload = _required_mapping_value(source.get("result_payload"))
    version_identity = _required_mapping_value(result_payload.get("version_identity"))
    contributor_outcome = _required_mapping_value(result_payload.get("contributor_outcome"))
    contribution_summary = _required_mapping_value(result_payload.get("contribution_summary"))

    status_issue_codes: list[str] = []
    if finalized_status != "finalized":
        issue = ValidationIssue(
            severity="ERROR",
            code="forms_health_contribution_finalization_incomplete",
            message="Health-contribution forms mapping requires finalized computation output.",
            field="finalization_status",
        )
        issues.append(issue)
        status_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="forms_health_contribution_finalization_ready",
            field="finalization_status",
            message_on_pass="Health-contribution finalized-output readiness validation passed.",
            message_on_fail="Health-contribution finalized-output readiness validation failed.",
            issue_codes=status_issue_codes,
        )
    )

    lane_issue_codes: list[str] = []
    historical_version_id = _optional_non_empty_string(
        version_identity.get("historical_version_id")
    )
    regime_identifier = _optional_non_empty_string(version_identity.get("regime_identifier"))
    resolved_domain_path = _optional_non_empty_string(
        contributor_outcome.get("resolved_domain_path")
    )
    if (
        historical_version_id is None
        or regime_identifier is None
        or resolved_domain_path is None
        or _find_health_lane(
            regime_identifier=regime_identifier,
            resolved_domain_path=resolved_domain_path,
            historical_version_id=historical_version_id,
        )
        is None
    ):
        issue = ValidationIssue(
            severity="ERROR",
            code="forms_health_contribution_supported_lane_missing",
            message="Health-contribution forms mapping requires a supported governed lane.",
            field="result_payload.version_identity.historical_version_id",
        )
        issues.append(issue)
        lane_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="forms_health_contribution_supported_lane_ready",
            field="result_payload.version_identity.historical_version_id",
            message_on_pass="Health-contribution lane validation passed for forms mapping.",
            message_on_fail="Health-contribution lane validation failed for forms mapping.",
            issue_codes=lane_issue_codes,
        )
    )

    summary_issue_codes: list[str] = []
    for field_name in ("contribution_basis_kes", "total_contribution_kes"):
        value = contribution_summary.get(field_name)
        if value is None or not _has_two_decimal_precision(value):
            issue = ValidationIssue(
                severity="ERROR",
                code="forms_health_contribution_summary_inconsistent",
                message=(
                    "Health-contribution summary amounts must be numeric with 2-decimal precision."
                ),
                field=f"result_payload.contribution_summary.{field_name}",
            )
            issues.append(issue)
            summary_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="forms_health_contribution_summary_ready",
            field="result_payload.contribution_summary",
            message_on_pass="Health-contribution summary pre-generation validation passed.",
            message_on_fail="Health-contribution summary pre-generation validation failed.",
            issue_codes=summary_issue_codes,
        )
    )


def _evaluate_report_request_payload(
    *,
    tax_domain: str,
    source: Mapping[str, object],
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> None:
    required_fields = (
        "computation_id",
        "form_id",
        "report_type",
        "tax_year",
        "historical_version_id",
    )
    required_issue_codes: list[str] = []
    values: dict[str, object] = {}
    for field_name in required_fields:
        value = source.get(field_name)
        values[field_name] = value
        if field_name == "tax_year":
            if not isinstance(value, int):
                issue = ValidationIssue(
                    severity="ERROR",
                    code="reports_validation_required_field_missing",
                    message="Reports generation requires deterministic lineage fields.",
                    field=field_name,
                )
                issues.append(issue)
                required_issue_codes.append(issue.code)
        elif _optional_non_empty_string(value) is None:
            issue = ValidationIssue(
                severity="ERROR",
                code="reports_validation_required_field_missing",
                message="Reports generation requires deterministic lineage fields.",
                field=field_name,
            )
            issues.append(issue)
            required_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="reports_validation_required_fields_ready",
            field="report_request",
            message_on_pass="Report-generation required field validation passed.",
            message_on_fail="Report-generation required field validation failed.",
            issue_codes=required_issue_codes,
        )
    )

    report_type = _optional_non_empty_string(values.get("report_type"))
    type_issue_codes: list[str] = []
    expected_report_type = (
        "income_tax_summary" if tax_domain == "income_tax" else "health_contribution_summary"
    )
    if report_type is not None and report_type != expected_report_type:
        issue = ValidationIssue(
            severity="ERROR",
            code="reports_validation_report_type_inconsistent",
            message="Report type is inconsistent with the requested tax domain.",
            field="report_type",
        )
        issues.append(issue)
        type_issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="reports_validation_report_type_ready",
            field="report_type",
            message_on_pass="Report-type validation passed.",
            message_on_fail="Report-type validation failed.",
            issue_codes=type_issue_codes,
        )
    )


def _evaluate_income_tax_form_result_payload(
    *,
    source: Mapping[str, object],
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> None:
    issue_codes: list[str] = []
    if _optional_non_empty_string(source.get("artifact_id")) is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="orchestration_forms_result_incomplete",
            message="Forms adapter result payload is missing artifact_id.",
            field="artifact_id",
        )
        issues.append(issue)
        issue_codes.append(issue.code)
    if _optional_non_empty_string(source.get("form_type")) != "income_tax_return":
        issue = ValidationIssue(
            severity="ERROR",
            code="orchestration_forms_result_incomplete",
            message="Forms adapter result payload has an unexpected form_type.",
            field="form_type",
        )
        issues.append(issue)
        issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="orchestration_forms_result_ready",
            field="artifact_id",
            message_on_pass="Orchestration forms result validation passed.",
            message_on_fail="Orchestration forms result validation failed.",
            issue_codes=issue_codes,
        )
    )


def _evaluate_health_form_result_payload(
    *,
    source: Mapping[str, object],
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> None:
    issue_codes: list[str] = []
    if _optional_non_empty_string(source.get("form_ready_reference")) is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="orchestration_forms_result_incomplete",
            message="Forms adapter result payload is missing form_ready_reference.",
            field="form_ready_reference",
        )
        issues.append(issue)
        issue_codes.append(issue.code)
    if _optional_non_empty_string(source.get("form_type")) != "health_contribution_summary":
        issue = ValidationIssue(
            severity="ERROR",
            code="orchestration_forms_result_incomplete",
            message="Forms adapter result payload has an unexpected form_type.",
            field="form_type",
        )
        issues.append(issue)
        issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="orchestration_forms_result_ready",
            field="form_ready_reference",
            message_on_pass="Orchestration forms result validation passed.",
            message_on_fail="Orchestration forms result validation failed.",
            issue_codes=issue_codes,
        )
    )


def _evaluate_report_result_payload(
    *,
    tax_domain: str,
    source: Mapping[str, object],
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> None:
    issue_codes: list[str] = []
    if _optional_non_empty_string(source.get("report_id")) is None:
        issue = ValidationIssue(
            severity="ERROR",
            code="orchestration_reports_result_incomplete",
            message="Reports adapter result payload is missing report_id.",
            field="report_id",
        )
        issues.append(issue)
        issue_codes.append(issue.code)
    expected_report_type = (
        "income_tax_summary" if tax_domain == "income_tax" else "health_contribution_summary"
    )
    if _optional_non_empty_string(source.get("report_type")) != expected_report_type:
        issue = ValidationIssue(
            severity="ERROR",
            code="orchestration_reports_result_incomplete",
            message="Reports adapter result payload has an unexpected report_type.",
            field="report_type",
        )
        issues.append(issue)
        issue_codes.append(issue.code)
    lineage_reference = _required_mapping_value(source.get("lineage_reference"))
    for field_name in ("historical_version_id", "supported_lane_id"):
        if _optional_non_empty_string(lineage_reference.get(field_name)) is None:
            issue = ValidationIssue(
                severity="ERROR",
                code="orchestration_reports_result_incomplete",
                message="Reports adapter result payload is missing lineage_reference fields.",
                field=f"lineage_reference.{field_name}",
            )
            issues.append(issue)
            issue_codes.append(issue.code)
    rule_results.append(
        _rule_from_issue_codes(
            rule_code="orchestration_reports_result_ready",
            field="report_id",
            message_on_pass="Orchestration reports result validation passed.",
            message_on_fail="Orchestration reports result validation failed.",
            issue_codes=issue_codes,
        )
    )


def _finalize_evaluation(
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> ValidationEvaluation:
    if not issues:
        issues.append(
            ValidationIssue(
                severity="INFO",
                code="validation_passed",
                message="Validation checks passed.",
                field=None,
            )
        )

    status: ValidationStatus = (
        "rejected" if any(issue.severity == "ERROR" for issue in issues) else "accepted"
    )
    return ValidationEvaluation(
        validation_status=status,
        issues=tuple(issues),
        rule_results=tuple(rule_results),
    )


def _build_workflow_envelope(
    *,
    workflow: str,
    tax_domain: str,
    issues: list[ValidationIssue],
    rule_results: list[ValidationRuleResult],
) -> GovernedValidationEnvelope:
    normalized_issues = (
        tuple(issues)
        if issues
        else (
            ValidationIssue(
                severity="INFO",
                code="validation_passed",
                message="Validation checks passed.",
                field=None,
            ),
        )
    )
    validation_status: ValidationStatus = (
        "rejected" if any(issue.severity == "ERROR" for issue in normalized_issues) else "accepted"
    )
    return build_governed_validation_envelope(
        workflow=workflow,
        tax_domain=tax_domain,
        validation_status=validation_status,
        issues=normalized_issues,
        rule_results=tuple(rule_results),
    )


def _rule_from_issue_codes(
    *,
    rule_code: str,
    field: str | None,
    message_on_pass: str,
    message_on_fail: str,
    issue_codes: list[str],
) -> ValidationRuleResult:
    if issue_codes:
        return ValidationRuleResult(
            rule_code=rule_code,
            outcome="failed",
            severity="ERROR",
            message=message_on_fail,
            field=field,
            linked_issue_codes=tuple(issue_codes),
        )
    return ValidationRuleResult(
        rule_code=rule_code,
        outcome="passed",
        severity="INFO",
        message=message_on_pass,
        field=field,
        linked_issue_codes=(),
    )


def _find_health_lane(
    *,
    regime_identifier: str,
    resolved_domain_path: str,
    historical_version_id: str,
) -> _HealthLaneSpec | None:
    for lane in _SUPPORTED_HEALTH_LANES:
        if (
            lane.regime_identifier == regime_identifier
            and lane.resolved_domain_path == resolved_domain_path
            and lane.historical_version_id == historical_version_id
        ):
            return lane
    return None


def _date_within_window(
    *,
    effective_date: date,
    effective_start: str,
    effective_end: str,
) -> bool:
    start = date.fromisoformat(effective_start)
    if effective_date < start:
        return False
    if effective_end == "open":
        return True
    end = date.fromisoformat(effective_end)
    return effective_date <= end


def _optional_non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _as_object(value: Mapping[str, object] | object) -> dict[str, object]:
    if isinstance(value, Mapping):
        typed_value = cast(Mapping[object, object], value)
        output: dict[str, object] = {}
        for key, item in typed_value.items():
            output[str(key)] = item
        return output
    return {}


def _required_mapping_value(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        typed_value = cast(Mapping[object, object], value)
        output: dict[str, object] = {}
        for key, item in typed_value.items():
            output[str(key)] = item
        return output
    return {}


def _parse_optional_date(
    value: object,
    field_name: str,
) -> tuple[date | None, ValidationIssue | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, ValidationIssue(
            severity="ERROR",
            code="invalid_date_format",
            message=f"{field_name} must be an ISO date string in YYYY-MM-DD format.",
            field=field_name,
        )
    normalized = value.strip()
    if not normalized:
        return None, None
    try:
        return date.fromisoformat(normalized), None
    except ValueError:
        return None, ValidationIssue(
            severity="ERROR",
            code="invalid_date_format",
            message=f"{field_name} must be an ISO date string in YYYY-MM-DD format.",
            field=field_name,
        )


def _parse_required_date(
    value: object,
    field_name: str,
    issue_code: str,
    message: str,
) -> tuple[date | None, ValidationIssue | None]:
    if not isinstance(value, str):
        return None, ValidationIssue(
            severity="ERROR",
            code=issue_code,
            message=message,
            field=field_name,
        )
    normalized = value.strip()
    if not normalized:
        return None, ValidationIssue(
            severity="ERROR",
            code=issue_code,
            message=message,
            field=field_name,
        )
    try:
        return date.fromisoformat(normalized), None
    except ValueError:
        return None, ValidationIssue(
            severity="ERROR",
            code=issue_code,
            message=message,
            field=field_name,
        )


def _has_two_decimal_precision(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    exponent = decimal_value.as_tuple().exponent
    return isinstance(exponent, int) and exponent >= -2


def _to_decimal(value: object) -> Decimal:
    return Decimal(str(value))
