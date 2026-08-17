"""Bind supported income-tax form-ready outputs to deterministic form versions."""

from __future__ import annotations

from typing import cast
from collections.abc import Mapping

from services.forms.app.income_tax.form_mapping import FORM_TYPE
from services.forms.app.income_tax.form_audit_coverage import (
    build_income_tax_form_binding_audit_evidence,
)


class IncomeTaxFormVersionBindingError(RuntimeError):
    """Represent deterministic form-version binding failures."""

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


SUPPORTED_FORM_VERSION_BINDINGS: dict[tuple[str, str, int], dict[str, str]] = {
    (
        "resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): {
        "form_version_id": "ITX-FORM-20210101-RES-EMP-V1",
        "template_id": "income_tax_return_resident_employment_2021_01_01_v1",
    },
    (
        "non_resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): {
        "form_version_id": "ITX-FORM-20210101-NRES-EMP-V1",
        "template_id": "income_tax_return_non_resident_employment_2021_01_01_v1",
    },
    (
        "resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "form_version_id": "ITX-FORM-20230701-RES-EMP-V1",
        "template_id": "income_tax_return_resident_employment_2023_07_01_v1",
    },
    (
        "non_resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "form_version_id": "ITX-FORM-20230701-NRES-EMP-V1",
        "template_id": "income_tax_return_non_resident_employment_2023_07_01_v1",
    },
    (
        "resident_employment_plus_qualifying_interest_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "form_version_id": "ITX-FORM-20230701-RES-EMP-QINT-V1",
        "template_id": (
            "income_tax_return_resident_employment_plus_qualifying_interest_2023_07_01_v1"
        ),
    },
}


def bind_income_tax_form_version(form_ready_output: Mapping[str, object]) -> dict[str, object]:
    """Bind one supported form-ready output to a governed form version/template."""

    source = _as_object(form_ready_output, reason="invalid_form_ready_output")
    mapping_status = _require_string(source, "mapping_status")
    form_type = _require_string(source, "form_type")
    supported_lane_id = _require_string(source, "supported_lane_id")

    if mapping_status != "ok":
        raise IncomeTaxFormVersionBindingError(
            reason="invalid_mapping_status",
            message="Form version binding requires a successful form mapping result.",
            details={"mapping_status": mapping_status},
        )
    if form_type != FORM_TYPE:
        raise IncomeTaxFormVersionBindingError(
            reason="unsupported_form_type",
            message="Form version binding supports income-tax return forms only.",
            details={"form_type": form_type},
        )

    computation_identity = _require_object(source, "computation_identity")
    version_identity = _require_object(source, "version_identity")
    taxpayer = _require_object(source, "taxpayer")
    lineage = _require_object(source, "lineage")

    tax_year = _require_int(computation_identity, "tax_year")
    historical_version_id = _require_string(version_identity, "historical_version_id")
    effective_start = _require_string(version_identity, "effective_start")
    effective_end = _require_string(version_identity, "effective_end")
    _require_string(version_identity, "version_selection_basis")
    resident_status = _require_string(taxpayer, "resident_status")
    source_anchor_ids = _list_of_strings(version_identity, "source_anchor_ids")
    if (supported_lane_id.startswith("resident_") and resident_status == "non_resident") or (
        supported_lane_id.startswith("non_resident_") and resident_status == "resident"
    ):
        raise IncomeTaxFormVersionBindingError(
            reason="ambiguous_form_version_context",
            message="Version identity context is ambiguous for governed form binding.",
            details={
                "supported_lane_id": supported_lane_id,
                "resident_status": resident_status,
                "historical_version_id": historical_version_id,
                "effective_start": effective_start,
                "effective_end": effective_end,
            },
        )

    binding_key = (supported_lane_id, historical_version_id, tax_year)
    binding = SUPPORTED_FORM_VERSION_BINDINGS.get(binding_key)
    if binding is None:
        raise IncomeTaxFormVersionBindingError(
            reason="unsupported_form_version_binding",
            message="No governed form-version binding exists for this supported lane context.",
            details={
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": tax_year,
            },
        )

    binding_output: dict[str, object] = {
        "binding_status": "bound",
        "form_type": form_type,
        "form_version_id": binding["form_version_id"],
        "template_id": binding["template_id"],
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "binding_lineage": {
            "computation_id": _require_string(computation_identity, "computation_id"),
            "finalized_audit_event_id": _require_string(
                computation_identity, "finalized_audit_event_id"
            ),
            "input_hash": _require_string(computation_identity, "input_hash"),
            "resident_status": resident_status,
            "taxpayer_kind": _require_string(taxpayer, "taxpayer_kind"),
            "effective_start": effective_start,
            "effective_end": effective_end,
            "source_anchor_ids": source_anchor_ids,
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
        },
    }
    binding_output["audit_evidence"] = build_income_tax_form_binding_audit_evidence(binding_output)
    return binding_output


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxFormVersionBindingError(
            reason=reason,
            message="Expected JSON object input for deterministic form version binding.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxFormVersionBindingError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxFormVersionBindingError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxFormVersionBindingError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxFormVersionBindingError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxFormVersionBindingError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings
