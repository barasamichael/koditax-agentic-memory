"""Define deterministic action-adapter contract for income-tax submission abstraction."""

from __future__ import annotations

from typing import Literal
from typing import Protocol
from typing import TypedDict
from typing import NotRequired
import hashlib

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.trace_context import build_trace_id

ActionAdapterStatus = Literal["accepted", "mock_pending", "unsupported"]


class ActionAdapterCapabilityContext(TypedDict):
    """Represent deterministic capability context for adapter dispatch."""

    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None


class KnowledgeRouteCapability(TypedDict):
    """Represent one governed Knowledge operation capability consumed by orchestration."""

    route_id: str
    target_service: Literal["knowledge"]
    target_operation: Literal["search_knowledge", "retrieve_knowledge", "timeline_search_knowledge"]
    route_mode: Literal["search", "retrieve", "timeline_search"]
    preserves_chronology: bool
    governed_evidence_required: bool


class ActionAdapterRequest(TypedDict):
    """Represent deterministic action-adapter request envelope."""

    action_type: str
    correlation_id: str
    submission_payload_ref: str | None
    capability_context: ActionAdapterCapabilityContext
    trace_id: NotRequired[str]
    idempotency_key: NotRequired[str]
    route_id: NotRequired[str]
    target_service: NotRequired[str]
    target_operation: NotRequired[str]
    plan_id: NotRequired[str]
    step_id: NotRequired[str]
    auth_context: NotRequired[dict[str, str | None]]
    route_payload: NotRequired[dict[str, object]]


class ActionAdapterTrace(TypedDict):
    """Represent deterministic trace linkage fields for adapter outputs."""

    correlation_id: str
    trace_id: str
    adapter_request_id: str
    adapter_name: str
    submission_payload_ref: str | None
    idempotency_key: NotRequired[str]
    route_id: NotRequired[str]
    target_service: NotRequired[str]
    target_operation: NotRequired[str]
    plan_id: NotRequired[str]
    step_id: NotRequired[str]


class ActionAdapterRejectedContext(TypedDict):
    """Represent deterministic rejected context for unsupported adapter requests."""

    action_type: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    correlation_id: str


class ActionAdapterErrorEnvelope(TypedDict):
    """Represent canonical deterministic rejection payload for adapter dispatch."""

    error_code: str
    message: str
    reason_code: str
    reason: str
    rejected_context: ActionAdapterRejectedContext
    required_controls: list[str]
    next_allowed_actions: list[str]
    trace_id: str


class ActionAdapterResponse(TypedDict):
    """Represent deterministic action-adapter response envelope."""

    adapter_status: ActionAdapterStatus
    provider_reference: str | None
    action_result_code: str
    message: str
    trace: ActionAdapterTrace
    error: ActionAdapterErrorEnvelope | None
    result_payload: NotRequired[dict[str, object]]


class SubmissionActionAdapter(Protocol):
    """Define stable deterministic action-adapter interface for submission actions."""

    adapter_name: str
    supported_action_types: tuple[str, ...]

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        """Dispatch one deterministic adapter request."""
        ...


def dispatch_submission_action_with_adapter(
    *,
    request: ActionAdapterRequest,
    adapter: SubmissionActionAdapter,
) -> ActionAdapterResponse:
    """Dispatch request through one adapter implementation with deterministic guards."""

    if request["action_type"] not in adapter.supported_action_types:
        return build_unsupported_action_response(
            request=request,
            adapter_name=adapter.adapter_name,
            reason_code="unsupported_action_type",
            reason="Action type is outside supported deterministic action-adapter scope.",
        )
    return adapter.dispatch(request)


def build_unsupported_action_response(
    *,
    request: ActionAdapterRequest,
    adapter_name: str,
    reason_code: str,
    reason: str,
) -> ActionAdapterResponse:
    """Build canonical deterministic rejection envelope for unsupported adapter requests."""

    capability_context = request["capability_context"]
    trace_id = build_trace_id(request["correlation_id"])
    return {
        "adapter_status": "unsupported",
        "provider_reference": None,
        "action_result_code": "unsupported_action_type",
        "message": "Action request is not supported by deterministic submission adapter contract.",
        "trace": {
            "correlation_id": request["correlation_id"],
            "trace_id": trace_id,
            "adapter_request_id": build_adapter_request_id(
                request=request,
                adapter_name=adapter_name,
            ),
            "adapter_name": adapter_name,
            "submission_payload_ref": request["submission_payload_ref"],
        },
        "error": {
            "error_code": "unsupported_submission_action",
            "message": "Submission action type is not supported by registered adapter contract.",
            "reason_code": reason_code,
            "reason": reason,
            "rejected_context": {
                "action_type": request["action_type"],
                "supported_lane_id": capability_context["supported_lane_id"],
                "historical_version_id": capability_context["historical_version_id"],
                "tax_year": capability_context["tax_year"],
                "correlation_id": request["correlation_id"],
            },
            "required_controls": ["revise_action_type"],
            "next_allowed_actions": ["revise_input", "reject"],
            "trace_id": trace_id,
        },
    }


def build_adapter_request_id(
    *,
    request: ActionAdapterRequest,
    adapter_name: str,
) -> str:
    """Return deterministic adapter request identity."""

    digest_input = {
        "scope": "income_tax_submission_action_adapter",
        "adapter_name": adapter_name,
        "request": request,
    }
    return _sha256_hex(canonical_json_dumps(digest_input))


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
