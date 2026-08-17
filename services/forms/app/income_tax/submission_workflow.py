"""Manage deterministic internal workflow states for income-tax submission payloads."""

from __future__ import annotations

from typing import cast
from typing import Literal
import hashlib
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.income_tax.submission_payload_construction import SUBMISSION_PAYLOAD_TYPE
from services.forms.app.income_tax.submission_payload_construction import SUBMISSION_PAYLOAD_VERSION

SubmissionWorkflowStatus = Literal["prepared", "ready_for_submission", "submitted_internal"]
WORKFLOW_TYPE = "income_tax_submission_workflow"
WORKFLOW_VERSION = "income_tax_submission_workflow_v1"
SUPPORTED_WORKFLOW_CONTEXTS = {
    ("resident_employment_income_2021_01_01", "KIT-VER-20210101-A", 2021),
    ("non_resident_employment_income_2021_01_01", "KIT-VER-20210101-A", 2021),
    ("resident_employment_income_2023_07_01", "KIT-VER-20230701-A", 2023),
    ("non_resident_employment_income_2023_07_01", "KIT-VER-20230701-A", 2023),
    (
        "resident_employment_plus_qualifying_interest_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ),
}
ALLOWED_TRANSITIONS: dict[SubmissionWorkflowStatus, set[SubmissionWorkflowStatus]] = {
    "prepared": {"ready_for_submission"},
    "ready_for_submission": {"submitted_internal"},
    "submitted_internal": set(),
}
DEFAULT_TRANSITION_REASONS: dict[tuple[SubmissionWorkflowStatus, SubmissionWorkflowStatus], str] = {
    ("prepared", "ready_for_submission"): "lineage_validated",
    ("ready_for_submission", "submitted_internal"): "internal_submission_recorded",
}


class IncomeTaxSubmissionWorkflowError(RuntimeError):
    """Represent deterministic submission-workflow failures."""

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


def initialize_income_tax_submission_workflow(
    *,
    submission_payload_output: Mapping[str, object],
) -> dict[str, object]:
    """Initialize one deterministic internal submission workflow from one payload output."""

    payload = _as_object(submission_payload_output, reason="invalid_submission_payload_output")
    _validate_payload_for_workflow(payload)

    submission_payload_id = _require_string(payload, "payload_id")
    report_id = _require_string(payload, "report_id")
    form_artifact_id = _require_string(payload, "form_artifact_id")
    computation_id = _require_string(payload, "computation_id")
    supported_lane_id = _require_string(payload, "supported_lane_id")
    historical_version_id = _require_string(payload, "historical_version_id")
    tax_year = _require_int(payload, "tax_year")

    workflow_record_identity = {
        "workflow_type": WORKFLOW_TYPE,
        "workflow_version": WORKFLOW_VERSION,
        "submission_payload_id": submission_payload_id,
        "report_id": report_id,
        "form_artifact_id": form_artifact_id,
        "computation_id": computation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
    }
    workflow_record_id = _sha256_hex(canonical_json_dumps(workflow_record_identity))
    transition = _build_transition_entry(
        workflow_record_id=workflow_record_id,
        transition_index=1,
        from_status=None,
        to_status="prepared",
        transition_reason="workflow_initialized",
    )

    payload_lineage = _require_object(payload, "lineage")
    payload_audit_evidence = _require_object(payload, "audit_evidence")
    workflow_record: dict[str, object] = {
        "workflow_record_id": workflow_record_id,
        "workflow_type": WORKFLOW_TYPE,
        "workflow_version": WORKFLOW_VERSION,
        "submission_payload_id": submission_payload_id,
        "report_id": report_id,
        "form_artifact_id": form_artifact_id,
        "computation_id": computation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "current_status": "prepared",
        "status_history": [transition],
        "lineage": {
            "payload_type": _require_string(payload, "payload_type"),
            "payload_version": _require_string(payload, "payload_version"),
            "input_hash": _require_string(payload_lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(
                payload_lineage, "finalized_audit_event_id"
            ),
            "source_anchor_ids": _list_of_strings(payload_lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(payload_lineage, "applied_policy_ids"),
            "artifact_audit_evidence_id": _require_string(
                payload_lineage, "artifact_audit_evidence_id"
            ),
            "report_audit_evidence_id": _require_string(
                payload_lineage, "report_audit_evidence_id"
            ),
            "payload_audit_evidence_id": _require_string(
                payload_audit_evidence, "audit_evidence_id"
            ),
            "payload_content_sha256": _require_string(payload_lineage, "payload_content_sha256"),
        },
    }
    workflow_record["audit_evidence"] = _build_workflow_audit_evidence(workflow_record)
    return workflow_record


def advance_income_tax_submission_workflow(
    *,
    workflow_record: Mapping[str, object],
    target_status: str,
) -> dict[str, object]:
    """Advance one deterministic workflow record to one supported internal status."""

    source = _as_object(workflow_record, reason="invalid_workflow_record")
    current_status = _parse_workflow_status(_require_string(source, "current_status"))
    normalized_target_status = _parse_workflow_status(target_status)

    _validate_workflow_record(source)
    if normalized_target_status == current_status:
        return source

    allowed_targets = ALLOWED_TRANSITIONS[current_status]
    if normalized_target_status not in allowed_targets:
        raise IncomeTaxSubmissionWorkflowError(
            reason="invalid_status_transition",
            message="Workflow transition is not allowed for the current status.",
            details={
                "current_status": current_status,
                "target_status": normalized_target_status,
            },
        )

    history = _list_of_objects(source, "status_history")
    workflow_record_id = _require_string(source, "workflow_record_id")
    transition_index = len(history) + 1
    transition_reason = DEFAULT_TRANSITION_REASONS[(current_status, normalized_target_status)]
    transition = _build_transition_entry(
        workflow_record_id=workflow_record_id,
        transition_index=transition_index,
        from_status=current_status,
        to_status=normalized_target_status,
        transition_reason=transition_reason,
    )

    advanced_record: dict[str, object] = {
        **source,
        "current_status": normalized_target_status,
        "status_history": [*history, transition],
    }
    advanced_record["audit_evidence"] = _build_workflow_audit_evidence(advanced_record)
    return advanced_record


def _validate_payload_for_workflow(payload: Mapping[str, object]) -> None:
    if _require_string(payload, "construction_status") != "constructed":
        raise IncomeTaxSubmissionWorkflowError(
            reason="invalid_payload_construction_status",
            message="Workflow initialization requires a constructed submission payload.",
        )
    if _require_string(payload, "payload_type") != SUBMISSION_PAYLOAD_TYPE:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_submission_payload_type",
            message="Workflow initialization supports governed income-tax payloads only.",
        )
    if _require_string(payload, "payload_version") != SUBMISSION_PAYLOAD_VERSION:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_submission_payload_version",
            message="Workflow initialization requires the governed payload version.",
        )
    payload_context = (
        _require_string(payload, "supported_lane_id"),
        _require_string(payload, "historical_version_id"),
        _require_int(payload, "tax_year"),
    )
    if payload_context not in SUPPORTED_WORKFLOW_CONTEXTS:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_workflow_scope",
            message="No governed submission workflow exists for this payload context.",
            details={
                "supported_lane_id": payload_context[0],
                "historical_version_id": payload_context[1],
                "tax_year": payload_context[2],
            },
        )
    payload_audit = _require_object(payload, "audit_evidence")
    if _require_string(payload_audit, "action") != "submission_payload_construction":
        raise IncomeTaxSubmissionWorkflowError(
            reason="invalid_payload_audit_evidence",
            message="Workflow initialization requires submission-payload audit evidence lineage.",
        )


def _validate_workflow_record(workflow_record: Mapping[str, object]) -> None:
    workflow_type = _require_string(workflow_record, "workflow_type")
    workflow_version = _require_string(workflow_record, "workflow_version")
    if workflow_type != WORKFLOW_TYPE or workflow_version != WORKFLOW_VERSION:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_workflow_record",
            message="Workflow transition supports governed income-tax workflow records only.",
            details={"workflow_type": workflow_type, "workflow_version": workflow_version},
        )
    workflow_context = (
        _require_string(workflow_record, "supported_lane_id"),
        _require_string(workflow_record, "historical_version_id"),
        _require_int(workflow_record, "tax_year"),
    )
    if workflow_context not in SUPPORTED_WORKFLOW_CONTEXTS:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_workflow_scope",
            message="No governed submission workflow exists for this workflow context.",
            details={
                "supported_lane_id": workflow_context[0],
                "historical_version_id": workflow_context[1],
                "tax_year": workflow_context[2],
            },
        )
    lineage = _require_object(workflow_record, "lineage")
    _require_string(workflow_record, "report_id")
    _require_string(workflow_record, "form_artifact_id")
    if _require_string(lineage, "payload_type") != SUBMISSION_PAYLOAD_TYPE:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_workflow_lineage",
            message="Workflow record lineage must reference income-tax payload artifacts.",
        )
    if _require_string(lineage, "payload_version") != SUBMISSION_PAYLOAD_VERSION:
        raise IncomeTaxSubmissionWorkflowError(
            reason="unsupported_workflow_lineage",
            message="Workflow record lineage must reference the governed payload version.",
        )
    _list_of_objects(workflow_record, "status_history")


def _build_transition_entry(
    *,
    workflow_record_id: str,
    transition_index: int,
    from_status: SubmissionWorkflowStatus | None,
    to_status: SubmissionWorkflowStatus,
    transition_reason: str,
) -> dict[str, object]:
    transition_identity = {
        "workflow_record_id": workflow_record_id,
        "transition_index": transition_index,
        "from_status": from_status,
        "to_status": to_status,
        "transition_reason": transition_reason,
    }
    return {
        "transition_id": _sha256_hex(canonical_json_dumps(transition_identity)),
        "transition_index": transition_index,
        "from_status": from_status,
        "to_status": to_status,
        "transition_reason": transition_reason,
    }


def _build_workflow_audit_evidence(workflow_record: Mapping[str, object]) -> dict[str, object]:
    status_history = _list_of_objects(workflow_record, "status_history")
    current_status = _require_string(workflow_record, "current_status")
    latest_transition = status_history[-1]
    latest_transition_id = _require_string(latest_transition, "transition_id")
    latest_transition_reason = _require_string(latest_transition, "transition_reason")
    lineage = _require_object(workflow_record, "lineage")

    payload = {
        "audit_kind": "income_tax_submission_workflow",
        "action": "submission_workflow_transition",
        "action_status": "applied",
        "workflow_record_id": _require_string(workflow_record, "workflow_record_id"),
        "submission_payload_id": _require_string(workflow_record, "submission_payload_id"),
        "report_id": _require_string(workflow_record, "report_id"),
        "form_artifact_id": _require_string(workflow_record, "form_artifact_id"),
        "computation_id": _require_string(workflow_record, "computation_id"),
        "supported_lane_id": _require_string(workflow_record, "supported_lane_id"),
        "historical_version_id": _require_string(workflow_record, "historical_version_id"),
        "tax_year": _require_int(workflow_record, "tax_year"),
        "current_status": current_status,
        "latest_transition_id": latest_transition_id,
        "latest_transition_reason": latest_transition_reason,
        "lineage": {
            "input_hash": _require_string(lineage, "input_hash"),
            "finalized_audit_event_id": _require_string(lineage, "finalized_audit_event_id"),
            "source_anchor_ids": _list_of_strings(lineage, "source_anchor_ids"),
            "applied_policy_ids": _list_of_strings(lineage, "applied_policy_ids"),
            "payload_audit_evidence_id": _require_string(lineage, "payload_audit_evidence_id"),
            "report_audit_evidence_id": _require_string(lineage, "report_audit_evidence_id"),
            "artifact_audit_evidence_id": _require_string(lineage, "artifact_audit_evidence_id"),
        },
    }
    return {
        **payload,
        "audit_evidence_id": _sha256_hex(canonical_json_dumps(payload)),
    }


def _parse_workflow_status(value: str) -> SubmissionWorkflowStatus:
    normalized = value.strip()
    if normalized not in {"prepared", "ready_for_submission", "submitted_internal"}:
        raise IncomeTaxSubmissionWorkflowError(
            reason="invalid_workflow_status",
            message="Workflow status is not supported by governed submission workflow.",
            details={"status": value},
        )
    return cast(SubmissionWorkflowStatus, normalized)


def _as_object(value: Mapping[str, object] | object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IncomeTaxSubmissionWorkflowError(
            reason=reason,
            message="Expected JSON object input for deterministic submission workflow handling.",
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxSubmissionWorkflowError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    typed_value = cast(Mapping[object, object], value)
    return {str(key): typed_value[key] for key in typed_value}


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxSubmissionWorkflowError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: Mapping[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxSubmissionWorkflowError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    return value


def _list_of_strings(source: Mapping[str, object], field_name: str) -> list[str]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxSubmissionWorkflowError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    strings: list[str] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, str) or not item.strip():
            raise IncomeTaxSubmissionWorkflowError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only non-empty strings.",
                details={"field_name": field_name},
            )
        strings.append(item)
    return strings


def _list_of_objects(source: Mapping[str, object], field_name: str) -> list[dict[str, object]]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise IncomeTaxSubmissionWorkflowError(
            reason="missing_required_field",
            message=f"Required list field '{field_name}' is missing.",
            details={"field_name": field_name},
        )
    objects: list[dict[str, object]] = []
    typed_value = cast(list[object], value)
    for item in typed_value:
        if not isinstance(item, Mapping):
            raise IncomeTaxSubmissionWorkflowError(
                reason="invalid_list_item",
                message=f"Field '{field_name}' must contain only objects.",
                details={"field_name": field_name},
            )
        typed_item = cast(Mapping[object, object], item)
        objects.append({str(key): typed_item[key] for key in typed_item})
    return objects


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
