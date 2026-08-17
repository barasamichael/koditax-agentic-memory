"""Validate translated orchestration plans before dispatch using deterministic guardrails."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from typing import NotRequired
from collections.abc import Mapping

from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.trace_context import build_optional_trace_id
from services.orchestration.app.intent_to_plan import PLAN_STEP_DEFINITIONS
from shared.validation.income_tax_capability_manifest import assert_supported_lane
from shared.validation.income_tax_capability_manifest import CapabilityManifestError
from shared.validation.income_tax_capability_manifest import load_income_tax_vertical_slice_manifest

SUPPORTED_INTENT_IDS = {"compute_income_tax"}
UNSUPPORTED_SCOPE_MESSAGE = "Prompt scope is not supported by governed income-tax pilot capability."
REQUIRED_TOP_LEVEL_FIELDS = (
    "plan_id",
    "intent_class",
    "supported_lane_id",
    "historical_version_id",
    "tax_year",
    "steps",
    "plan_status",
    "correlation_id",
    "trace_id",
)
REQUIRED_STEP_FIELDS = (
    "step_order",
    "step_id",
    "step_type",
    "module_ref",
    "action_ref",
    "external_action",
)
ALLOWED_STEP_SEQUENCE_BY_INTENT: dict[str, list[tuple[str, str, str, bool]]] = {
    "compute_income_tax": [
        (step.step_id, step.module_ref, step.action_ref, False) for step in PLAN_STEP_DEFINITIONS
    ],
}


class PlanRejectedContext(TypedDict):
    """Represent deterministic rejected context for plan validation failures."""

    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    tax_domain: str
    prompt_class: str


class PlanValidationError(TypedDict):
    """Represent canonical deterministic plan-validation rejection payload."""

    error_code: str
    message: str
    reason: str
    reason_code: NotRequired[str]
    rejected_context: PlanRejectedContext
    correlation_id: str | None
    trace_id: str | None


class PlanValidationResult(TypedDict):
    """Represent deterministic dispatch-guard validation outcome."""

    validation_status: str
    error: PlanValidationError | None


def validate_income_tax_intent_plan_for_dispatch(
    plan: Mapping[str, object],
) -> PlanValidationResult:
    """Validate translated plan for deterministic dispatch guard enforcement."""

    for field_name in REQUIRED_TOP_LEVEL_FIELDS:
        value = plan.get(field_name)
        if value is None:
            return _rejected(
                plan=plan,
                reason="malformed_plan",
                message=f"Plan is missing required field '{field_name}'.",
            )
        if isinstance(value, str) and not value.strip():
            return _rejected(
                plan=plan,
                reason="malformed_plan",
                message=f"Plan required field '{field_name}' is empty.",
            )

    intent_class = plan.get("intent_class")
    if not isinstance(intent_class, str) or intent_class not in SUPPORTED_INTENT_IDS:
        return _rejected(
            plan=plan,
            reason="unsupported_intent_class",
            message="Plan intent is not supported for deterministic dispatch.",
        )

    if plan.get("plan_status") != "planned":
        return _rejected(
            plan=plan,
            reason="malformed_plan",
            message="Plan status must be 'planned' before dispatch.",
        )

    supported_lane_id = plan.get("supported_lane_id")
    historical_version_id = plan.get("historical_version_id")
    tax_year = plan.get("tax_year")
    if (
        not isinstance(supported_lane_id, str)
        or not supported_lane_id.strip()
        or not isinstance(historical_version_id, str)
        or not historical_version_id.strip()
        or not isinstance(tax_year, int)
    ):
        return _rejected(
            plan=plan,
            reason="missing_lane_context",
            message="Plan lane/version context is missing or invalid.",
        )

    steps_value = plan.get("steps")
    if not isinstance(steps_value, list):
        return _rejected(
            plan=plan,
            reason="malformed_plan_steps",
            message="Plan must contain one or more deterministic dispatch steps.",
        )
    typed_steps = cast(list[object], steps_value)
    if len(typed_steps) == 0:
        return _rejected(
            plan=plan,
            reason="malformed_plan_steps",
            message="Plan must contain one or more deterministic dispatch steps.",
        )

    allowed_step_sequence = ALLOWED_STEP_SEQUENCE_BY_INTENT[intent_class]
    if len(typed_steps) != len(allowed_step_sequence):
        return _rejected(
            plan=plan,
            reason="unsupported_step_sequence",
            message="Plan step count is outside deterministic allowlisted sequence for intent.",
        )

    for index, step_value in enumerate(typed_steps):
        if not isinstance(step_value, Mapping):
            return _rejected(
                plan=plan,
                reason="malformed_plan_steps",
                message="Plan step must be an object mapping.",
            )
        step = cast(Mapping[str, object], step_value)
        for field_name in REQUIRED_STEP_FIELDS:
            if step.get(field_name) is None:
                return _rejected(
                    plan=plan,
                    reason="malformed_plan_steps",
                    message=f"Plan step is missing required field '{field_name}'.",
                )

        step_order = step.get("step_order")
        step_id = step.get("step_id")
        module_ref = step.get("module_ref")
        action_ref = step.get("action_ref")
        external_action = step.get("external_action")
        if (
            not isinstance(step_order, int)
            or not isinstance(step_id, str)
            or not isinstance(module_ref, str)
            or not isinstance(action_ref, str)
        ):
            return _rejected(
                plan=plan,
                reason="malformed_plan_steps",
                message="Plan step action fields must be strings.",
            )

        expected_step_order = index + 1
        if step_order != expected_step_order:
            return _rejected(
                plan=plan,
                reason="unsupported_step_sequence",
                message="Plan steps are not ordered deterministically by step_order.",
            )

        if external_action is not False:
            return _rejected(
                plan=plan,
                reason="unsupported_external_action",
                message="Plan step cannot request external side-effect action at this phase.",
            )

        expected_step = allowed_step_sequence[index]
        step_tuple = (step_id, module_ref, action_ref, external_action)
        if step_tuple != expected_step:
            return _rejected(
                plan=plan,
                reason="unsupported_step_action",
                message="Plan step action is outside deterministic allowlisted supported scope.",
            )

    try:
        manifest = load_income_tax_vertical_slice_manifest()
        assert_supported_lane(
            manifest,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )
    except CapabilityManifestError:
        return _rejected(
            plan=plan,
            reason="unsupported_lane_context",
            message="Plan lane/version context is outside manifest-supported capability.",
        )

    emit_income_tax_audit_event(
        event_type="plan_validated",
        status="accepted",
        correlation_id=_optional_str(plan.get("correlation_id")),
        trace_id=_optional_str(plan.get("trace_id")),
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        context={"plan_id": _optional_str(plan.get("plan_id"))},
    )
    return {
        "validation_status": "accepted",
        "error": None,
    }


def _rejected(
    *,
    plan: Mapping[str, object],
    reason: str,
    message: str,
) -> PlanValidationResult:
    correlation_id = _optional_str(plan.get("correlation_id"))
    trace_id = _optional_str(plan.get("trace_id")) or build_optional_trace_id(correlation_id)
    supported_lane_id = _optional_str(plan.get("supported_lane_id"))
    historical_version_id = _optional_str(plan.get("historical_version_id"))
    tax_year = _optional_int(plan.get("tax_year"))
    emit_income_tax_audit_event(
        event_type="plan_validated",
        status="rejected",
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        context={"reason": reason, "message": message},
    )
    return {
        "validation_status": "rejected",
        "error": {
            "error_code": "unsupported_prompt_scope",
            "message": message,
            "reason": reason,
            "rejected_context": {
                "supported_lane_id": _optional_str(plan.get("supported_lane_id")),
                "historical_version_id": _optional_str(plan.get("historical_version_id")),
                "tax_year": _optional_int(plan.get("tax_year")),
                "tax_domain": "income_tax",
                "prompt_class": "income_tax_prompt_flow",
            },
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        },
    }


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


_ROUTE_TARGETS_BY_ROUTE_ID: dict[str, tuple[str, str]] = {
    "income_tax_compute_route_v1": ("tax_core", "execute_computation"),
    "health_contribution_compute_route_v1": ("tax_core", "execute_computation"),
    "knowledge_search_route_v1": ("knowledge", "search_knowledge"),
    "knowledge_retrieve_route_v1": ("knowledge", "retrieve_knowledge"),
    "meta_conversation_route_v1": ("orchestration", "generate_meta_conversation_response"),
    "income_tax_form_generation_route_v1": ("forms", "generate_income_tax_form_artifact"),
    "health_contribution_form_mapping_route_v1": (
        "forms",
        "map_health_contribution_output_to_form_ready",
    ),
    "income_tax_report_generation_route_v1": ("reports", "create_income_tax_report_artifact"),
    "health_contribution_report_generation_route_v1": (
        "reports",
        "create_health_contribution_report_artifact",
    ),
    "income_tax_document_evidence_route_v1": ("document_ai", "search_document_evidence"),
}

_KNOWLEDGE_ONLY_ROUTE_IDS: frozenset[str] = frozenset(
    {"knowledge_search_route_v1", "knowledge_retrieve_route_v1"}
)

_ALLOWED_ROUTE_IDS_BY_DOMAIN: dict[str, frozenset[str]] = {
    "income_tax": frozenset(
        {
            "income_tax_compute_route_v1",
            "knowledge_search_route_v1",
            "knowledge_retrieve_route_v1",
            "income_tax_form_generation_route_v1",
            "income_tax_report_generation_route_v1",
            "income_tax_document_evidence_route_v1",
        }
    ),
    "health_contribution": frozenset(
        {
            "health_contribution_compute_route_v1",
            "knowledge_search_route_v1",
            "knowledge_retrieve_route_v1",
            "health_contribution_form_mapping_route_v1",
            "health_contribution_report_generation_route_v1",
        }
    ),
    # All knowledge-only domains share the same knowledge route IDs.
    "paye_generalized": _KNOWLEDGE_ONLY_ROUTE_IDS,
    "vat": _KNOWLEDGE_ONLY_ROUTE_IDS,
    "withholding_tax_generalized": _KNOWLEDGE_ONLY_ROUTE_IDS,
    "rental_income_generalized": _KNOWLEDGE_ONLY_ROUTE_IDS,
    "business_income_generalized": _KNOWLEDGE_ONLY_ROUTE_IDS,
    "general_tax": frozenset(
        {
            "knowledge_search_route_v1",
            "knowledge_retrieve_route_v1",
            "meta_conversation_route_v1",
        }
    ),
}

_SUPPORTED_MULTI_STEP_EXECUTION_TYPES = frozenset(
    {
        ("compute_plus_grounding", "income_tax"),
        ("compute_plus_grounding", "health_contribution"),
    }
)


def validate_governed_orchestration_plan(
    *,
    plan: Mapping[str, object],
    intent_class: str,
    tax_domain_hint: str,
    for_execution: bool,
) -> PlanValidationResult:
    """Validate governed canonical plan shapes for decisioning and execution gating."""

    required_top_level_fields = (
        "plan_id",
        "plan_version",
        "plan_status",
        "planning_mode",
        "execution_ready",
        "steps",
    )
    for field_name in required_top_level_fields:
        if field_name not in plan:
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="malformed_plan",
                message=f"Plan is missing required field '{field_name}'.",
            )
    if plan.get("plan_status") == "clarification_required":
        if plan.get("planning_mode") != "clarification_required":
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="malformed_plan",
                message="Clarification-required plans must use clarification planning mode.",
            )
        if plan.get("execution_ready") is not False:
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="illegal_plan_shape",
                message="Clarification-required plans cannot be execution-ready.",
            )
        return {"validation_status": "accepted", "error": None}

    planning_mode = plan.get("planning_mode")
    if planning_mode not in {"single_step", "multi_step"}:
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="malformed_plan",
            message="Plan planning_mode must be single_step or multi_step.",
        )
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="malformed_plan_steps",
            message="Plan must contain one or more governed steps.",
        )
    typed_steps = cast(list[object], steps)
    if len(typed_steps) == 0:
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="malformed_plan_steps",
            message="Plan must contain one or more governed steps.",
        )
    seen_step_ids: set[str] = set()
    step_ids: list[str] = []
    for step_value in typed_steps:
        if not isinstance(step_value, Mapping):
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="malformed_plan_steps",
                message="Plan steps must be object mappings.",
            )
        step = cast(Mapping[str, object], step_value)
        for field_name in (
            "step_id",
            "route_id",
            "target_service",
            "target_operation",
            "step_status",
            "depends_on",
        ):
            if field_name not in step:
                return _rejected_governed(
                    plan=plan,
                    intent_class=intent_class,
                    tax_domain_hint=tax_domain_hint,
                    reason="malformed_plan_steps",
                    message=f"Plan step is missing required field '{field_name}'.",
                )
        step_id = step.get("step_id")
        route_id = step.get("route_id")
        target_service = step.get("target_service")
        target_operation = step.get("target_operation")
        depends_on = step.get("depends_on")
        if (
            not isinstance(step_id, str)
            or not isinstance(route_id, str)
            or not isinstance(target_service, str)
            or not isinstance(target_operation, str)
            or not isinstance(depends_on, list)
        ):
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="malformed_plan_steps",
                message="Plan step field types are invalid.",
            )
        if step_id in seen_step_ids:
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="broken_dependency_graph",
                message="Plan step ids must be unique.",
            )
        seen_step_ids.add(step_id)
        step_ids.append(step_id)
        expected_route_target = _ROUTE_TARGETS_BY_ROUTE_ID.get(route_id)
        if expected_route_target is None:
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="illegal_route_target",
                message="Plan route is outside supported governed scope.",
            )
        if expected_route_target != (target_service, target_operation):
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="route_service_mismatch",
                message="Plan route target does not match the governed route mapping.",
            )
        if route_id not in _ALLOWED_ROUTE_IDS_BY_DOMAIN.get(tax_domain_hint, frozenset()):
            return _rejected_governed(
                plan=plan,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                reason="cross_domain_leakage",
                message="Plan route is outside the allowed domain-specific governed route set.",
            )
    step_id_set = set(step_ids)
    for step_value in typed_steps:
        step = cast(Mapping[str, object], step_value)
        step_id = cast(str, step["step_id"])
        depends_on = cast(list[object], step["depends_on"])
        for dependency in depends_on:
            if not isinstance(dependency, str) or dependency not in step_id_set:
                return _rejected_governed(
                    plan=plan,
                    intent_class=intent_class,
                    tax_domain_hint=tax_domain_hint,
                    reason="broken_dependency_graph",
                    message="Plan depends_on references must point to existing step ids.",
                )
            if dependency == step_id:
                return _rejected_governed(
                    plan=plan,
                    intent_class=intent_class,
                    tax_domain_hint=tax_domain_hint,
                    reason="cyclic_dependency_graph",
                    message="Plan steps cannot depend on themselves.",
                )
    if _contains_cycle(typed_steps):
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="cyclic_dependency_graph",
            message="Plan dependency graph contains a cycle.",
        )
    if intent_class.startswith("compute_") and not _plan_contains_target(
        typed_steps,
        target_service="tax_core",
        target_operation="execute_computation",
    ):
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="illegal_plan_missing_tax_core",
            message="Compute plan must contain a tax_core.execute_computation step.",
        )
    if intent_class in {
        "lookup_grounded_knowledge",
        "retrieve_grounded_knowledge",
        "compute_plus_grounding",
    } and not _plan_contains_target(
        typed_steps,
        target_service="knowledge",
        target_operation=None,
    ):
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="illegal_plan_missing_knowledge",
            message="Grounded plan must contain a knowledge step.",
        )
    if planning_mode == "single_step" and len(typed_steps) != 1:
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="illegal_plan_shape",
            message="Single-step plans must contain exactly one step.",
        )
    if planning_mode == "multi_step" and len(typed_steps) < 2:
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="illegal_plan_shape",
            message="Multi-step plans must contain at least two steps.",
        )
    execution_ready = plan.get("execution_ready")
    if not isinstance(execution_ready, bool):
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="malformed_plan",
            message="Plan execution_ready must be a boolean.",
        )
    if for_execution:
        if planning_mode == "single_step" and execution_ready:
            return {"validation_status": "accepted", "error": None}
        if (
            planning_mode == "multi_step"
            and execution_ready
            and (intent_class, tax_domain_hint) in _SUPPORTED_MULTI_STEP_EXECUTION_TYPES
            and _is_supported_compute_plus_grounding_execution_plan(
                typed_steps,
                tax_domain_hint=tax_domain_hint,
            )
        ):
            return {"validation_status": "accepted", "error": None}
        return _rejected_governed(
            plan=plan,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            reason="plan_execution_not_supported",
            message=(
                "Only execution-ready governed single-step plans and explicitly enabled "
                "multi-step plans can be dispatched in this phase."
            ),
        )
    return {"validation_status": "accepted", "error": None}


def _rejected_governed(
    *,
    plan: Mapping[str, object],
    intent_class: str,
    tax_domain_hint: str,
    reason: str,
    message: str,
) -> PlanValidationResult:
    error_code = _error_code_for_plan_contract_reason(reason)
    correlation_id = _optional_str(plan.get("correlation_id"))
    trace_id = _optional_str(plan.get("trace_id")) or build_optional_trace_id(correlation_id)
    return {
        "validation_status": "rejected",
        "error": {
            "error_code": error_code,
            "message": message,
            "reason": reason,
            "reason_code": reason,
            "rejected_context": {
                "supported_lane_id": None,
                "historical_version_id": None,
                "tax_year": None,
                "tax_domain": tax_domain_hint,
                "prompt_class": "income_tax_prompt_flow",
            },
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        },
    }


def _error_code_for_plan_contract_reason(reason: str) -> str:
    if reason in {
        "malformed_plan",
        "malformed_plan_steps",
        "illegal_plan_shape",
        "illegal_route_target",
        "route_service_mismatch",
        "cross_domain_leakage",
        "broken_dependency_graph",
        "cyclic_dependency_graph",
        "illegal_plan_missing_tax_core",
        "illegal_plan_missing_knowledge",
        "unsupported_step_sequence",
        "unsupported_step_action",
        "unsupported_external_action",
        "plan_execution_not_supported",
    }:
        return "orchestration_plan_contract_violation"
    return "unsupported_prompt_scope"


def _plan_contains_target(
    steps: list[object],
    *,
    target_service: str,
    target_operation: str | None,
) -> bool:
    for step_value in steps:
        step = cast(Mapping[str, object], step_value)
        if step.get("target_service") != target_service:
            continue
        if target_operation is not None and step.get("target_operation") != target_operation:
            continue
        return True
    return False


def _contains_cycle(steps: list[object]) -> bool:
    graph: dict[str, list[str]] = {}
    for step_value in steps:
        step = cast(Mapping[str, object], step_value)
        graph[cast(str, step["step_id"])] = [
            cast(str, item) for item in cast(list[object], step["depends_on"])
        ]
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visited:
            return False
        if step_id in visiting:
            return True
        visiting.add(step_id)
        for dependency in graph.get(step_id, []):
            if visit(dependency):
                return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    return any(visit(step_id) for step_id in graph)


def _is_supported_compute_plus_grounding_execution_plan(
    steps: list[object],
    *,
    tax_domain_hint: str,
) -> bool:
    if len(steps) != 2:
        return False
    first = cast(Mapping[str, object], steps[0])
    second = cast(Mapping[str, object], steps[1])
    expected_compute_route = (
        "income_tax_compute_route_v1"
        if tax_domain_hint == "income_tax"
        else "health_contribution_compute_route_v1"
    )
    first_step_id = cast(str, first["step_id"])
    return (
        first.get("route_id") == expected_compute_route
        and first.get("target_service") == "tax_core"
        and first.get("target_operation") == "execute_computation"
        and second.get("route_id") == "knowledge_search_route_v1"
        and second.get("target_service") == "knowledge"
        and second.get("target_operation") == "search_knowledge"
        and cast(list[object], second["depends_on"]) == [first_step_id]
    )
