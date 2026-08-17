"""Generate deterministic income-tax reports from finalized form artifacts."""

from __future__ import annotations

from typing import cast
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

REPORT_TYPE = "income_tax_computation_report"
REPORT_VERSION = "income_tax_vertical_slice_report_v1"
SUPPORTED_REPORT_BINDINGS: dict[tuple[str, str, int], str] = {
    (
        "resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): "Income Tax Report - Resident Employment (2021 Window)",
    (
        "non_resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): "Income Tax Report - Non-Resident Employment (2021 Window)",
    (
        "resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): "Income Tax Report - Resident Employment (2023 Window)",
    (
        "non_resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): "Income Tax Report - Non-Resident Employment (2023 Window)",
    (
        "resident_employment_plus_qualifying_interest_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): "Income Tax Report - Resident Employment + Qualifying Interest (2023 Window)",
}


class IncomeTaxReportGenerationError(RuntimeError):
    """Represent deterministic report-generation failures."""

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


def generate_income_tax_report(
    *,
    form_artifact_output: Mapping[str, object],
) -> dict[str, object]:
    """Generate one deterministic report from one finalized supported form artifact."""

    source = _as_object(form_artifact_output, reason="invalid_form_artifact_output")
    generation_status = _require_string(source, "generation_status")
    if generation_status != "generated":
        raise IncomeTaxReportGenerationError(
            reason="upstream_artifact_not_generated",
            message="Report generation requires a generated form artifact output.",
            details={"generation_status": generation_status},
        )

    artifact_type = _require_string(source, "artifact_type")
    if artifact_type != "income_tax_form_artifact":
        raise IncomeTaxReportGenerationError(
            reason="unsupported_artifact_type",
            message="Report generation supports governed income-tax form artifacts only.",
            details={"artifact_type": artifact_type},
        )

    form_type = _require_string(source, "form_type")
    if form_type != "income_tax_return":
        raise IncomeTaxReportGenerationError(
            reason="unsupported_form_scope",
            message="Report generation supports governed income-tax form artifacts only.",
            details={"form_type": form_type},
        )

    form_artifact_id = _require_string(source, "artifact_id")
    form_version_id = _require_string(source, "form_version_id")
    template_id = _require_string(source, "template_id")
    supported_lane_id = _require_string(source, "supported_lane_id")
    historical_version_id = _require_string(source, "historical_version_id")
    computation_id = _require_string(source, "computation_id")
    tax_year = _require_int(source, "tax_year")
    artifact_content_sha256 = _require_string(source, "content_sha256")

    report_binding_key = (supported_lane_id, historical_version_id, tax_year)
    report_title = SUPPORTED_REPORT_BINDINGS.get(report_binding_key)
    if report_title is None:
        raise IncomeTaxReportGenerationError(
            reason="unsupported_report_scope",
            message="No governed report binding exists for this supported lane context.",
            details={
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": tax_year,
            },
        )

    artifact_lineage = _require_object(source, "lineage")
    finalization_status = _require_string(artifact_lineage, "finalization_status")
    if finalization_status != "finalized":
        raise IncomeTaxReportGenerationError(
            reason="upstream_not_finalized",
            message="Report generation requires finalized computation lineage.",
            details={"finalization_status": finalization_status},
        )
    finalized_at = _require_string(artifact_lineage, "finalized_at")
    finalized_audit_event_id = _require_string(artifact_lineage, "finalized_audit_event_id")
    input_hash = _require_string(artifact_lineage, "input_hash")
    source_anchor_ids = _list_of_strings(artifact_lineage, "source_anchor_ids")
    applied_policy_ids = _list_of_strings(artifact_lineage, "applied_policy_ids")
    validation_focus_domains = _list_of_strings(artifact_lineage, "validation_focus_domains")
    binding_lineage = _require_object(artifact_lineage, "binding_lineage")

    generated_content_payload = _require_object(source, "generated_content_payload")
    header = _require_object(generated_content_payload, "header")
    _require_matching_value(
        "form_version_id",
        form_version_id,
        _require_string(header, "form_version_id"),
    )
    _require_matching_value(
        "supported_lane_id",
        supported_lane_id,
        _require_string(header, "supported_lane_id"),
    )
    _require_matching_value(
        "historical_version_id",
        historical_version_id,
        _require_string(header, "historical_version_id"),
    )
    _require_matching_value("tax_year", tax_year, _require_int(header, "tax_year"))

    liability_fields = _require_object(generated_content_payload, "liability_fields")
    domain_fields = _require_object(generated_content_payload, "domain_fields")
    form_fields = _require_object(generated_content_payload, "form_fields")
    impact_fields = _require_object(generated_content_payload, "impact_fields")
    treatment_fields = _require_object(generated_content_payload, "treatment_fields")
    taxpayer = _require_object(generated_content_payload, "taxpayer")

    human_readable_summary: dict[str, object] = {
        "title": report_title,
        "period_label": f"Tax Year {tax_year}",
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "taxpayer_label": _build_taxpayer_label(taxpayer),
        "net_income_tax_due_kes": _require_string(liability_fields, "net_income_tax_due_kes"),
        "refund_due_kes": _require_string(liability_fields, "refund_due_kes"),
        "chargeable_income_kes": _require_string(liability_fields, "chargeable_income_kes"),
        "total_reliefs_kes": _require_string(liability_fields, "total_reliefs_kes"),
        "final_tax_excluded_income_kes": _require_string(
            liability_fields, "final_tax_excluded_income_kes"
        ),
    }
    machine_usable_summary: dict[str, object] = {
        "header": header,
        "taxpayer": taxpayer,
        "liability_fields": liability_fields,
        "domain_fields": domain_fields,
        "form_fields": form_fields,
        "impact_fields": impact_fields,
        "treatment_fields": treatment_fields,
    }
    report_payload = {
        "human_readable_summary": human_readable_summary,
        "machine_usable_summary": machine_usable_summary,
    }
    report_content_sha256 = _sha256_hex(canonical_json_dumps(report_payload))
    report_identity_payload = {
        "report_type": REPORT_TYPE,
        "report_version": REPORT_VERSION,
        "form_artifact_id": form_artifact_id,
        "computation_id": computation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "report_content_sha256": report_content_sha256,
    }
    report_id = _sha256_hex(canonical_json_dumps(report_identity_payload))

    report_output: dict[str, object] = {
        "generation_status": "generated",
        "report_id": report_id,
        "report_type": REPORT_TYPE,
        "report_version": REPORT_VERSION,
        "form_artifact_id": form_artifact_id,
        "computation_id": computation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "human_readable_summary": human_readable_summary,
        "machine_usable_summary": machine_usable_summary,
        "lineage": {
            "form_type": form_type,
            "form_version_id": form_version_id,
            "template_id": template_id,
            "input_hash": input_hash,
            "finalized_at": finalized_at,
            "finalized_audit_event_id": finalized_audit_event_id,
            "source_anchor_ids": source_anchor_ids,
            "applied_policy_ids": applied_policy_ids,
            "validation_focus_domains": validation_focus_domains,
            "artifact_content_sha256": artifact_content_sha256,
            "report_content_sha256": report_content_sha256,
            "binding_lineage": binding_lineage,
            "artifact_audit_evidence_id": _extract_artifact_audit_evidence_id(source),
        },
    }
    report_output["audit_evidence"] = _build_report_generation_audit_evidence(
        report_output=report_output
    )
    return report_output


def _build_taxpayer_label(taxpayer: Mapping[str, object]) -> str:
    taxpayer_kind = _require_string(taxpayer, "taxpayer_kind").replace("_", " ")
    resident_status = _require_string(taxpayer, "resident_status").replace("_", " ")
    return f"{taxpayer_kind.title()} - {resident_status.title()}"


def _extract_artifact_audit_evidence_id(source: Mapping[str, object]) -> str:
    artifact_audit = _require_object(source, "audit_evidence")
    action = _require_string(artifact_audit, "action")
    if action != "artifact_generation":
        raise IncomeTaxReportGenerationError(
            reason="invalid_artifact_audit_evidence",
            message="Report generation requires artifact-generation audit evidence lineage.",
            details={"action": action},
        )
    return _require_string(artifact_audit, "audit_evidence_id")


def _build_report_generation_audit_evidence(
    *,
    report_output: Mapping[str, object],
) -> dict[str, object]:
    lineage = _require_object(report_output, "lineage")
    payload = {
        "audit_kind": "income_tax_report_generation",
        "action": "report_generation",
        "action_status": "generated",
        "report_id": _require_string(report_output, "report_id"),
        "report_type": _require_string(report_output, "report_type"),
        "report_version": _require_string(report_output, "report_version"),
        "form_artifact_id": _require_string(report_output, "form_artifact_id"),
        "computation_id": _require_string(report_output, "computation_id"),
        "supported_lane_id": _require_string(report_output, "supported_lane_id"),
        "historical_version_id": _require_string(report_output, "historical_version_id"),
        "tax_year": _require_int(report_output, "tax_year"),
        "lineage": {
            "input_hash": _require_string(lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
            "artifact_audit_evidence_id": _require_string(lineage, "artifact_audit_evidence_id"),
        },
    }
    audit_evidence_id = _sha256_hex(canonical_json_dumps(payload))
    return {
        **payload,
        "audit_evidence_id": audit_evidence_id,
    }


def _require_matching_value(field_name: str, *values: object) -> None:
    distinct_values = {value for value in values}
    if len(distinct_values) <= 1:
        return
    raise IncomeTaxReportGenerationError(
        reason="lineage_mismatch",
        message=f"Report generation requires matching lineage field '{field_name}'.",
        details={"field_name": field_name, "values": list(values)},
    )


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxReportGenerationError(
            reason=reason,
            message="Expected JSON object input for deterministic report generation.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxReportGenerationError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxReportGenerationError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxReportGenerationError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxReportGenerationError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxReportGenerationError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
