"""Bind supported income-tax reports to deterministic report-version identities."""

from __future__ import annotations

from typing import cast
from collections.abc import Mapping

from services.forms.app.income_tax.report_generation import REPORT_TYPE


class IncomeTaxReportVersionBindingError(RuntimeError):
    """Represent deterministic report-version binding failures."""

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


SUPPORTED_REPORT_VERSION_BINDINGS: dict[tuple[str, str, int], dict[str, str]] = {
    (
        "resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): {
        "report_version_id": "ITX-RPT-20210101-RES-EMP-V1",
        "report_template_id": "income_tax_report_resident_employment_2021_01_01_v1",
    },
    (
        "non_resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): {
        "report_version_id": "ITX-RPT-20210101-NRES-EMP-V1",
        "report_template_id": "income_tax_report_non_resident_employment_2021_01_01_v1",
    },
    (
        "resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "report_version_id": "ITX-RPT-20230701-RES-EMP-V1",
        "report_template_id": "income_tax_report_resident_employment_2023_07_01_v1",
    },
    (
        "non_resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "report_version_id": "ITX-RPT-20230701-NRES-EMP-V1",
        "report_template_id": "income_tax_report_non_resident_employment_2023_07_01_v1",
    },
    (
        "resident_employment_plus_qualifying_interest_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "report_version_id": "ITX-RPT-20230701-RES-EMP-QINT-V1",
        "report_template_id": (
            "income_tax_report_resident_employment_plus_qualifying_interest_2023_07_01_v1"
        ),
    },
}


def bind_income_tax_report_version(report_output: Mapping[str, object]) -> dict[str, object]:
    """Bind one supported report output to a governed report version/template."""

    source = _as_object(report_output, reason="invalid_report_output")
    generation_status = _require_string(source, "generation_status")
    report_type = _require_string(source, "report_type")
    report_version = _require_string(source, "report_version")

    if generation_status != "generated":
        raise IncomeTaxReportVersionBindingError(
            reason="invalid_report_generation_status",
            message="Report version binding requires a generated report output.",
            details={"generation_status": generation_status},
        )
    if report_type != REPORT_TYPE:
        raise IncomeTaxReportVersionBindingError(
            reason="unsupported_report_type",
            message="Report version binding supports governed income-tax reports only.",
            details={"report_type": report_type},
        )

    supported_lane_id = _require_string(source, "supported_lane_id")
    historical_version_id = _require_string(source, "historical_version_id")
    tax_year = _require_int(source, "tax_year")

    binding_key = (supported_lane_id, historical_version_id, tax_year)
    binding = SUPPORTED_REPORT_VERSION_BINDINGS.get(binding_key)
    if binding is None:
        raise IncomeTaxReportVersionBindingError(
            reason="unsupported_report_version_binding",
            message="No governed report-version binding exists for this supported lane context.",
            details={
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": tax_year,
            },
        )

    lineage = _require_object(source, "lineage")
    audit_evidence = _require_object(source, "audit_evidence")
    if _require_string(audit_evidence, "action") != "report_generation":
        raise IncomeTaxReportVersionBindingError(
            reason="invalid_report_audit_evidence",
            message="Report version binding requires report-generation audit evidence lineage.",
        )

    return {
        "binding_status": "bound",
        "report_type": report_type,
        "report_version": report_version,
        "report_version_id": binding["report_version_id"],
        "report_template_id": binding["report_template_id"],
        "report_id": _require_string(source, "report_id"),
        "form_artifact_id": _require_string(source, "form_artifact_id"),
        "computation_id": _require_string(source, "computation_id"),
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "binding_lineage": {
            "input_hash": _require_string(lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
            "artifact_audit_evidence_id": _require_string(lineage, "artifact_audit_evidence_id"),
            "report_audit_evidence_id": _require_string(audit_evidence, "audit_evidence_id"),
            "report_content_sha256": _require_string(lineage, "report_content_sha256"),
        },
    }


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxReportVersionBindingError(
            reason=reason,
            message="Expected JSON object input for deterministic report version binding.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxReportVersionBindingError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxReportVersionBindingError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxReportVersionBindingError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxReportVersionBindingError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxReportVersionBindingError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings
