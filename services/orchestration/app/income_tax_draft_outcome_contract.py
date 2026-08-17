"""Build canonical deterministic draft-outcome response envelope for income-tax prompt flow."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from collections.abc import Mapping


class DraftContext(TypedDict):
    """Represent deterministic draft context for client review."""

    tax_type: str
    supported_lane_id: str
    historical_version_id: str
    tax_year: int


class ReviewSummary(TypedDict):
    """Represent key liability figures for deterministic user review."""

    chargeable_income_kes: str
    gross_tax_kes: str
    total_reliefs_kes: str
    net_income_tax_due_kes: str
    refund_due_kes: str


class ArtifactRefs(TypedDict):
    """Represent deterministic artifact references for review-time traceability."""

    form_artifact_id: str
    form_version_id: str
    report_id: str
    report_version_id: str
    submission_preview_payload_id: str
    submission_preview_payload_version: str


class DraftOutcomeLineage(TypedDict):
    """Represent deterministic lineage anchors for draft outcome contract."""

    computation_id: str
    input_hash: str
    rule_version: str
    finalized_audit_event_id: str
    form_audit_evidence_id: str
    report_audit_evidence_id: str
    payload_audit_evidence_id: str


class IncomeTaxDraftOutcomeEnvelope(TypedDict):
    """Represent canonical deterministic draft-outcome response contract."""

    status: str
    message: str
    prompt_id: str
    prompt_text: str
    draft_context: DraftContext
    review_summary: ReviewSummary
    artifacts: ArtifactRefs
    lineage: DraftOutcomeLineage
    next_allowed_actions: list[str]


class IncomeTaxDraftOutcomeContractError(RuntimeError):
    """Represent deterministic contract-mapping failures for draft-outcome response."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, object]:
        """Return canonical deterministic payload for contract mapping errors."""

        return {
            "error_code": "draft_outcome_contract_mapping_failed",
            "message": self.message,
            "reason": self.reason,
            "details": self.details,
        }


def build_income_tax_draft_outcome_response(
    *,
    prompt_id: str,
    prompt_text: str,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
    computation_output: Mapping[str, object],
    finalized_output: Mapping[str, object],
    form_artifact_output: Mapping[str, object],
    report_output: Mapping[str, object],
    report_version_binding: Mapping[str, object],
    submission_payload_output: Mapping[str, object],
) -> IncomeTaxDraftOutcomeEnvelope:
    """Map deterministic execution outputs to one canonical draft-ready review envelope."""

    result_payload = _require_mapping(finalized_output, "result_payload")
    liability_summary = _require_mapping(result_payload, "liability_summary")
    form_audit = _require_mapping(form_artifact_output, "audit_evidence")
    report_audit = _require_mapping(report_output, "audit_evidence")
    payload_audit = _require_mapping(submission_payload_output, "audit_evidence")

    envelope: IncomeTaxDraftOutcomeEnvelope = {
        "status": "draft_ready",
        "message": (
            "Draft income-tax outcome is ready for review. Confirm to continue, "
            "reject to stop, or revise input."
        ),
        "prompt_id": prompt_id,
        "prompt_text": prompt_text,
        "draft_context": {
            "tax_type": _require_string(computation_output, "tax_type"),
            "supported_lane_id": supported_lane_id,
            "historical_version_id": historical_version_id,
            "tax_year": tax_year,
        },
        "review_summary": {
            "chargeable_income_kes": _require_string(liability_summary, "chargeable_income_kes"),
            "gross_tax_kes": _require_string(liability_summary, "gross_tax_kes"),
            "total_reliefs_kes": _require_string(liability_summary, "total_reliefs_kes"),
            "net_income_tax_due_kes": _require_string(liability_summary, "net_income_tax_due_kes"),
            "refund_due_kes": _require_string(liability_summary, "refund_due_kes"),
        },
        "artifacts": {
            "form_artifact_id": _require_string(form_artifact_output, "artifact_id"),
            "form_version_id": _require_string(form_artifact_output, "form_version_id"),
            "report_id": _require_string(report_output, "report_id"),
            "report_version_id": _require_string(report_version_binding, "report_version_id"),
            "submission_preview_payload_id": _require_string(
                submission_payload_output, "payload_id"
            ),
            "submission_preview_payload_version": _require_string(
                submission_payload_output, "payload_version"
            ),
        },
        "lineage": {
            "computation_id": _require_string(finalized_output, "computation_id"),
            "input_hash": _require_string(computation_output, "input_hash"),
            "rule_version": _require_string(computation_output, "rule_version"),
            "finalized_audit_event_id": _require_string(
                finalized_output, "finalized_audit_event_id"
            ),
            "form_audit_evidence_id": _require_string(form_audit, "audit_evidence_id"),
            "report_audit_evidence_id": _require_string(report_audit, "audit_evidence_id"),
            "payload_audit_evidence_id": _require_string(payload_audit, "audit_evidence_id"),
        },
        "next_allowed_actions": ["confirm", "reject", "revise_input"],
    }
    return envelope


def _require_mapping(source: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    value = source.get(field_name)
    if not isinstance(value, Mapping):
        raise IncomeTaxDraftOutcomeContractError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing in draft outcome contract.",
            details={"field_name": field_name},
        )
    return cast(Mapping[str, object], value)


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxDraftOutcomeContractError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing in draft outcome contract.",
            details={"field_name": field_name},
        )
    return value
