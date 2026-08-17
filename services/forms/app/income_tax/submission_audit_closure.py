"""Close income-tax submission workflows with deterministic audit and immutability rules."""

from __future__ import annotations

from typing import cast
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.income_tax.submission_workflow import WORKFLOW_TYPE
from services.forms.app.income_tax.submission_workflow import WORKFLOW_VERSION
from services.forms.app.income_tax.submission_workflow import SUPPORTED_WORKFLOW_CONTEXTS
from services.forms.app.income_tax.submission_payload_construction import SUBMISSION_PAYLOAD_TYPE
from services.forms.app.income_tax.submission_payload_construction import SUBMISSION_PAYLOAD_VERSION

CLOSURE_TYPE = "income_tax_submission_audit_closure"
CLOSURE_VERSION = "income_tax_submission_audit_closure_v1"
CLOSURE_STATUS = "closed_internal"
FINAL_INTERNAL_STATUS = "submitted_internal"
EXTERNAL_CONFIRMATION_STATUS = "not_available_in_scope"
IMMUTABLE_IDENTITY_FIELDS = (
    "closure_record_id",
    "workflow_record_id",
    "submission_payload_id",
    "report_id",
    "form_artifact_id",
    "computation_id",
    "supported_lane_id",
    "historical_version_id",
    "tax_year",
    "final_internal_status",
)


class IncomeTaxSubmissionAuditClosureError(RuntimeError):
    """Represent deterministic submission-closure failures."""

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


def close_income_tax_submission_workflow(
    *,
    workflow_record: Mapping[str, object],
) -> dict[str, object]:
    """Close one submission workflow into a deterministic immutable audit record."""

    workflow = _as_object(workflow_record, reason="invalid_workflow_record")
    _validate_workflow_record_for_closure(workflow)

    status_history = _list_of_objects(workflow, "status_history")
    latest_transition = status_history[-1]
    latest_transition_id = _require_string(latest_transition, "transition_id")
    latest_transition_reason = _require_string(latest_transition, "transition_reason")

    lineage = _require_object(workflow, "lineage")
    workflow_audit_evidence = _require_object(workflow, "audit_evidence")

    closure_identity = {
        "closure_type": CLOSURE_TYPE,
        "closure_version": CLOSURE_VERSION,
        "workflow_record_id": _require_string(workflow, "workflow_record_id"),
        "submission_payload_id": _require_string(workflow, "submission_payload_id"),
        "report_id": _require_string(workflow, "report_id"),
        "form_artifact_id": _require_string(workflow, "form_artifact_id"),
        "computation_id": _require_string(workflow, "computation_id"),
        "supported_lane_id": _require_string(workflow, "supported_lane_id"),
        "historical_version_id": _require_string(workflow, "historical_version_id"),
        "tax_year": _require_int(workflow, "tax_year"),
        "final_internal_status": FINAL_INTERNAL_STATUS,
        "latest_transition_id": latest_transition_id,
    }
    closure_record_id = _sha256_hex(canonical_json_dumps(closure_identity))

    closure_output: dict[str, object] = {
        "closure_status": CLOSURE_STATUS,
        "closure_record_id": closure_record_id,
        "closure_type": CLOSURE_TYPE,
        "closure_version": CLOSURE_VERSION,
        "workflow_record_id": _require_string(workflow, "workflow_record_id"),
        "submission_payload_id": _require_string(workflow, "submission_payload_id"),
        "report_id": _require_string(workflow, "report_id"),
        "form_artifact_id": _require_string(workflow, "form_artifact_id"),
        "computation_id": _require_string(workflow, "computation_id"),
        "supported_lane_id": _require_string(workflow, "supported_lane_id"),
        "historical_version_id": _require_string(workflow, "historical_version_id"),
        "tax_year": _require_int(workflow, "tax_year"),
        "final_internal_status": FINAL_INTERNAL_STATUS,
        "external_confirmation_status": EXTERNAL_CONFIRMATION_STATUS,
        "immutable_identity_fields": {
            "closure_record_id": closure_record_id,
            "workflow_record_id": _require_string(workflow, "workflow_record_id"),
            "submission_payload_id": _require_string(workflow, "submission_payload_id"),
            "report_id": _require_string(workflow, "report_id"),
            "form_artifact_id": _require_string(workflow, "form_artifact_id"),
            "computation_id": _require_string(workflow, "computation_id"),
            "supported_lane_id": _require_string(workflow, "supported_lane_id"),
            "historical_version_id": _require_string(workflow, "historical_version_id"),
            "tax_year": _require_int(workflow, "tax_year"),
            "final_internal_status": FINAL_INTERNAL_STATUS,
        },
        "lineage": {
            "workflow_type": _require_string(workflow, "workflow_type"),
            "workflow_version": _require_string(workflow, "workflow_version"),
            "payload_type": _require_string(lineage, "payload_type"),
            "payload_version": _require_string(lineage, "payload_version"),
            "input_hash": _require_string(lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
            "artifact_audit_evidence_id": _require_string(lineage, "artifact_audit_evidence_id"),
            "report_audit_evidence_id": _require_string(lineage, "report_audit_evidence_id"),
            "payload_audit_evidence_id": _require_string(lineage, "payload_audit_evidence_id"),
            "workflow_audit_evidence_id": _require_string(
                workflow_audit_evidence, "audit_evidence_id"
            ),
            "payload_content_sha256": _require_string(lineage, "payload_content_sha256"),
            "latest_transition_id": latest_transition_id,
            "latest_transition_reason": latest_transition_reason,
        },
    }
    closure_output["closure_audit_evidence"] = _build_submission_closure_audit_evidence(
        closure_output=closure_output
    )
    return closure_output


def enforce_income_tax_submission_closure_immutability(
    *,
    baseline_closure_output: Mapping[str, object],
    candidate_closure_output: Mapping[str, object],
) -> dict[str, object]:
    """Enforce immutable identity fields for one already-closed submission output."""

    baseline = _as_object(baseline_closure_output, reason="invalid_baseline_closure_output")
    candidate = _as_object(candidate_closure_output, reason="invalid_candidate_closure_output")

    _validate_closure_record(baseline)
    _validate_closure_record(candidate)

    mutated_fields: list[str] = []
    for field_name in IMMUTABLE_IDENTITY_FIELDS:
        if baseline.get(field_name) != candidate.get(field_name):
            mutated_fields.append(field_name)

    baseline_immutable = _require_object(baseline, "immutable_identity_fields")
    candidate_immutable = _require_object(candidate, "immutable_identity_fields")
    if baseline_immutable != candidate_immutable:
        mutated_fields.append("immutable_identity_fields")

    if mutated_fields:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="illegal_post_closure_mutation",
            message="Closed submission artifacts cannot mutate immutable identity fields.",
            details={"mutated_fields": sorted(set(mutated_fields))},
        )

    return candidate


def _build_submission_closure_audit_evidence(
    *,
    closure_output: Mapping[str, object],
) -> dict[str, object]:
    lineage = _require_object(closure_output, "lineage")
    payload = {
        "audit_kind": "income_tax_submission_closure",
        "action": "submission_workflow_closure",
        "action_status": CLOSURE_STATUS,
        "closure_record_id": _require_string(closure_output, "closure_record_id"),
        "workflow_record_id": _require_string(closure_output, "workflow_record_id"),
        "submission_payload_id": _require_string(closure_output, "submission_payload_id"),
        "report_id": _require_string(closure_output, "report_id"),
        "form_artifact_id": _require_string(closure_output, "form_artifact_id"),
        "computation_id": _require_string(closure_output, "computation_id"),
        "supported_lane_id": _require_string(closure_output, "supported_lane_id"),
        "historical_version_id": _require_string(closure_output, "historical_version_id"),
        "tax_year": _require_int(closure_output, "tax_year"),
        "final_internal_status": _require_string(closure_output, "final_internal_status"),
        "external_confirmation_status": _require_string(
            closure_output, "external_confirmation_status"
        ),
        "lineage": {
            "input_hash": _require_string(lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
            "artifact_audit_evidence_id": _require_string(lineage, "artifact_audit_evidence_id"),
            "report_audit_evidence_id": _require_string(lineage, "report_audit_evidence_id"),
            "payload_audit_evidence_id": _require_string(lineage, "payload_audit_evidence_id"),
            "workflow_audit_evidence_id": _require_string(lineage, "workflow_audit_evidence_id"),
        },
    }
    return {
        **payload,
        "audit_evidence_id": _sha256_hex(canonical_json_dumps(payload)),
    }


def _validate_workflow_record_for_closure(workflow_record: Mapping[str, object]) -> None:
    workflow_type = _require_string(workflow_record, "workflow_type")
    workflow_version = _require_string(workflow_record, "workflow_version")
    if workflow_type != WORKFLOW_TYPE or workflow_version != WORKFLOW_VERSION:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_workflow_record",
            message="Closure supports governed income-tax workflow records only.",
            details={"workflow_type": workflow_type, "workflow_version": workflow_version},
        )

    workflow_context = (
        _require_string(workflow_record, "supported_lane_id"),
        _require_string(workflow_record, "historical_version_id"),
        _require_int(workflow_record, "tax_year"),
    )
    if workflow_context not in SUPPORTED_WORKFLOW_CONTEXTS:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_closure_scope",
            message="No governed submission closure exists for this workflow context.",
            details={
                "supported_lane_id": workflow_context[0],
                "historical_version_id": workflow_context[1],
                "tax_year": workflow_context[2],
            },
        )

    current_status = _require_string(workflow_record, "current_status")
    if current_status != FINAL_INTERNAL_STATUS:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="workflow_not_ready_for_closure",
            message="Submission closure requires terminal internal workflow status.",
            details={
                "current_status": current_status,
                "required_status": FINAL_INTERNAL_STATUS,
            },
        )

    status_history = _list_of_objects(workflow_record, "status_history")
    latest_transition = status_history[-1]
    latest_transition_status = _require_string(latest_transition, "to_status")
    if latest_transition_status != FINAL_INTERNAL_STATUS:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="invalid_workflow_history",
            message="Workflow history does not reflect a terminal internal submission state.",
            details={"latest_transition_status": latest_transition_status},
        )

    _require_string(workflow_record, "workflow_record_id")
    _require_string(workflow_record, "submission_payload_id")
    _require_string(workflow_record, "report_id")
    _require_string(workflow_record, "form_artifact_id")
    _require_string(workflow_record, "computation_id")

    lineage = _require_object(workflow_record, "lineage")
    if _require_string(lineage, "payload_type") != SUBMISSION_PAYLOAD_TYPE:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_workflow_lineage",
            message="Submission closure requires income-tax submission payload lineage.",
        )
    if _require_string(lineage, "payload_version") != SUBMISSION_PAYLOAD_VERSION:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_workflow_lineage",
            message="Submission closure requires the governed payload version lineage.",
        )
    _require_string(lineage, "input_hash")
    _require_string(lineage, "finalized_audit_event_id")
    _list_of_strings(lineage, "source_anchor_ids")
    _list_of_strings(lineage, "applied_policy_ids")
    _require_string(lineage, "artifact_audit_evidence_id")
    _require_string(lineage, "report_audit_evidence_id")
    _require_string(lineage, "payload_audit_evidence_id")
    _require_string(lineage, "payload_content_sha256")

    workflow_audit_evidence = _require_object(workflow_record, "audit_evidence")
    if _require_string(workflow_audit_evidence, "action") != "submission_workflow_transition":
        raise IncomeTaxSubmissionAuditClosureError(
            reason="invalid_workflow_audit_evidence",
            message="Submission closure requires workflow-transition audit evidence lineage.",
        )
    _require_string(workflow_audit_evidence, "audit_evidence_id")


def _validate_closure_record(closure_record: Mapping[str, object]) -> None:
    if _require_string(closure_record, "closure_type") != CLOSURE_TYPE:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_closure_record",
            message="Closure immutability supports governed income-tax closure records only.",
        )
    if _require_string(closure_record, "closure_version") != CLOSURE_VERSION:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_closure_record",
            message="Closure immutability requires the governed closure version.",
        )
    if _require_string(closure_record, "closure_status") != CLOSURE_STATUS:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="invalid_closure_status",
            message="Closure immutability requires closed internal submission records.",
        )
    if _require_string(closure_record, "final_internal_status") != FINAL_INTERNAL_STATUS:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="invalid_closure_status",
            message="Closure immutability requires terminal internal status lineage.",
        )
    if _require_string(closure_record, "external_confirmation_status") != (
        EXTERNAL_CONFIRMATION_STATUS
    ):
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_external_confirmation_status",
            message="Closure records cannot imply unsupported external filing confirmations.",
        )

    closure_context = (
        _require_string(closure_record, "supported_lane_id"),
        _require_string(closure_record, "historical_version_id"),
        _require_int(closure_record, "tax_year"),
    )
    if closure_context not in SUPPORTED_WORKFLOW_CONTEXTS:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="unsupported_closure_scope",
            message="No governed submission closure exists for this closure context.",
            details={
                "supported_lane_id": closure_context[0],
                "historical_version_id": closure_context[1],
                "tax_year": closure_context[2],
            },
        )

    immutable_fields = _require_object(closure_record, "immutable_identity_fields")
    for field_name in IMMUTABLE_IDENTITY_FIELDS:
        if immutable_fields.get(field_name) != closure_record.get(field_name):
            raise IncomeTaxSubmissionAuditClosureError(
                reason="invalid_immutable_identity_fields",
                message="Closure immutable identity fields must match top-level closure identity.",
                details={"field_name": field_name},
            )

    closure_audit_evidence = _require_object(closure_record, "closure_audit_evidence")
    if _require_string(closure_audit_evidence, "action") != "submission_workflow_closure":
        raise IncomeTaxSubmissionAuditClosureError(
            reason="invalid_closure_audit_evidence",
            message="Closure records require closure-stage audit evidence.",
        )
    _require_string(closure_audit_evidence, "audit_evidence_id")


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxSubmissionAuditClosureError(
            reason=reason,
            message="Expected JSON object input for deterministic submission closure handling.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxSubmissionAuditClosureError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxSubmissionAuditClosureError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxSubmissionAuditClosureError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxSubmissionAuditClosureError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxSubmissionAuditClosureError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings


def _list_of_objects(source: Mapping[str, object], field_name: str) -> list[dict[str, object]]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxSubmissionAuditClosureError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    objects: list[dict[str, object]] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, Mapping):
            raise IncomeTaxSubmissionAuditClosureError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only objects.",
                details={"field_name": field_name},
            )
        typed_item = cast(Mapping[object, object], item)
        objects.append({str(key): typed_item[key] for key in typed_item})
    if not objects:
        raise IncomeTaxSubmissionAuditClosureError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return objects


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
