"""Generate immutable income-tax form artifacts for supported finalized lanes."""

from __future__ import annotations

from typing import cast
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.income_tax.form_mapping import FORM_TYPE
from services.forms.app.income_tax.form_audit_coverage import (
    build_income_tax_form_artifact_audit_evidence,
)


class IncomeTaxFormArtifactGenerationError(RuntimeError):
    """Represent deterministic form-artifact generation failures."""

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


def generate_income_tax_form_artifact(
    *,
    finalized_output: Mapping[str, object],
    form_ready_output: Mapping[str, object],
    form_version_binding: Mapping[str, object],
) -> dict[str, object]:
    """Generate one immutable form artifact from finalized supported upstream outputs."""

    finalized = _as_object(finalized_output, reason="invalid_finalized_output")
    mapped = _as_object(form_ready_output, reason="invalid_form_ready_output")
    binding = _as_object(form_version_binding, reason="invalid_form_version_binding")

    finalization_status = _require_string(finalized, "finalization_status")
    if finalization_status != "finalized":
        raise IncomeTaxFormArtifactGenerationError(
            reason="computation_not_finalized",
            message="Form artifacts may only be generated from finalized computations.",
            details={"finalization_status": finalization_status},
        )

    tax_type = _require_string(finalized, "tax_type")
    regime_type = _require_string(finalized, "regime_type")
    if tax_type != "income_tax" or regime_type != "income_tax":
        raise IncomeTaxFormArtifactGenerationError(
            reason="unsupported_tax_type",
            message="Form artifact generation supports governed income-tax outputs only.",
            details={"tax_type": tax_type, "regime_type": regime_type},
        )

    if _require_string(mapped, "mapping_status") != "ok":
        raise IncomeTaxFormArtifactGenerationError(
            reason="invalid_mapping_status",
            message="Form artifact generation requires a successful form mapping result.",
            details={"mapping_status": mapped.get("mapping_status")},
        )
    if _require_string(binding, "binding_status") != "bound":
        raise IncomeTaxFormArtifactGenerationError(
            reason="invalid_binding_status",
            message="Form artifact generation requires a bound form version.",
            details={"binding_status": binding.get("binding_status")},
        )

    computation_id = _require_string(finalized, "computation_id")
    tax_year = _require_int(finalized, "tax_year")
    input_hash = _require_string(finalized, "input_hash")
    finalized_at = _require_string(finalized, "finalized_at")
    finalized_audit_event_id = _require_string(finalized, "finalized_audit_event_id")
    finalized_result_payload = _require_object(finalized, "result_payload")
    finalized_version_identity = _require_object(finalized_result_payload, "version_identity")

    mapped_identity = _require_object(mapped, "computation_identity")
    mapped_version = _require_object(mapped, "version_identity")
    mapped_taxpayer = _require_object(mapped, "taxpayer")
    mapped_lineage = _require_object(mapped, "lineage")
    binding_lineage = _require_object(binding, "binding_lineage")

    _require_matching_value(
        "computation_id",
        computation_id,
        _require_string(mapped_identity, "computation_id"),
        _require_string(binding_lineage, "computation_id"),
    )
    _require_matching_value(
        "finalized_audit_event_id",
        finalized_audit_event_id,
        _require_string(mapped_identity, "finalized_audit_event_id"),
        _require_string(binding_lineage, "finalized_audit_event_id"),
    )
    _require_matching_value(
        "tax_year",
        tax_year,
        _require_int(mapped_identity, "tax_year"),
        _require_int(binding, "tax_year"),
    )
    _require_matching_value(
        "historical_version_id",
        _require_string(finalized_version_identity, "historical_version_id"),
        _require_string(mapped_version, "historical_version_id"),
        _require_string(binding, "historical_version_id"),
    )
    _require_matching_value(
        "supported_lane_id",
        _require_string(mapped, "supported_lane_id"),
        _require_string(binding, "supported_lane_id"),
    )
    _require_matching_value(
        "form_type",
        _require_string(mapped, "form_type"),
        _require_string(binding, "form_type"),
        FORM_TYPE,
    )
    _require_matching_value(
        "input_hash",
        input_hash,
        _require_string(mapped_identity, "input_hash"),
        _require_string(binding_lineage, "input_hash"),
    )

    if _require_list(mapped, "unsupported_fields"):
        raise IncomeTaxFormArtifactGenerationError(
            reason="unsupported_mapped_fields",
            message=(
                "Form artifact generation requires form-ready outputs with no unsupported fields."
            ),
            details={"unsupported_fields": mapped["unsupported_fields"]},
        )

    generated_content_payload = {
        "header": {
            "form_type": _require_string(binding, "form_type"),
            "form_version_id": _require_string(binding, "form_version_id"),
            "template_id": _require_string(binding, "template_id"),
            "supported_lane_id": _require_string(binding, "supported_lane_id"),
            "historical_version_id": _require_string(binding, "historical_version_id"),
            "tax_year": _require_int(binding, "tax_year"),
        },
        "taxpayer": mapped_taxpayer,
        "liability_fields": _require_object(mapped, "liability_fields"),
        "domain_fields": _require_object(mapped, "domain_fields"),
        "form_fields": _require_object(mapped, "form_fields"),
        "impact_fields": _require_object(mapped, "impact_fields"),
        "treatment_fields": _require_object(mapped, "treatment_fields"),
    }
    content_sha256 = _sha256_hex(canonical_json_dumps(generated_content_payload))
    artifact_identity_payload = {
        "form_type": _require_string(binding, "form_type"),
        "form_version_id": _require_string(binding, "form_version_id"),
        "supported_lane_id": _require_string(binding, "supported_lane_id"),
        "historical_version_id": _require_string(binding, "historical_version_id"),
        "computation_id": computation_id,
        "tax_year": tax_year,
        "content_sha256": content_sha256,
    }
    artifact_id = _sha256_hex(canonical_json_dumps(artifact_identity_payload))

    artifact_output: dict[str, object] = {
        "generation_status": "generated",
        "artifact_id": artifact_id,
        "artifact_type": "income_tax_form_artifact",
        "form_type": _require_string(binding, "form_type"),
        "form_version_id": _require_string(binding, "form_version_id"),
        "template_id": _require_string(binding, "template_id"),
        "supported_lane_id": _require_string(binding, "supported_lane_id"),
        "historical_version_id": _require_string(binding, "historical_version_id"),
        "computation_id": computation_id,
        "tax_year": tax_year,
        "content_sha256": content_sha256,
        "generated_content_payload": generated_content_payload,
        "lineage": {
            "input_hash": input_hash,
            "finalization_status": finalization_status,
            "finalized_at": finalized_at,
            "finalized_audit_event_id": finalized_audit_event_id,
            "source_anchor_ids": _list_of_strings(mapped_version, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(mapped_lineage, "applied_policy_ids"),
            "validation_focus_domains": _list_of_strings(
                mapped_lineage, "validation_focus_domains"
            ),
            "binding_lineage": binding_lineage,
        },
    }
    artifact_output["audit_evidence"] = build_income_tax_form_artifact_audit_evidence(
        artifact_output
    )
    return artifact_output


def _require_matching_value(field_name: str, *values: object) -> None:
    distinct_values = {value for value in values}
    if len(distinct_values) <= 1:
        return
    raise IncomeTaxFormArtifactGenerationError(
        reason="lineage_mismatch",
        message=f"Form artifact generation requires matching lineage field '{field_name}'.",
        details={"field_name": field_name, "values": list(values)},
    )


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxFormArtifactGenerationError(
            reason=reason,
            message="Expected JSON object input for deterministic form artifact generation.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxFormArtifactGenerationError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxFormArtifactGenerationError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxFormArtifactGenerationError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_list(source: Mapping[str, object], field_name: str) -> list[object]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxFormArtifactGenerationError(
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
            raise IncomeTaxFormArtifactGenerationError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(value)
    return strings


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
