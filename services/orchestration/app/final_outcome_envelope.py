"""Build canonical deterministic final outcome envelope with trace and audit references."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
from collections.abc import Mapping
from collections.abc import Sequence

from services.orchestration.app.audit_events import IncomeTaxAuditEvent
from services.orchestration.app.trace_context import build_optional_trace_id

OutcomeStatus = Literal["success", "rejected", "error", "pending"]


class FinalOutcomeTraceBlock(TypedDict):
    """Represent deterministic trace linkage for final outcome responses."""

    trace_id: str | None
    correlation_id: str | None
    lineage_refs: dict[str, object]


class DocumentEvidenceRefs(TypedDict):
    """Represent deterministic document evidence lineage references."""

    document_id: str
    representation_id: str
    projection_ref_id: str
    conflict_report_ref_id: str | None
    conflict_policy_decision_ref_id: str | None


class FinalOutcomeAuditBlock(TypedDict):
    """Represent deterministic audit linkage summary for final outcome responses."""

    event_count: int
    event_ids: list[str]
    event_types: list[str]
    latest_event_id_by_type: dict[str, str]


class FinalOutcomeEnvelope(TypedDict):
    """Represent canonical final response envelope for orchestration outcomes."""

    outcome_status: OutcomeStatus
    message: str
    trace: FinalOutcomeTraceBlock
    audit: FinalOutcomeAuditBlock
    result: dict[str, object]


class FinalOutcomeEnvelopeError(ValueError):
    """Represent deterministic final-outcome envelope construction failures."""

    def __init__(self, *, error_code: str, message: str, reason: str) -> None:
        super().__init__(reason)
        self.error_code = error_code
        self.message = message
        self.reason = reason

    def payload(self) -> dict[str, object]:
        """Return canonical deterministic error payload."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
        }


def map_action_status_to_outcome_status(action_status: str) -> OutcomeStatus:
    """Map canonical action status to final deterministic outcome status."""

    if action_status == "accepted":
        return "success"
    if action_status == "pending":
        return "pending"
    if action_status == "rejected":
        return "rejected"
    if action_status == "retryable_failure":
        return "error"
    return "error"


def build_income_tax_final_outcome_envelope(
    *,
    outcome_status: OutcomeStatus,
    message: str,
    result: Mapping[str, object],
    correlation_id: str | None,
    trace_id: str | None = None,
    lineage_refs: Mapping[str, object] | None = None,
    audit_events: Sequence[Mapping[str, object]] | None = None,
    document_evidence_refs: Mapping[str, object] | None = None,
    require_document_evidence_refs: bool = False,
) -> FinalOutcomeEnvelope:
    """Build canonical deterministic final outcome envelope for client-safe consumption."""

    resolved_trace_id = trace_id or build_optional_trace_id(correlation_id)
    resolved_document_evidence_refs = _normalize_document_evidence_refs(
        document_evidence_refs=document_evidence_refs,
        require_document_evidence_refs=require_document_evidence_refs,
    )
    resolved_lineage_refs = dict(lineage_refs or {})
    if resolved_document_evidence_refs is not None:
        resolved_lineage_refs["document_evidence_refs"] = resolved_document_evidence_refs
    sorted_events = _sorted_audit_events(audit_events or [])
    event_ids = [event["event_id"] for event in sorted_events]
    event_types = [event["event_type"] for event in sorted_events]
    latest_event_id_by_type = _latest_event_id_by_type(sorted_events)
    return {
        "outcome_status": outcome_status,
        "message": message,
        "trace": {
            "trace_id": resolved_trace_id,
            "correlation_id": correlation_id,
            "lineage_refs": resolved_lineage_refs,
        },
        "audit": {
            "event_count": len(sorted_events),
            "event_ids": event_ids,
            "event_types": event_types,
            "latest_event_id_by_type": latest_event_id_by_type,
        },
        "result": dict(result),
    }


def _normalize_document_evidence_refs(
    *,
    document_evidence_refs: Mapping[str, object] | None,
    require_document_evidence_refs: bool,
) -> DocumentEvidenceRefs | None:
    if document_evidence_refs is None:
        if require_document_evidence_refs:
            raise FinalOutcomeEnvelopeError(
                error_code="missing_document_evidence_refs",
                message=(
                    "Required document evidence references are missing from final outcome path."
                ),
                reason="required_document_evidence_refs_missing",
            )
        return None

    refs = dict(document_evidence_refs)
    document_id = _require_non_empty_string(
        refs=refs,
        field_name="document_id",
        error_code="missing_document_evidence_refs",
    )
    representation_id = _require_non_empty_string(
        refs=refs,
        field_name="representation_id",
        error_code="missing_document_evidence_refs",
    )
    projection_ref_id = _require_non_empty_string(
        refs=refs,
        field_name="projection_ref_id",
        error_code="missing_document_evidence_refs",
    )
    conflict_report_ref_id = _optional_string(refs.get("conflict_report_ref_id"))
    conflict_policy_decision_ref_id = _optional_string(refs.get("conflict_policy_decision_ref_id"))
    return {
        "document_id": document_id,
        "representation_id": representation_id,
        "projection_ref_id": projection_ref_id,
        "conflict_report_ref_id": conflict_report_ref_id,
        "conflict_policy_decision_ref_id": conflict_policy_decision_ref_id,
    }


def _require_non_empty_string(
    *,
    refs: Mapping[str, object],
    field_name: str,
    error_code: str,
) -> str:
    value = refs.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    raise FinalOutcomeEnvelopeError(
        error_code=error_code,
        message="Required document evidence references are missing from final outcome path.",
        reason=f"required_document_evidence_ref_missing:{field_name}",
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise FinalOutcomeEnvelopeError(
        error_code="invalid_document_evidence_refs",
        message="Document evidence references contain invalid optional values.",
        reason="invalid_document_evidence_refs_optional_field",
    )


def _sorted_audit_events(
    events: Sequence[Mapping[str, object]],
) -> list[IncomeTaxAuditEvent]:
    typed_events: list[IncomeTaxAuditEvent] = []
    for event in events:
        typed_events.append(
            {
                "event_id": cast(str, event["event_id"]),
                "event_type": cast(str, event["event_type"]),
                "event_time": cast(str, event["event_time"]),
                "trace_id": cast(str | None, event.get("trace_id")),
                "correlation_id": cast(str | None, event.get("correlation_id")),
                "tenant_id": cast(str | None, event.get("tenant_id")),
                "user_id": cast(str | None, event.get("user_id")),
                "resource_id": cast(str | None, event.get("resource_id")),
                "status": cast(str, event["status"]),
                "supported_lane_id": cast(str | None, event.get("supported_lane_id")),
                "historical_version_id": cast(str | None, event.get("historical_version_id")),
                "tax_year": cast(int | None, event.get("tax_year")),
                "context": cast(dict[str, object], event.get("context", {})),
            }
        )
    return sorted(
        typed_events,
        key=lambda event: (event["event_time"], event["event_type"], event["event_id"]),
    )


def _latest_event_id_by_type(events: list[IncomeTaxAuditEvent]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for event in events:
        latest[event["event_type"]] = event["event_id"]
    return latest
