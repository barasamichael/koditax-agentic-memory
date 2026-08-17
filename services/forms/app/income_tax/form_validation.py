"""Deterministic pre-generation validation for governed income-tax form contexts."""

from __future__ import annotations

import re
from typing import cast
from typing import Literal
from typing import TypedDict
from collections.abc import Mapping

from services.forms.app.income_tax.form_version_binding import SUPPORTED_FORM_VERSION_BINDINGS

ValidationSeverity = Literal["info", "warning", "error"]

REQUIRED_FIELD_MISSING = "forms_required_field_missing"
FIELD_VALUE_INVALID = "forms_field_value_invalid"
CROSS_FIELD_INCONSISTENT = "forms_cross_field_inconsistent"
MONEY_PATTERN = re.compile(r"^-?\d+\.\d{2}$")
QUALIFYING_INTEREST_LANE_ID = "resident_employment_plus_qualifying_interest_2023_07_01"


class ValidationFinding(TypedDict):
    """Represent one deterministic machine-consumable validation finding."""

    code: str
    message: str
    field: str
    severity: ValidationSeverity


class FormValidationResult(TypedDict):
    """Represent deterministic pre-generation validation results."""

    is_valid: bool
    validation_status: Literal["valid", "invalid"]
    findings: list[ValidationFinding]


def validate_income_tax_pre_generation_context(
    *,
    form_ready_output: Mapping[str, object],
    form_version_binding: Mapping[str, object],
) -> FormValidationResult:
    """Validate mapped/bound income-tax context before artifact generation."""

    mapped = _as_object(form_ready_output)
    binding = _as_object(form_version_binding)
    findings: list[ValidationFinding] = []

    mapping_status = _required_string(
        mapped,
        "mapping_status",
        "form_ready_output.mapping_status",
        findings,
    )
    binding_status = _required_string(
        binding,
        "binding_status",
        "form_version_binding.binding_status",
        findings,
    )
    mapped_form_type = _required_string(
        mapped,
        "form_type",
        "form_ready_output.form_type",
        findings,
    )
    binding_form_type = _required_string(
        binding,
        "form_type",
        "form_version_binding.form_type",
        findings,
    )
    mapped_lane = _required_string(
        mapped,
        "supported_lane_id",
        "form_ready_output.supported_lane_id",
        findings,
    )
    binding_lane = _required_string(
        binding,
        "supported_lane_id",
        "form_version_binding.supported_lane_id",
        findings,
    )
    binding_historical_version = _required_string(
        binding,
        "historical_version_id",
        "form_version_binding.historical_version_id",
        findings,
    )
    form_version_id = _required_string(
        binding,
        "form_version_id",
        "form_version_binding.form_version_id",
        findings,
    )
    template_id = _required_string(
        binding,
        "template_id",
        "form_version_binding.template_id",
        findings,
    )
    binding_tax_year = _required_int(
        binding,
        "tax_year",
        "form_version_binding.tax_year",
        findings,
    )

    mapped_identity = _required_object(
        mapped,
        "computation_identity",
        "form_ready_output.computation_identity",
        findings,
    )
    mapped_version_identity = _required_object(
        mapped,
        "version_identity",
        "form_ready_output.version_identity",
        findings,
    )
    mapped_liability_fields = _required_object(
        mapped,
        "liability_fields",
        "form_ready_output.liability_fields",
        findings,
    )
    mapped_form_fields = _required_object(
        mapped,
        "form_fields",
        "form_ready_output.form_fields",
        findings,
    )
    binding_lineage = _required_object(
        binding,
        "binding_lineage",
        "form_version_binding.binding_lineage",
        findings,
    )

    mapped_computation_id = _required_string(
        mapped_identity,
        "computation_id",
        "form_ready_output.computation_identity.computation_id",
        findings,
    )
    mapped_tax_year = _required_int(
        mapped_identity,
        "tax_year",
        "form_ready_output.computation_identity.tax_year",
        findings,
    )
    mapped_finalized_audit_event_id = _required_string(
        mapped_identity,
        "finalized_audit_event_id",
        "form_ready_output.computation_identity.finalized_audit_event_id",
        findings,
    )
    mapped_input_hash = _required_string(
        mapped_identity,
        "input_hash",
        "form_ready_output.computation_identity.input_hash",
        findings,
    )
    mapped_historical_version = _required_string(
        mapped_version_identity,
        "historical_version_id",
        "form_ready_output.version_identity.historical_version_id",
        findings,
    )
    bound_computation_id = _required_string(
        binding_lineage,
        "computation_id",
        "form_version_binding.binding_lineage.computation_id",
        findings,
    )
    bound_finalized_audit_event_id = _required_string(
        binding_lineage,
        "finalized_audit_event_id",
        "form_version_binding.binding_lineage.finalized_audit_event_id",
        findings,
    )
    bound_input_hash = _required_string(
        binding_lineage,
        "input_hash",
        "form_version_binding.binding_lineage.input_hash",
        findings,
    )

    chargeable_income_liability = _required_money_string(
        mapped_liability_fields,
        "chargeable_income_kes",
        "form_ready_output.liability_fields.chargeable_income_kes",
        findings,
    )
    net_income_tax_due_liability = _required_money_string(
        mapped_liability_fields,
        "net_income_tax_due_kes",
        "form_ready_output.liability_fields.net_income_tax_due_kes",
        findings,
    )
    refund_due_liability = _required_money_string(
        mapped_liability_fields,
        "refund_due_kes",
        "form_ready_output.liability_fields.refund_due_kes",
        findings,
    )
    excluded_income_liability = _required_money_string(
        mapped_liability_fields,
        "final_tax_excluded_income_kes",
        "form_ready_output.liability_fields.final_tax_excluded_income_kes",
        findings,
    )
    chargeable_income_form = _required_money_string(
        mapped_form_fields,
        "chargeable_income_kes",
        "form_ready_output.form_fields.chargeable_income_kes",
        findings,
    )
    net_income_tax_due_form = _required_money_string(
        mapped_form_fields,
        "net_income_tax_due_kes",
        "form_ready_output.form_fields.net_income_tax_due_kes",
        findings,
    )
    refund_due_form = _required_money_string(
        mapped_form_fields,
        "refund_due_kes",
        "form_ready_output.form_fields.refund_due_kes",
        findings,
    )
    investment_income_form = _required_money_string(
        mapped_form_fields,
        "investment_income_kes",
        "form_ready_output.form_fields.investment_income_kes",
        findings,
    )

    if mapping_status != "ok":
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message="Mapping status must be 'ok' before generation.",
            field="form_ready_output.mapping_status",
        )

    if binding_status != "bound":
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message="Binding status must be 'bound' before generation.",
            field="form_version_binding.binding_status",
        )

    if mapped_form_type != "income_tax_return":
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message="Mapped form type must be 'income_tax_return'.",
            field="form_ready_output.form_type",
        )
    if binding_form_type != "income_tax_return":
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message="Bound form type must be 'income_tax_return'.",
            field="form_version_binding.form_type",
        )

    _append_cross_field_if_mismatch(
        findings,
        mapped_form_type,
        binding_form_type,
        field="form_version_binding.form_type",
        message="Mapped and bound form types must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        mapped_lane,
        binding_lane,
        field="form_version_binding.supported_lane_id",
        message="Mapped and bound lane identifiers must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        mapped_historical_version,
        binding_historical_version,
        field="form_version_binding.historical_version_id",
        message="Mapped and bound historical version identifiers must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        mapped_computation_id,
        bound_computation_id,
        field="form_version_binding.binding_lineage.computation_id",
        message="Mapped and bound computation identifiers must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        mapped_tax_year,
        binding_tax_year,
        field="form_version_binding.tax_year",
        message="Mapped and bound tax years must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        mapped_finalized_audit_event_id,
        bound_finalized_audit_event_id,
        field="form_version_binding.binding_lineage.finalized_audit_event_id",
        message="Mapped and bound finalized audit event identifiers must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        mapped_input_hash,
        bound_input_hash,
        field="form_version_binding.binding_lineage.input_hash",
        message="Mapped and bound input hashes must match.",
    )
    _append_cross_field_if_mismatch(
        findings,
        chargeable_income_liability,
        chargeable_income_form,
        field="form_ready_output.form_fields.chargeable_income_kes",
        message=(
            "Form field chargeable income must equal liability field chargeable income "
            "for supported templates."
        ),
    )
    _append_cross_field_if_mismatch(
        findings,
        net_income_tax_due_liability,
        net_income_tax_due_form,
        field="form_ready_output.form_fields.net_income_tax_due_kes",
        message=(
            "Form field net income tax due must equal liability field net income tax due "
            "for supported templates."
        ),
    )
    _append_cross_field_if_mismatch(
        findings,
        refund_due_liability,
        refund_due_form,
        field="form_ready_output.form_fields.refund_due_kes",
        message=(
            "Form field refund due must equal liability field refund due for supported templates."
        ),
    )

    expected_binding = _expected_binding_for_context(
        supported_lane_id=mapped_lane,
        historical_version_id=mapped_historical_version,
        tax_year=mapped_tax_year,
    )
    if expected_binding is None:
        has_complete_template_context = (
            mapped_lane is not None
            and mapped_historical_version is not None
            and mapped_tax_year is not None
        )
        if has_complete_template_context:
            _append_finding(
                findings,
                code=FIELD_VALUE_INVALID,
                message=(
                    "No governed form template exists for the supplied lane and version context."
                ),
                field="form_version_binding.form_version_id",
            )
    else:
        _append_cross_field_if_mismatch(
            findings,
            form_version_id,
            expected_binding["form_version_id"],
            field="form_version_binding.form_version_id",
            message=("Bound form version id must match governed template for the mapped context."),
        )
        _append_cross_field_if_mismatch(
            findings,
            template_id,
            expected_binding["template_id"],
            field="form_version_binding.template_id",
            message="Bound template id must match governed template for the mapped context.",
        )

    if mapped_lane == QUALIFYING_INTEREST_LANE_ID:
        _append_cross_field_if_mismatch(
            findings,
            investment_income_form,
            excluded_income_liability,
            field="form_ready_output.form_fields.investment_income_kes",
            message=(
                "Qualifying-interest template requires investment income to equal "
                "final-tax excluded income."
            ),
        )

    is_valid = len(findings) == 0
    return {
        "is_valid": is_valid,
        "validation_status": "valid" if is_valid else "invalid",
        "findings": _sort_findings(findings),
    }


def _append_finding(
    findings: list[ValidationFinding],
    *,
    code: str,
    message: str,
    field: str,
) -> None:
    findings.append(
        {
            "code": code,
            "message": message,
            "field": field,
            "severity": "error",
        }
    )


def _sort_findings(findings: list[ValidationFinding]) -> list[ValidationFinding]:
    return sorted(
        findings,
        key=lambda finding: (finding["field"], finding["code"], finding["message"]),
    )


def _append_cross_field_if_mismatch(
    findings: list[ValidationFinding],
    left: object | None,
    right: object | None,
    *,
    field: str,
    message: str,
) -> None:
    if left is None or right is None:
        return
    if left == right:
        return
    _append_finding(
        findings,
        code=CROSS_FIELD_INCONSISTENT,
        message=message,
        field=field,
    )


def _expected_binding_for_context(
    *,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
) -> dict[str, str] | None:
    if supported_lane_id is None or historical_version_id is None or tax_year is None:
        return None
    expected = SUPPORTED_FORM_VERSION_BINDINGS.get(
        (supported_lane_id, historical_version_id, tax_year)
    )
    return dict(expected) if expected is not None else None


def _as_object(value: Mapping[str, object]) -> dict[str, object]:
    typed = cast(Mapping[object, object], value)
    return {str(key): typed[key] for key in typed}


def _required_object(
    source: Mapping[str, object] | None,
    field_name: str,
    field_path: str,
    findings: list[ValidationFinding],
) -> dict[str, object] | None:
    if source is None:
        return None
    if field_name not in source:
        _append_finding(
            findings,
            code=REQUIRED_FIELD_MISSING,
            message=f"Required field '{field_path}' is missing.",
            field=field_path,
        )
        return None
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message=f"Field '{field_path}' must be an object.",
            field=field_path,
        )
        return None
    typed = cast(Mapping[object, object], value)
    return {str(key): typed[key] for key in typed}


def _required_string(
    source: Mapping[str, object] | None,
    field_name: str,
    field_path: str,
    findings: list[ValidationFinding],
) -> str | None:
    if source is None:
        return None
    if field_name not in source:
        _append_finding(
            findings,
            code=REQUIRED_FIELD_MISSING,
            message=f"Required field '{field_path}' is missing.",
            field=field_path,
        )
        return None
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message=f"Field '{field_path}' must be a non-empty string.",
            field=field_path,
        )
        return None
    return value.strip()


def _required_int(
    source: Mapping[str, object] | None,
    field_name: str,
    field_path: str,
    findings: list[ValidationFinding],
) -> int | None:
    if source is None:
        return None
    if field_name not in source:
        _append_finding(
            findings,
            code=REQUIRED_FIELD_MISSING,
            message=f"Required field '{field_path}' is missing.",
            field=field_path,
        )
        return None
    value = source.get(field_name)
    if not isinstance(value, int):
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message=f"Field '{field_path}' must be an integer.",
            field=field_path,
        )
        return None
    return value


def _required_money_string(
    source: Mapping[str, object] | None,
    field_name: str,
    field_path: str,
    findings: list[ValidationFinding],
) -> str | None:
    value = _required_string(source, field_name, field_path, findings)
    if value is None:
        return None
    if MONEY_PATTERN.fullmatch(value) is None:
        _append_finding(
            findings,
            code=FIELD_VALUE_INVALID,
            message=f"Field '{field_path}' must use canonical money format (e.g., 0.00).",
            field=field_path,
        )
        return None
    return value
