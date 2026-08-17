"""Map adapter execution outcomes into canonical orchestration action-result statuses."""

from __future__ import annotations

from typing import Literal
from typing import TypedDict
from collections.abc import Mapping

from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse

CanonicalActionStatus = Literal["accepted", "rejected", "pending", "retryable_failure"]

ACCEPTED_RESULT_CODES = {
    "submission_action_accepted",
    "provider_accepted",
    "knowledge_lookup_resolved",
    "knowledge_timeline_resolved",
    "forms_artifact_generated",
    "forms_mapping_ready",
    "reports_artifact_generated",
    "document_evidence_resolved",
}
PENDING_RESULT_CODES = {
    "submission_action_mock_pending",
    "submission_action_pending",
    "provider_pending",
    "provider_queued",
    "provider_in_progress",
    "document_evidence_processing_pending",
}
RETRYABLE_RESULT_CODES = {
    "provider_retryable_failure",
    "provider_transient_failure",
    "provider_timeout",
}
REJECTED_RESULT_CODES = {
    "unsupported_action_type",
    "provider_rejected",
    "hard_validation_rejection",
}


class CanonicalActionResult(TypedDict):
    """Represent canonical deterministic action-result mapping output."""

    action_status: CanonicalActionStatus
    reason_code: str
    reason: str
    retryable: bool
    next_retry_at: str | None
    provider_reference: str | None
    correlation_id: str
    idempotency_key: str
    trace_id: str


def map_action_result(
    *,
    idempotency_key: str,
    correlation_id: str,
    trace_id: str,
    execution_status: str,
    adapter_response: ActionAdapterResponse | None,
    execution_error: Mapping[str, object] | None,
) -> CanonicalActionResult:
    """Map deterministic execution outcome into canonical action-result status envelope."""

    adapter_status: str | None = None
    action_result_code: str | None = None
    if execution_status == "rejected":
        if execution_error is not None:
            reason_code = execution_error.get("reason_code")
            reason = execution_error.get("reason")
            result: CanonicalActionResult = {
                "action_status": "rejected",
                "reason_code": (
                    reason_code if isinstance(reason_code, str) else "execution_rejected"
                ),
                "reason": (
                    reason
                    if isinstance(reason, str)
                    else "Action execution was rejected before adapter dispatch."
                ),
                "retryable": False,
                "next_retry_at": None,
                "provider_reference": None,
                "correlation_id": correlation_id,
                "idempotency_key": idempotency_key,
                "trace_id": trace_id,
            }
        else:
            result = {
                "action_status": "rejected",
                "reason_code": "execution_rejected",
                "reason": "Action execution was rejected before adapter dispatch.",
                "retryable": False,
                "next_retry_at": None,
                "provider_reference": None,
                "correlation_id": correlation_id,
                "idempotency_key": idempotency_key,
                "trace_id": trace_id,
            }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    if adapter_response is None:
        result = {
            "action_status": "rejected",
            "reason_code": "adapter_response_missing",
            "reason": "Adapter response is missing for resolved execution outcome.",
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": None,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    provider_reference = adapter_response["provider_reference"]
    adapter_status = adapter_response["adapter_status"]
    action_result_code = adapter_response["action_result_code"]
    adapter_error = adapter_response["error"]

    if adapter_error is not None:
        result = {
            "action_status": "rejected",
            "reason_code": adapter_error["reason_code"],
            "reason": adapter_error["reason"],
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": provider_reference,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    if action_result_code in ACCEPTED_RESULT_CODES and adapter_status == "accepted":
        result = {
            "action_status": "accepted",
            "reason_code": action_result_code,
            "reason": adapter_response["message"],
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": provider_reference,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    if action_result_code in PENDING_RESULT_CODES or adapter_status == "mock_pending":
        result = {
            "action_status": "pending",
            "reason_code": action_result_code,
            "reason": adapter_response["message"],
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": provider_reference,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    if action_result_code in RETRYABLE_RESULT_CODES:
        result = {
            "action_status": "retryable_failure",
            "reason_code": action_result_code,
            "reason": adapter_response["message"],
            "retryable": True,
            "next_retry_at": None,
            "provider_reference": provider_reference,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    if action_result_code in REJECTED_RESULT_CODES or adapter_status == "unsupported":
        result = {
            "action_status": "rejected",
            "reason_code": action_result_code,
            "reason": adapter_response["message"],
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": provider_reference,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        _emit_action_result_mapped_event(
            result=result,
            execution_status=execution_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            adapter_status=adapter_status,
            action_result_code=action_result_code,
        )
        return result

    result = {
        "action_status": "rejected",
        "reason_code": "unknown_adapter_outcome",
        "reason": (
            "Adapter outcome is not recognized by deterministic canonical action-result mapping."
        ),
        "retryable": False,
        "next_retry_at": None,
        "provider_reference": provider_reference,
        "correlation_id": correlation_id,
        "idempotency_key": idempotency_key,
        "trace_id": trace_id,
    }
    _emit_action_result_mapped_event(
        result=result,
        execution_status=execution_status,
        correlation_id=correlation_id,
        trace_id=trace_id,
        adapter_status=adapter_status,
        action_result_code=action_result_code,
    )
    return result


def _emit_action_result_mapped_event(
    *,
    result: CanonicalActionResult,
    execution_status: str,
    correlation_id: str,
    trace_id: str,
    adapter_status: str | None,
    action_result_code: str | None,
) -> None:
    emit_income_tax_audit_event(
        event_type="action_execution_result_mapped",
        status=result["action_status"],
        correlation_id=correlation_id,
        trace_id=trace_id,
        context={
            "execution_status": execution_status,
            "adapter_status": adapter_status,
            "adapter_result_code": action_result_code,
            "reason_code": result["reason_code"],
            "retryable": result["retryable"],
        },
    )
