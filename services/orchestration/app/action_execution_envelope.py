"""Provide deterministic idempotent execution envelope for adapter action requests."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
from typing import NotRequired
import hashlib
from collections.abc import Callable

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.trace_context import build_trace_id
from services.orchestration.app.action_result_mapping import map_action_result
from services.orchestration.app.action_result_mapping import CanonicalActionResult
from services.orchestration.app.action_execution_store import ActionExecutionStore
from services.orchestration.app.action_execution_store import ActionExecutionStoreError
from services.orchestration.app.action_execution_store import ActionExecutionStoreRecord
from services.orchestration.app.action_execution_store import InMemoryActionExecutionStore
from services.orchestration.app.action_execution_store import build_default_action_execution_store
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse
from services.orchestration.app.action_adapter_contract import ActionAdapterCapabilityContext

ExecutionStatus = Literal["resolved", "rejected"]


class OrchestrationPlanStep(TypedDict):
    """Represent one canonical orchestration plan step."""

    step_id: str
    route_id: str
    target_service: str
    target_operation: str
    step_status: str
    depends_on: list[str]
    step_purpose: str | None


class OrchestrationExecutionPlan(TypedDict):
    """Represent canonical production-safe plan contract for execution runtime."""

    plan_id: str
    plan_version: str
    plan_status: str
    planning_mode: str
    execution_ready: bool
    steps: list[OrchestrationPlanStep]


class ActionExecutionContext(TypedDict):
    """Represent deterministic action context for idempotent execution envelope."""

    action_type: str
    submission_payload_ref: NotRequired[str | None]
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    route_id: NotRequired[str | None]
    target_service: NotRequired[str | None]
    target_operation: NotRequired[str | None]
    plan_id: NotRequired[str | None]
    step_id: NotRequired[str | None]
    tenant_id: NotRequired[str | None]
    user_id: NotRequired[str | None]


class ActionExecutionRequest(TypedDict):
    """Represent deterministic request envelope for idempotent action execution."""

    idempotency_key: str
    correlation_id: str
    action_type: str
    submission_payload_ref: str | None
    capability_context: ActionAdapterCapabilityContext
    trace_id: NotRequired[str]
    route_id: NotRequired[str]
    target_service: NotRequired[str]
    target_operation: NotRequired[str]
    plan_id: NotRequired[str]
    step_id: NotRequired[str]
    auth_context: NotRequired[dict[str, str | None]]
    route_payload: NotRequired[dict[str, object]]


class ActionExecutionTrace(TypedDict):
    """Represent deterministic trace linkage for one execution envelope."""

    execution_envelope_id: str
    correlation_id: str
    trace_id: str
    idempotency_key: str
    request_fingerprint: str


class ActionExecutionRejectedContext(TypedDict):
    """Represent deterministic context for idempotent request rejection."""

    idempotency_key: str
    correlation_id: str
    action_type: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None


class ActionExecutionErrorEnvelope(TypedDict):
    """Represent canonical deterministic rejection envelope for execution errors."""

    error_code: str
    message: str
    reason_code: str
    reason: str
    rejected_context: ActionExecutionRejectedContext
    required_controls: list[str]
    next_allowed_actions: list[str]
    trace_id: str


class ActionExecutionEnvelope(TypedDict):
    """Represent deterministic idempotent execution response envelope."""

    execution_id: str
    idempotency_key: str
    correlation_id: str
    request_fingerprint: str
    plan: OrchestrationExecutionPlan
    action_context: ActionExecutionContext
    execution_status: ExecutionStatus
    adapter_response: ActionAdapterResponse | None
    mapped_result: CanonicalActionResult
    error: ActionExecutionErrorEnvelope | None
    trace: ActionExecutionTrace


class ActionExecutionRecord(TypedDict):
    """Represent stored idempotency record for one execution key."""

    execution_id: str
    idempotency_key: str
    request_fingerprint: str
    envelope: ActionExecutionEnvelope


class InMemoryActionExecutionIdempotencyStore(InMemoryActionExecutionStore):
    """Compatibility alias for deterministic in-memory idempotency record storage."""


_default_execution_idempotency_store: ActionExecutionStore = build_default_action_execution_store()


def execute_idempotent_action_request(
    *,
    request: ActionExecutionRequest,
    dispatch_adapter_request: Callable[[ActionAdapterRequest], ActionAdapterResponse],
    idempotency_store: ActionExecutionStore | None = None,
) -> ActionExecutionEnvelope:
    """Resolve one action request through deterministic idempotency envelope semantics."""

    store = (
        idempotency_store if idempotency_store is not None else _default_execution_idempotency_store
    )
    idempotency_key = request["idempotency_key"].strip()
    trace_id = build_trace_id(request["correlation_id"])
    execution_envelope_id = build_execution_envelope_id(request)
    plan = _build_execution_plan(request=request)
    auth_context = request.get("auth_context") or {}
    emit_income_tax_audit_event(
        event_type="action_execution_requested",
        status="requested",
        correlation_id=request["correlation_id"],
        trace_id=trace_id,
        supported_lane_id=request["capability_context"]["supported_lane_id"],
        historical_version_id=request["capability_context"]["historical_version_id"],
        tax_year=request["capability_context"]["tax_year"],
        context={
            "action_type": request["action_type"],
            "idempotency_key": idempotency_key,
            "route_id": request.get("route_id"),
            "target_service": request.get("target_service"),
            "target_operation": request.get("target_operation"),
            "plan_id": plan["plan_id"],
            "tenant_id": auth_context.get("tenant_id"),
            "user_id": auth_context.get("user_id"),
            "resource_id": execution_envelope_id,
        },
    )
    if not idempotency_key:
        return _rejected_envelope(
            request=request,
            reason_code="missing_idempotency_key",
            reason=(
                "Action execution request must include non-empty idempotency key for "
                "deterministic replay-safe handling."
            ),
            required_controls=["provide_idempotency_key"],
            next_allowed_actions=["revise_input", "reject"],
        )

    request_fingerprint = build_action_execution_request_fingerprint(request)
    try:
        existing = store.get(idempotency_key)
    except ActionExecutionStoreError as error:
        return _rejected_envelope(
            request=request,
            reason_code=error.reason_code,
            reason=error.message,
            required_controls=["restore_execution_persistence"],
            next_allowed_actions=["retry", "reject"],
        )
    if existing is not None:
        if existing["request_fingerprint"] != request_fingerprint:
            return _rejected_envelope(
                request=request,
                reason_code="idempotency_key_payload_conflict",
                reason=(
                    "Idempotency key has already been used with different action payload "
                    "fingerprint."
                ),
                required_controls=["revise_idempotency_key"],
                next_allowed_actions=["revise_input", "reject"],
            )
        return cast(ActionExecutionEnvelope, existing["envelope"])

    adapter_request = _as_adapter_request(request)
    emit_income_tax_audit_event(
        event_type="route_dispatch_requested",
        status="requested",
        correlation_id=request["correlation_id"],
        trace_id=trace_id,
        supported_lane_id=request["capability_context"]["supported_lane_id"],
        historical_version_id=request["capability_context"]["historical_version_id"],
        tax_year=request["capability_context"]["tax_year"],
        context={
            "action_type": request["action_type"],
            "route_id": request.get("route_id"),
            "target_service": request.get("target_service"),
            "target_operation": request.get("target_operation"),
            "tenant_id": auth_context.get("tenant_id"),
            "user_id": auth_context.get("user_id"),
            "resource_id": execution_envelope_id,
        },
    )
    adapter_response = dispatch_adapter_request(adapter_request)
    emit_income_tax_audit_event(
        event_type="route_dispatch_resolved",
        status=adapter_response["adapter_status"],
        correlation_id=request["correlation_id"],
        trace_id=trace_id,
        supported_lane_id=request["capability_context"]["supported_lane_id"],
        historical_version_id=request["capability_context"]["historical_version_id"],
        tax_year=request["capability_context"]["tax_year"],
        context={
            "action_type": request["action_type"],
            "route_id": request.get("route_id"),
            "target_service": request.get("target_service"),
            "target_operation": request.get("target_operation"),
            "action_result_code": adapter_response["action_result_code"],
            "tenant_id": auth_context.get("tenant_id"),
            "user_id": auth_context.get("user_id"),
            "resource_id": execution_envelope_id,
        },
    )
    envelope: ActionExecutionEnvelope = {
        "execution_id": execution_envelope_id,
        "idempotency_key": idempotency_key,
        "correlation_id": request["correlation_id"],
        "request_fingerprint": request_fingerprint,
        "plan": plan,
        "action_context": {
            "action_type": request["action_type"],
            "submission_payload_ref": request["submission_payload_ref"],
            "supported_lane_id": request["capability_context"]["supported_lane_id"],
            "historical_version_id": request["capability_context"]["historical_version_id"],
            "tax_year": request["capability_context"]["tax_year"],
            "route_id": request.get("route_id"),
            "target_service": request.get("target_service"),
            "target_operation": request.get("target_operation"),
            "plan_id": request.get("plan_id"),
            "step_id": request.get("step_id"),
            "tenant_id": auth_context.get("tenant_id"),
            "user_id": auth_context.get("user_id"),
        },
        "execution_status": "resolved",
        "adapter_response": adapter_response,
        "mapped_result": map_action_result(
            idempotency_key=idempotency_key,
            correlation_id=request["correlation_id"],
            trace_id=trace_id,
            execution_status="resolved",
            adapter_response=adapter_response,
            execution_error=None,
        ),
        "error": None,
        "trace": {
            "execution_envelope_id": execution_envelope_id,
            "correlation_id": request["correlation_id"],
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
        },
    }
    try:
        store.put(
            cast(
                ActionExecutionStoreRecord,
                {
                    "execution_id": execution_envelope_id,
                    "idempotency_key": idempotency_key,
                    "request_fingerprint": request_fingerprint,
                    "envelope": cast(dict[str, object], envelope),
                },
            )
        )
    except ActionExecutionStoreError as error:
        return _rejected_envelope(
            request=request,
            reason_code=error.reason_code,
            reason=error.message,
            required_controls=["restore_execution_persistence"],
            next_allowed_actions=["retry", "reject"],
        )
    emit_income_tax_audit_event(
        event_type="action_execution_persisted",
        status="persisted",
        correlation_id=request["correlation_id"],
        trace_id=trace_id,
        supported_lane_id=request["capability_context"]["supported_lane_id"],
        historical_version_id=request["capability_context"]["historical_version_id"],
        tax_year=request["capability_context"]["tax_year"],
        context={
            "action_type": request["action_type"],
            "idempotency_key": idempotency_key,
            "plan_id": plan["plan_id"],
            "tenant_id": auth_context.get("tenant_id"),
            "user_id": auth_context.get("user_id"),
            "resource_id": execution_envelope_id,
        },
    )
    return envelope


def reset_default_action_execution_idempotency_store() -> None:
    """Reset default idempotency records for deterministic test isolation."""

    global _default_execution_idempotency_store
    try:
        _default_execution_idempotency_store.clear()
    finally:
        _default_execution_idempotency_store = build_default_action_execution_store()


def set_default_action_execution_idempotency_store(store: ActionExecutionStore) -> None:
    """Override the default execution store for runtime/tests."""

    global _default_execution_idempotency_store
    _default_execution_idempotency_store = store


def get_default_action_execution_idempotency_store() -> ActionExecutionStore:
    """Return the active default execution store used by orchestration runtime."""

    return _default_execution_idempotency_store


def build_action_execution_request_fingerprint(request: ActionExecutionRequest) -> str:
    """Build deterministic request fingerprint for idempotency conflict detection."""

    digest_input = {
        "correlation_id": request["correlation_id"],
        "action_type": request["action_type"],
        "submission_payload_ref": request["submission_payload_ref"],
        "capability_context": request["capability_context"],
        "route_id": request.get("route_id"),
        "target_service": request.get("target_service"),
        "target_operation": request.get("target_operation"),
        "plan_id": request.get("plan_id"),
        "step_id": request.get("step_id"),
        "auth_context": request.get("auth_context"),
        "route_payload": request.get("route_payload"),
    }
    return _sha256_hex(canonical_json_dumps(digest_input))


def build_execution_envelope_id(request: ActionExecutionRequest) -> str:
    """Build deterministic execution-envelope identity for one idempotency key."""

    digest_input = {
        "scope": "orchestration_action_execution_envelope",
        "idempotency_key": request["idempotency_key"],
        "correlation_id": request["correlation_id"],
        "action_type": request["action_type"],
    }
    return _sha256_hex(canonical_json_dumps(digest_input))


def _as_adapter_request(request: ActionExecutionRequest) -> ActionAdapterRequest:
    adapter_request: ActionAdapterRequest = {
        "action_type": request["action_type"],
        "correlation_id": request["correlation_id"],
        "submission_payload_ref": request["submission_payload_ref"],
        "capability_context": request["capability_context"],
    }
    trace_id = request.get("trace_id")
    if isinstance(trace_id, str) and trace_id:
        adapter_request["trace_id"] = trace_id
    adapter_request["idempotency_key"] = request["idempotency_key"]
    route_id = request.get("route_id")
    if isinstance(route_id, str) and route_id:
        adapter_request["route_id"] = route_id
    target_service = request.get("target_service")
    if isinstance(target_service, str) and target_service:
        adapter_request["target_service"] = target_service
    target_operation = request.get("target_operation")
    if isinstance(target_operation, str) and target_operation:
        adapter_request["target_operation"] = target_operation
    auth_context = request.get("auth_context")
    if isinstance(auth_context, dict):
        adapter_request["auth_context"] = auth_context
    plan_id = request.get("plan_id")
    if isinstance(plan_id, str) and plan_id:
        adapter_request["plan_id"] = plan_id
    step_id = request.get("step_id")
    if isinstance(step_id, str) and step_id:
        adapter_request["step_id"] = step_id
    route_payload = request.get("route_payload")
    if isinstance(route_payload, dict):
        adapter_request["route_payload"] = route_payload
    return adapter_request


def _rejected_envelope(
    *,
    request: ActionExecutionRequest,
    reason_code: str,
    reason: str,
    required_controls: list[str],
    next_allowed_actions: list[str],
) -> ActionExecutionEnvelope:
    idempotency_key = request["idempotency_key"]
    request_fingerprint = build_action_execution_request_fingerprint(request)
    execution_envelope_id = build_execution_envelope_id(request)
    trace_id = build_trace_id(request["correlation_id"])
    capability_context = request["capability_context"]
    auth_context = request.get("auth_context") or {}
    envelope: ActionExecutionEnvelope = {
        "execution_id": execution_envelope_id,
        "idempotency_key": idempotency_key,
        "correlation_id": request["correlation_id"],
        "request_fingerprint": request_fingerprint,
        "plan": _build_execution_plan(request=request),
        "action_context": {
            "action_type": request["action_type"],
            "supported_lane_id": capability_context["supported_lane_id"],
            "historical_version_id": capability_context["historical_version_id"],
            "tax_year": capability_context["tax_year"],
            "route_id": request.get("route_id"),
            "target_service": request.get("target_service"),
            "target_operation": request.get("target_operation"),
            "plan_id": request.get("plan_id"),
            "step_id": request.get("step_id"),
            "tenant_id": auth_context.get("tenant_id"),
            "user_id": auth_context.get("user_id"),
        },
        "execution_status": "rejected",
        "adapter_response": None,
        "mapped_result": {
            "action_status": "rejected",
            "reason_code": reason_code,
            "reason": reason,
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": None,
            "correlation_id": request["correlation_id"],
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        },
        "error": {
            "error_code": "idempotent_action_execution_rejected",
            "message": "Action execution request failed deterministic idempotency enforcement.",
            "reason_code": reason_code,
            "reason": reason,
            "rejected_context": {
                "idempotency_key": idempotency_key,
                "correlation_id": request["correlation_id"],
                "action_type": request["action_type"],
                "supported_lane_id": capability_context["supported_lane_id"],
                "historical_version_id": capability_context["historical_version_id"],
                "tax_year": capability_context["tax_year"],
            },
            "required_controls": required_controls,
            "next_allowed_actions": next_allowed_actions,
            "trace_id": trace_id,
        },
        "trace": {
            "execution_envelope_id": execution_envelope_id,
            "correlation_id": request["correlation_id"],
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
        },
    }
    envelope["mapped_result"] = map_action_result(
        idempotency_key=idempotency_key,
        correlation_id=request["correlation_id"],
        trace_id=trace_id,
        execution_status="rejected",
        adapter_response=None,
        execution_error=envelope["error"],
    )
    return envelope


def _build_execution_plan(*, request: ActionExecutionRequest) -> OrchestrationExecutionPlan:
    route_id = request.get("route_id") or request["action_type"]
    target_service = request.get("target_service") or "unknown"
    target_operation = request.get("target_operation") or request["action_type"]
    step_id = _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "orchestration_execution_plan_step",
                "route_id": route_id,
                "target_service": target_service,
                "target_operation": target_operation,
            }
        )
    )
    plan_id = _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "orchestration_execution_plan",
                "action_type": request["action_type"],
                "route_id": route_id,
                "target_service": target_service,
                "target_operation": target_operation,
            }
        )
    )
    return {
        "plan_id": plan_id,
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": "single_step",
        "execution_ready": True,
        "steps": [
            {
                "step_id": step_id,
                "route_id": route_id,
                "target_service": target_service,
                "target_operation": target_operation,
                "step_status": "planned",
                "depends_on": [],
                "step_purpose": "route_dispatch",
            }
        ],
    }


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
