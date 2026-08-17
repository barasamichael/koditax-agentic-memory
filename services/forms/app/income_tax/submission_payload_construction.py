"""Construct deterministic income-tax submission payloads from bound report outputs."""

from __future__ import annotations

from typing import cast
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

SUBMISSION_PAYLOAD_TYPE = "income_tax_submission_payload"
SUBMISSION_PAYLOAD_VERSION = "income_tax_submission_payload_v1"
SUPPORTED_SUBMISSION_PAYLOAD_BINDINGS: dict[tuple[str, str, int], dict[str, str]] = {
    (
        "resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): {
        "filing_profile_id": "ITX-SUBMIT-20210101-RES-EMP-V1",
        "filing_schema_id": "income_tax_submission_resident_employment_2021_01_01_v1",
    },
    (
        "non_resident_employment_income_2021_01_01",
        "KIT-VER-20210101-A",
        2021,
    ): {
        "filing_profile_id": "ITX-SUBMIT-20210101-NRES-EMP-V1",
        "filing_schema_id": "income_tax_submission_non_resident_employment_2021_01_01_v1",
    },
    (
        "resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "filing_profile_id": "ITX-SUBMIT-20230701-RES-EMP-V1",
        "filing_schema_id": "income_tax_submission_resident_employment_2023_07_01_v1",
    },
    (
        "non_resident_employment_income_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "filing_profile_id": "ITX-SUBMIT-20230701-NRES-EMP-V1",
        "filing_schema_id": "income_tax_submission_non_resident_employment_2023_07_01_v1",
    },
    (
        "resident_employment_plus_qualifying_interest_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ): {
        "filing_profile_id": "ITX-SUBMIT-20230701-RES-EMP-QINT-V1",
        "filing_schema_id": (
            "income_tax_submission_resident_employment_plus_qualifying_interest_2023_07_01_v1"
        ),
    },
}


class IncomeTaxSubmissionPayloadConstructionError(RuntimeError):
    """Represent deterministic submission-payload construction failures."""

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


def construct_income_tax_submission_payload(
    *,
    report_output: Mapping[str, object],
    report_version_binding: Mapping[str, object],
) -> dict[str, object]:
    """Construct one deterministic submission payload from report outputs."""

    report = _as_object(report_output, reason="invalid_report_output")
    binding = _as_object(report_version_binding, reason="invalid_report_binding_output")

    report_generation_status = _require_string(report, "generation_status")
    if report_generation_status != "generated":
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="invalid_report_generation_status",
            message="Submission payload construction requires a generated report output.",
            details={"generation_status": report_generation_status},
        )

    binding_status = _require_string(binding, "binding_status")
    if binding_status != "bound":
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="invalid_report_binding_status",
            message="Submission payload construction requires a bound report-version output.",
            details={"binding_status": binding_status},
        )

    report_type = _require_string(report, "report_type")
    binding_report_type = _require_string(binding, "report_type")
    if report_type != "income_tax_computation_report" or binding_report_type != report_type:
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="unsupported_report_scope",
            message="Submission payload construction supports governed income-tax reports only.",
            details={"report_type": report_type},
        )

    report_id = _require_string(report, "report_id")
    form_artifact_id = _require_string(report, "form_artifact_id")
    computation_id = _require_string(report, "computation_id")
    supported_lane_id = _require_string(report, "supported_lane_id")
    historical_version_id = _require_string(report, "historical_version_id")
    tax_year = _require_int(report, "tax_year")
    report_version = _require_string(report, "report_version")

    _require_matching_value("report_id", report_id, _require_string(binding, "report_id"))
    _require_matching_value(
        "form_artifact_id",
        form_artifact_id,
        _require_string(binding, "form_artifact_id"),
    )
    _require_matching_value(
        "computation_id",
        computation_id,
        _require_string(binding, "computation_id"),
    )
    _require_matching_value(
        "supported_lane_id",
        supported_lane_id,
        _require_string(binding, "supported_lane_id"),
    )
    _require_matching_value(
        "historical_version_id",
        historical_version_id,
        _require_string(binding, "historical_version_id"),
    )
    _require_matching_value("tax_year", tax_year, _require_int(binding, "tax_year"))
    _require_matching_value(
        "report_version",
        report_version,
        _require_string(binding, "report_version"),
    )

    binding_key = (supported_lane_id, historical_version_id, tax_year)
    filing_binding = SUPPORTED_SUBMISSION_PAYLOAD_BINDINGS.get(binding_key)
    if filing_binding is None:
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="unsupported_submission_payload_scope",
            message=(
                "No governed submission payload binding exists for this supported lane context."
            ),
            details={
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": tax_year,
            },
        )

    machine_summary = _require_object(report, "machine_usable_summary")
    liability_fields = _require_object(machine_summary, "liability_fields")
    form_fields = _require_object(machine_summary, "form_fields")
    treatment_fields = _require_object(machine_summary, "treatment_fields")
    taxpayer = _require_object(machine_summary, "taxpayer")

    report_lineage = _require_object(report, "lineage")
    binding_lineage = _require_object(binding, "binding_lineage")

    machine_usable_filing_payload: dict[str, object] = {
        "filing_header": {
            "payload_type": SUBMISSION_PAYLOAD_TYPE,
            "payload_version": SUBMISSION_PAYLOAD_VERSION,
            "filing_profile_id": filing_binding["filing_profile_id"],
            "filing_schema_id": filing_binding["filing_schema_id"],
            "report_type": report_type,
            "report_version": report_version,
            "report_version_id": _require_string(binding, "report_version_id"),
            "report_template_id": _require_string(binding, "report_template_id"),
            "supported_lane_id": supported_lane_id,
            "historical_version_id": historical_version_id,
            "tax_year": tax_year,
        },
        "taxpayer": taxpayer,
        "liability_fields": liability_fields,
        "form_fields": form_fields,
        "treatment_fields": treatment_fields,
        "filing_references": {
            "report_id": report_id,
            "form_artifact_id": form_artifact_id,
            "computation_id": computation_id,
        },
    }
    payload_content_sha256 = _sha256_hex(canonical_json_dumps(machine_usable_filing_payload))
    payload_identity = {
        "payload_type": SUBMISSION_PAYLOAD_TYPE,
        "payload_version": SUBMISSION_PAYLOAD_VERSION,
        "report_id": report_id,
        "form_artifact_id": form_artifact_id,
        "computation_id": computation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "payload_content_sha256": payload_content_sha256,
    }
    payload_id = _sha256_hex(canonical_json_dumps(payload_identity))

    payload_output: dict[str, object] = {
        "construction_status": "constructed",
        "payload_id": payload_id,
        "payload_type": SUBMISSION_PAYLOAD_TYPE,
        "payload_version": SUBMISSION_PAYLOAD_VERSION,
        "report_id": report_id,
        "form_artifact_id": form_artifact_id,
        "computation_id": computation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "machine_usable_filing_payload": machine_usable_filing_payload,
        "lineage": {
            "report_version_id": _require_string(binding, "report_version_id"),
            "report_template_id": _require_string(binding, "report_template_id"),
            "input_hash": _require_string(binding_lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(
                binding_lineage, "finalized_audit_event_id"
            ),
            "source_anchor_ids": _list_of_strings(binding_lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(binding_lineage, "applied_policy_ids"),
            "artifact_audit_evidence_id": _require_string(
                binding_lineage, "artifact_audit_evidence_id"
            ),
            "report_audit_evidence_id": _require_string(
                binding_lineage, "report_audit_evidence_id"
            ),
            "report_content_sha256": _require_string(binding_lineage, "report_content_sha256"),
            "payload_content_sha256": payload_content_sha256,
            "report_lineage": report_lineage,
        },
    }
    payload_output["audit_evidence"] = _build_submission_payload_audit_evidence(
        payload_output=payload_output
    )
    return payload_output


def _build_submission_payload_audit_evidence(
    *,
    payload_output: Mapping[str, object],
) -> dict[str, object]:
    lineage = _require_object(payload_output, "lineage")
    payload = {
        "audit_kind": "income_tax_submission_payload_construction",
        "action": "submission_payload_construction",
        "action_status": "constructed",
        "payload_id": _require_string(payload_output, "payload_id"),
        "payload_type": _require_string(payload_output, "payload_type"),
        "payload_version": _require_string(payload_output, "payload_version"),
        "report_id": _require_string(payload_output, "report_id"),
        "form_artifact_id": _require_string(payload_output, "form_artifact_id"),
        "computation_id": _require_string(payload_output, "computation_id"),
        "supported_lane_id": _require_string(payload_output, "supported_lane_id"),
        "historical_version_id": _require_string(payload_output, "historical_version_id"),
        "tax_year": _require_int(payload_output, "tax_year"),
        "lineage": {
            "input_hash": _require_string(lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
            "report_audit_evidence_id": _require_string(lineage, "report_audit_evidence_id"),
            "artifact_audit_evidence_id": _require_string(lineage, "artifact_audit_evidence_id"),
            "payload_content_sha256": _require_string(lineage, "payload_content_sha256"),
        },
    }
    return {
        **payload,
        "audit_evidence_id": _sha256_hex(canonical_json_dumps(payload)),
    }


def _require_matching_value(field_name: str, *values: object) -> None:
    distinct_values = {value for value in values}
    if len(distinct_values) <= 1:
        return
    raise IncomeTaxSubmissionPayloadConstructionError(
        reason="lineage_mismatch",
        message=f"Submission payload construction requires matching lineage field '{field_name}'.",
        details={"field_name": field_name, "values": list(values)},
    )


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason=reason,
            message="Expected JSON object input for deterministic submission payload construction.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxSubmissionPayloadConstructionError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxSubmissionPayloadConstructionError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
