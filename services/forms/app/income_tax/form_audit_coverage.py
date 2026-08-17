"""Build deterministic audit evidence for income-tax form-generation actions."""

from __future__ import annotations

from typing import cast
from typing import Literal
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

FormGenerationAction = Literal["mapping", "binding", "artifact_generation"]


class IncomeTaxFormAuditCoverageError(RuntimeError):
    """Represent deterministic form-audit coverage failures."""

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


def build_income_tax_form_mapping_audit_evidence(
    mapping_output: Mapping[str, object],
) -> dict[str, object]:
    """Build deterministic mapping-stage audit evidence from one mapping output."""

    source = _as_object(mapping_output, reason="invalid_mapping_output")
    if _require_string(source, "mapping_status") != "ok":
        raise IncomeTaxFormAuditCoverageError(
            reason="invalid_mapping_status",
            message="Mapping audit evidence requires a successful mapping output.",
        )

    computation_identity = _require_object(source, "computation_identity")
    version_identity = _require_object(source, "version_identity")
    lineage = _require_object(source, "lineage")

    payload: dict[str, object] = {
        "audit_kind": "income_tax_form_generation",
        "action": "mapping",
        "action_status": "mapped",
        "form_type": _require_string(source, "form_type"),
        "form_version": _require_string(source, "form_version"),
        "form_version_id": None,
        "supported_lane_id": _require_string(source, "supported_lane_id"),
        "historical_version_id": _require_string(version_identity, "historical_version_id"),
        "tax_year": _require_int(computation_identity, "tax_year"),
        "computation_id": _require_string(computation_identity, "computation_id"),
        "finalized_audit_event_id": _require_string(
            computation_identity, "finalized_audit_event_id"
        ),
        "artifact_id": None,
        "template_id": None,
        "content_sha256": None,
        "lineage": {
            "input_hash": _require_string(computation_identity, "input_hash"),
            "rule_version": _require_string(computation_identity, "rule_version"),
            "source_anchor_ids": _list_of_strings(version_identity, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
        },
    }
    return _with_evidence_id(payload)


def build_income_tax_form_binding_audit_evidence(
    binding_output: Mapping[str, object],
) -> dict[str, object]:
    """Build deterministic version-binding audit evidence from one binding output."""

    source = _as_object(binding_output, reason="invalid_binding_output")
    if _require_string(source, "binding_status") != "bound":
        raise IncomeTaxFormAuditCoverageError(
            reason="invalid_binding_status",
            message="Binding audit evidence requires a bound form version output.",
        )

    binding_lineage = _require_object(source, "binding_lineage")
    payload: dict[str, object] = {
        "audit_kind": "income_tax_form_generation",
        "action": "binding",
        "action_status": "bound",
        "form_type": _require_string(source, "form_type"),
        "form_version": None,
        "form_version_id": _require_string(source, "form_version_id"),
        "supported_lane_id": _require_string(source, "supported_lane_id"),
        "historical_version_id": _require_string(source, "historical_version_id"),
        "tax_year": _require_int(source, "tax_year"),
        "computation_id": _require_string(binding_lineage, "computation_id"),
        "finalized_audit_event_id": _require_string(binding_lineage, "finalized_audit_event_id"),
        "artifact_id": None,
        "template_id": _require_string(source, "template_id"),
        "content_sha256": None,
        "lineage": {
            "input_hash": _require_string(binding_lineage, "input_hash"),
            "rule_version": None,
            "source_anchor_ids": _list_of_strings(binding_lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(binding_lineage, "applied_policy_ids"),
        },
    }
    return _with_evidence_id(payload)


def build_income_tax_form_artifact_audit_evidence(
    artifact_output: Mapping[str, object],
) -> dict[str, object]:
    """Build deterministic artifact-generation audit evidence from one artifact output."""

    source = _as_object(artifact_output, reason="invalid_artifact_output")
    if _require_string(source, "generation_status") != "generated":
        raise IncomeTaxFormAuditCoverageError(
            reason="invalid_generation_status",
            message="Artifact audit evidence requires a generated artifact output.",
        )

    lineage = _require_object(source, "lineage")
    payload: dict[str, object] = {
        "audit_kind": "income_tax_form_generation",
        "action": "artifact_generation",
        "action_status": "generated",
        "form_type": _require_string(source, "form_type"),
        "form_version": None,
        "form_version_id": _require_string(source, "form_version_id"),
        "supported_lane_id": _require_string(source, "supported_lane_id"),
        "historical_version_id": _require_string(source, "historical_version_id"),
        "tax_year": _require_int(source, "tax_year"),
        "computation_id": _require_string(source, "computation_id"),
        "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
        "artifact_id": _require_string(source, "artifact_id"),
        "template_id": _require_string(source, "template_id"),
        "content_sha256": _require_string(source, "content_sha256"),
        "lineage": {
            "input_hash": _require_string(lineage, "input_hash"),
            "rule_version": None,
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
        },
    }
    return _with_evidence_id(payload)


def build_income_tax_form_failure_audit_evidence(
    *,
    action: FormGenerationAction,
    error_reason: str,
    error_message: str,
    error_details: Mapping[str, object] | None = None,
    finalized_output: Mapping[str, object] | None = None,
    form_ready_output: Mapping[str, object] | None = None,
    form_version_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build deterministic failed-action audit evidence for form-generation failures."""

    if not error_reason.strip():
        raise IncomeTaxFormAuditCoverageError(
            reason="missing_error_reason",
            message="Failure audit evidence requires a non-empty error reason.",
        )
    if not error_message.strip():
        raise IncomeTaxFormAuditCoverageError(
            reason="missing_error_message",
            message="Failure audit evidence requires a non-empty error message.",
        )

    context = _resolve_failure_context(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )
    payload: dict[str, object] = {
        "audit_kind": "income_tax_form_generation",
        "action": action,
        "action_status": "failed",
        "form_type": context["form_type"],
        "form_version": context["form_version"],
        "form_version_id": context["form_version_id"],
        "supported_lane_id": context["supported_lane_id"],
        "historical_version_id": context["historical_version_id"],
        "tax_year": context["tax_year"],
        "computation_id": context["computation_id"],
        "finalized_audit_event_id": context["finalized_audit_event_id"],
        "artifact_id": context["artifact_id"],
        "template_id": context["template_id"],
        "content_sha256": context["content_sha256"],
        "lineage": {
            "input_hash": context["input_hash"],
            "rule_version": context["rule_version"],
            "source_anchor_ids": context["source_anchor_ids"],
            "applied_policy_ids": context["applied_policy_ids"],
        },
        "error": {
            "reason": error_reason,
            "message": error_message,
            "details": (
                _as_object(error_details, reason="invalid_error_details")
                if error_details is not None
                else {}
            ),
        },
    }
    return _with_evidence_id(payload)


def _resolve_failure_context(
    *,
    finalized_output: Mapping[str, object] | None,
    form_ready_output: Mapping[str, object] | None,
    form_version_binding: Mapping[str, object] | None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "form_type": None,
        "form_version": None,
        "form_version_id": None,
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
        "computation_id": None,
        "finalized_audit_event_id": None,
        "artifact_id": None,
        "template_id": None,
        "content_sha256": None,
        "input_hash": None,
        "rule_version": None,
        "source_anchor_ids": [],
        "applied_policy_ids": [],
    }

    if finalized_output is not None:
        finalized = _as_object(finalized_output, reason="invalid_finalized_output")
        context["computation_id"] = _optional_string(finalized, "computation_id")
        context["finalized_audit_event_id"] = _optional_string(
            finalized, "finalized_audit_event_id"
        )
        context["tax_year"] = _optional_int(finalized, "tax_year")
        context["input_hash"] = _optional_string(finalized, "input_hash")
        context["rule_version"] = _optional_string(finalized, "rule_version")
        finalized_payload = _optional_object(finalized, "result_payload")
        if finalized_payload is not None:
            version_identity = _optional_object(finalized_payload, "version_identity")
            if version_identity is not None:
                context["historical_version_id"] = _optional_string(
                    version_identity, "historical_version_id"
                )
                context["source_anchor_ids"] = _optional_list_of_strings(
                    version_identity, "source_anchor_ids"
                )

    if form_ready_output is not None:
        mapped = _as_object(form_ready_output, reason="invalid_form_ready_output")
        context["form_type"] = _optional_string(mapped, "form_type") or context["form_type"]
        context["form_version"] = (
            _optional_string(mapped, "form_version") or context["form_version"]
        )
        context["supported_lane_id"] = (
            _optional_string(mapped, "supported_lane_id") or context["supported_lane_id"]
        )
        mapped_identity = _optional_object(mapped, "computation_identity")
        if mapped_identity is not None:
            context["computation_id"] = (
                _optional_string(mapped_identity, "computation_id") or context["computation_id"]
            )
            context["finalized_audit_event_id"] = (
                _optional_string(mapped_identity, "finalized_audit_event_id")
                or context["finalized_audit_event_id"]
            )
            context["tax_year"] = _optional_int(mapped_identity, "tax_year") or context["tax_year"]
            context["input_hash"] = (
                _optional_string(mapped_identity, "input_hash") or context["input_hash"]
            )
            context["rule_version"] = (
                _optional_string(mapped_identity, "rule_version") or context["rule_version"]
            )
        mapped_version = _optional_object(mapped, "version_identity")
        if mapped_version is not None:
            context["historical_version_id"] = (
                _optional_string(mapped_version, "historical_version_id")
                or context["historical_version_id"]
            )
            context["source_anchor_ids"] = (
                _optional_list_of_strings(mapped_version, "source_anchor_ids")
                or context["source_anchor_ids"]
            )
        mapped_lineage = _optional_object(mapped, "lineage")
        if mapped_lineage is not None:
            context["applied_policy_ids"] = (
                _optional_list_of_strings(mapped_lineage, "applied_policy_ids")
                or context["applied_policy_ids"]
            )

    if form_version_binding is not None:
        binding = _as_object(form_version_binding, reason="invalid_form_version_binding")
        context["form_type"] = _optional_string(binding, "form_type") or context["form_type"]
        context["form_version_id"] = (
            _optional_string(binding, "form_version_id") or context["form_version_id"]
        )
        context["supported_lane_id"] = (
            _optional_string(binding, "supported_lane_id") or context["supported_lane_id"]
        )
        context["historical_version_id"] = (
            _optional_string(binding, "historical_version_id") or context["historical_version_id"]
        )
        context["tax_year"] = _optional_int(binding, "tax_year") or context["tax_year"]
        context["template_id"] = _optional_string(binding, "template_id") or context["template_id"]
        binding_lineage = _optional_object(binding, "binding_lineage")
        if binding_lineage is not None:
            context["computation_id"] = (
                _optional_string(binding_lineage, "computation_id") or context["computation_id"]
            )
            context["finalized_audit_event_id"] = (
                _optional_string(binding_lineage, "finalized_audit_event_id")
                or context["finalized_audit_event_id"]
            )
            context["input_hash"] = (
                _optional_string(binding_lineage, "input_hash") or context["input_hash"]
            )
            context["source_anchor_ids"] = (
                _optional_list_of_strings(binding_lineage, "source_anchor_ids")
                or context["source_anchor_ids"]
            )
            context["applied_policy_ids"] = (
                _optional_list_of_strings(binding_lineage, "applied_policy_ids")
                or context["applied_policy_ids"]
            )

    return context


def _with_evidence_id(payload: Mapping[str, object]) -> dict[str, object]:
    normalized_payload = _as_object(payload, reason="invalid_audit_payload")
    evidence_id = hashlib.sha256(
        canonical_json_dumps(normalized_payload).encode("utf-8")
    ).hexdigest()
    return {**normalized_payload, "audit_evidence_id": evidence_id}


def _as_object(
    value: Mapping[str, object] | Mapping[object, object] | object,
    *,
    reason: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxFormAuditCoverageError(
            reason=reason,
            message="Expected JSON object input for deterministic form audit coverage.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxFormAuditCoverageError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _optional_object(source: Mapping[str, object], field_name: str) -> dict[str, object] | None:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        return None
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxFormAuditCoverageError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _optional_string(source: Mapping[str, object], field_name: str) -> str | None:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxFormAuditCoverageError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _optional_int(source: Mapping[str, object], field_name: str) -> int | None:
    value = source.get(field_name)
    if not isinstance(value, int):
        return None
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxFormAuditCoverageError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(list[object], value)
    result: list[str] = []
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxFormAuditCoverageError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        result.append(item)
    return result


def _optional_list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        return []
    typed_value = cast(list[object], value)
    result: list[str] = []
    for item in typed_value:
        if isinstance(item, str) and item.strip():
            result.append(item)
    return result
