"""Governed multi-step execution runtime for orchestration plans."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
import hashlib
from collections.abc import Mapping
from collections.abc import Callable

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.intent_plan_validator import validate_governed_orchestration_plan
from services.orchestration.app.action_execution_envelope import OrchestrationPlanStep
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import ActionExecutionEnvelope
from services.orchestration.app.action_execution_envelope import OrchestrationExecutionPlan
from services.orchestration.app.request_timer import timed_print

MultiStepStepStatus = Literal["planned", "running", "resolved", "blocked", "rejected"]


class MultiStepExecutionStepResult(TypedDict):
    """Represent one deterministic multi-step execution result entry."""

    step_id: str
    route_id: str
    target_service: str
    target_operation: str
    step_status: MultiStepStepStatus
    depends_on: list[str]
    step_purpose: str | None
    execution_id: str | None
    mapped_result: dict[str, object] | None
    adapter_response: dict[str, object] | None
    error: dict[str, object] | None


class MultiStepExecutionStepSummary(TypedDict):
    """Represent deterministic multi-step execution summary counts."""

    total_steps: int
    resolved_steps: int
    blocked_steps: int
    rejected_steps: int
    pending_steps: int
    accepted_steps: int


class MultiStepExecutionAggregate(TypedDict):
    """Represent one deterministic aggregated multi-step execution result."""

    execution_id: str
    plan: OrchestrationExecutionPlan
    step_results: list[MultiStepExecutionStepResult]
    step_summary: MultiStepExecutionStepSummary
    mapped_result: dict[str, object]
    grounded_evidence: list[dict[str, object]] | None
    grounding_status: Literal["grounded", "not_applicable"]
    errors: list[dict[str, object]] | None


def execute_governed_multi_step_plan(
    *,
    plan: OrchestrationExecutionPlan,
    intent_class: str,
    tax_domain_hint: str,
    idempotency_key: str,
    correlation_id: str,
    trace_id: str,
    submission_payload_ref: str,
    capability_context: dict[str, object],
    auth_context: dict[str, str | None],
    knowledge_route_payload: dict[str, object] | None,
    resolve_action_type: Callable[..., str | None],
    dispatch_with_envelope: Callable[[ActionExecutionRequest], ActionExecutionEnvelope],
) -> MultiStepExecutionAggregate:
    """Execute one supported governed multi-step plan deterministically."""

    validation = validate_governed_orchestration_plan(
        plan=cast(Mapping[str, object], plan),
        intent_class=intent_class,
        tax_domain_hint=tax_domain_hint,
        for_execution=True,
    )
    if validation["validation_status"] != "accepted":
        error = validation["error"] or {}
        raise ValueError(str(error.get("reason", "plan_execution_not_supported")))

    plan_id = plan["plan_id"]
    step_results: list[MultiStepExecutionStepResult] = []
    dependency_status: dict[str, MultiStepStepStatus] = {}
    grounded_evidence: list[dict[str, object]] | None = None
    errors: list[dict[str, object]] = []

    for step in plan["steps"]:
        step_result = _build_initial_step_result(step=step)
        unmet_dependencies = [
            dependency_id
            for dependency_id in step["depends_on"]
            if dependency_status.get(dependency_id) != "resolved"
        ]
        if unmet_dependencies:
            step_result["step_status"] = "blocked"
            step_result["error"] = {
                "error_code": "step_execution_blocked",
                "message": (
                    "Dependent governed plan step was blocked because prerequisite "
                    "execution did not resolve successfully."
                ),
                "reason": "blocked_dependency_chain",
                "reason_code": "blocked_dependency_chain",
                "context": {
                    "step_id": step["step_id"],
                    "depends_on": step["depends_on"],
                    "blocked_dependencies": unmet_dependencies,
                    "plan_id": plan_id,
                },
            }
            dependency_status[step["step_id"]] = "blocked"
            errors.append(step_result["error"])
            emit_income_tax_audit_event(
                event_type="multi_step_execution_blocked",
                status="blocked",
                correlation_id=correlation_id,
                trace_id=trace_id,
                supported_lane_id=cast(str | None, capability_context.get("supported_lane_id")),
                historical_version_id=cast(
                    str | None,
                    capability_context.get("historical_version_id"),
                ),
                tax_year=cast(int | None, capability_context.get("tax_year")),
                context={
                    "resource_id": _build_multi_step_execution_id(
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        plan_id=plan_id,
                    ),
                    "plan_id": plan_id,
                    "step_id": step["step_id"],
                    "blocked_dependencies": unmet_dependencies,
                    "tenant_id": auth_context.get("tenant_id"),
                    "user_id": auth_context.get("user_id"),
                },
            )
            step_results.append(step_result)
            continue

        action_type = resolve_action_type(
            target_service=step["target_service"],
            target_operation=step["target_operation"],
        )
        if action_type is None:
            step_result["step_status"] = "rejected"
            step_result["error"] = {
                "error_code": "unsupported_orchestration_route",
                "message": "Governed plan step route target is outside supported execution scope.",
                "reason": "unsupported_route_target",
                "reason_code": "unsupported_route_target",
                "context": {
                    "step_id": step["step_id"],
                    "route_id": step["route_id"],
                    "target_service": step["target_service"],
                    "target_operation": step["target_operation"],
                    "plan_id": plan_id,
                },
            }
            dependency_status[step["step_id"]] = "rejected"
            errors.append(step_result["error"])
            step_results.append(step_result)
            continue

        step_result["step_status"] = "running"
        emit_income_tax_audit_event(
            event_type="multi_step_execution_step_requested",
            status="requested",
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=cast(str | None, capability_context.get("supported_lane_id")),
            historical_version_id=cast(
                str | None,
                capability_context.get("historical_version_id"),
            ),
            tax_year=cast(int | None, capability_context.get("tax_year")),
            context={
                "resource_id": _build_multi_step_execution_id(
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    plan_id=plan_id,
                ),
                "plan_id": plan_id,
                "step_id": step["step_id"],
                "route_id": step["route_id"],
                "tenant_id": auth_context.get("tenant_id"),
                "user_id": auth_context.get("user_id"),
            },
        )
        execution_request: ActionExecutionRequest = {
            "idempotency_key": _build_step_idempotency_key(
                idempotency_key=idempotency_key,
                plan_id=plan_id,
                step_id=step["step_id"],
            ),
            "correlation_id": correlation_id,
            "action_type": action_type,
            "submission_payload_ref": submission_payload_ref,
            "capability_context": {
                "supported_lane_id": cast(str | None, capability_context.get("supported_lane_id")),
                "historical_version_id": cast(
                    str | None,
                    capability_context.get("historical_version_id"),
                ),
                "tax_year": cast(int | None, capability_context.get("tax_year")),
            },
            "trace_id": trace_id,
            "route_id": step["route_id"],
            "target_service": step["target_service"],
            "target_operation": step["target_operation"],
            "plan_id": plan_id,
            "step_id": step["step_id"],
            "auth_context": auth_context,
        }
        route_payload = _route_payload_for_step(
            step=step,
            knowledge_route_payload=knowledge_route_payload,
            prior_step_results=step_results,
        )
        if route_payload is not None:
            execution_request["route_payload"] = route_payload
        timed_print(
            "[EXECUTE] About to dispatch governed plan step "
            f"step_id={step['step_id']} route_id={step['route_id']}"
        )
        envelope = dispatch_with_envelope(execution_request)
        timed_print(
            "[EXECUTE] Dispatched governed plan step "
            f"step_id={step['step_id']} route_id={step['route_id']}"
        )
        step_result["execution_id"] = envelope["execution_id"]
        step_result["mapped_result"] = cast(dict[str, object], envelope["mapped_result"])
        adapter_response = cast(dict[str, object] | None, envelope.get("adapter_response"))
        step_result["adapter_response"] = adapter_response
        if envelope["execution_status"] != "resolved":
            step_result["step_status"] = "rejected"
            step_result["error"] = cast(dict[str, object] | None, envelope.get("error"))
        else:
            action_status = str(
                cast(dict[str, object], envelope["mapped_result"]).get(
                    "action_status",
                    "rejected",
                )
            )
            if action_status in {"accepted", "pending"}:
                step_result["step_status"] = "resolved"
                if adapter_response is not None:
                    step_grounded = _extract_grounded_evidence(adapter_response)
                    if step_grounded is not None:
                        grounded_evidence = step_grounded
            else:
                step_result["step_status"] = "rejected"
                step_result["error"] = cast(dict[str, object] | None, envelope.get("error"))
                if step_result["error"] is None:
                    step_result["error"] = {
                        "error_code": "multi_step_adapter_rejected",
                        "message": (
                            "Governed plan step was rejected after deterministic adapter dispatch."
                        ),
                        "reason": str(
                            cast(dict[str, object], envelope["mapped_result"]).get(
                                "reason",
                                "adapter_execution_rejected",
                            )
                        ),
                        "reason_code": str(
                            cast(dict[str, object], envelope["mapped_result"]).get(
                                "reason_code",
                                "adapter_execution_rejected",
                            )
                        ),
                        "context": {
                            "step_id": step["step_id"],
                            "plan_id": plan_id,
                        },
                    }
        dependency_status[step["step_id"]] = step_result["step_status"]
        if step_result["error"] is not None:
            errors.append(step_result["error"])
        step_results.append(step_result)

    summary = _build_step_summary(step_results)
    aggregate_plan = _apply_step_statuses(plan=plan, step_results=step_results)
    mapped_result = _aggregate_mapped_result(
        step_results=step_results,
        correlation_id=correlation_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
    )
    return {
        "execution_id": _build_multi_step_execution_id(
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            plan_id=plan_id,
        ),
        "plan": aggregate_plan,
        "step_results": step_results,
        "step_summary": summary,
        "mapped_result": mapped_result,
        "grounded_evidence": grounded_evidence,
        "grounding_status": "grounded" if grounded_evidence is not None else "not_applicable",
        "errors": errors or None,
    }


def _build_initial_step_result(*, step: OrchestrationPlanStep) -> MultiStepExecutionStepResult:
    return {
        "step_id": step["step_id"],
        "route_id": step["route_id"],
        "target_service": step["target_service"],
        "target_operation": step["target_operation"],
        "step_status": "planned",
        "depends_on": list(step["depends_on"]),
        "step_purpose": step.get("step_purpose"),
        "execution_id": None,
        "mapped_result": None,
        "adapter_response": None,
        "error": None,
    }


def _route_payload_for_step(
    *,
    step: OrchestrationPlanStep,
    knowledge_route_payload: dict[str, object] | None,
    prior_step_results: list[MultiStepExecutionStepResult],
) -> dict[str, object] | None:
    if step["target_service"] != "knowledge":
        return None
    if knowledge_route_payload is None:
        return None
    base = dict(knowledge_route_payload)
    compute_context = _extract_compute_result_context(prior_step_results)
    if compute_context:
        base = _enrich_knowledge_payload(base, compute_context)
    return base


def _extract_compute_result_context(
    prior_step_results: list[MultiStepExecutionStepResult],
) -> dict[str, object]:
    """Extract key fields from a resolved tax_core step to guide knowledge search."""
    for step_result in prior_step_results:
        if step_result["target_service"] != "tax_core":
            continue
        if step_result["step_status"] != "resolved":
            continue
        adapter_response = step_result.get("adapter_response")
        if not isinstance(adapter_response, dict):
            continue
        result_payload = adapter_response.get("result_payload")
        if not isinstance(result_payload, dict):
            continue
        rp = cast(dict[str, object], result_payload)
        context: dict[str, object] = {}
        for field in (
            "applicable_rate",
            "tax_bracket",
            "effective_rate",
            "marginal_rate",
            "tax_liability",
            "gross_income",
            "taxable_income",
            "regime_identifier",
            "historical_version_id",
        ):
            value = rp.get(field)
            if value is not None:
                context[field] = value
        return context
    return {}


def _enrich_knowledge_payload(
    base: dict[str, object],
    compute_context: dict[str, object],
) -> dict[str, object]:
    """Rewrite the knowledge query to include compute result context."""
    original_query = base.get("query")
    if not isinstance(original_query, str) or not original_query.strip():
        return base

    # Build a focused enrichment clause from available compute signals.
    clauses: list[str] = []
    applicable_rate = compute_context.get("applicable_rate")
    tax_bracket = compute_context.get("tax_bracket")
    regime = compute_context.get("regime_identifier")
    historical_version = compute_context.get("historical_version_id")

    if applicable_rate is not None:
        clauses.append(f"applicable rate {applicable_rate}%")
    elif tax_bracket is not None:
        clauses.append(f"tax bracket {tax_bracket}")
    if regime is not None:
        clauses.append(f"under {regime} regime")
    if historical_version is not None:
        clauses.append(f"version {historical_version}")

    if not clauses:
        return base

    enriched_query = f"{original_query.rstrip()} — {', '.join(clauses)}"
    enriched = dict(base)
    enriched["query"] = enriched_query
    enriched["compute_context"] = compute_context
    return enriched


def _apply_step_statuses(
    *,
    plan: OrchestrationExecutionPlan,
    step_results: list[MultiStepExecutionStepResult],
) -> OrchestrationExecutionPlan:
    status_by_step_id = {step["step_id"]: step["step_status"] for step in step_results}
    updated_steps: list[OrchestrationPlanStep] = []
    for step in plan["steps"]:
        updated_steps.append(
            {
                **step,
                "step_status": status_by_step_id.get(step["step_id"], step["step_status"]),
            }
        )
    return {
        **plan,
        "steps": updated_steps,
    }


def _aggregate_mapped_result(
    *,
    step_results: list[MultiStepExecutionStepResult],
    correlation_id: str,
    trace_id: str,
    idempotency_key: str,
) -> dict[str, object]:
    for step_result in step_results:
        mapped_result = step_result.get("mapped_result")
        if not isinstance(mapped_result, dict):
            continue
        action_status = str(mapped_result.get("action_status", "rejected"))
        if action_status in {"rejected", "retryable_failure"}:
            return {
                **mapped_result,
                "failed_step_id": step_result["step_id"],
                "failed_route_id": step_result["route_id"],
            }
    if any(step["step_status"] == "blocked" for step in step_results):
        blocked_step = next(step for step in step_results if step["step_status"] == "blocked")
        return {
            "action_status": "rejected",
            "reason_code": "blocked_dependency_chain",
            "reason": (
                "Governed multi-step execution was blocked because one or more prerequisite "
                "steps failed to resolve."
            ),
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": None,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
            "failed_step_id": blocked_step["step_id"],
            "failed_route_id": blocked_step["route_id"],
        }
    if any(
        isinstance(step.get("mapped_result"), dict)
        and cast(dict[str, object], step["mapped_result"]).get("action_status") == "pending"
        for step in step_results
    ):
        return {
            "action_status": "pending",
            "reason_code": "multi_step_execution_pending",
            "reason": (
                "One or more governed plan steps remain provider-pending after "
                "deterministic multi-step dispatch."
            ),
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": None,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
    return {
        "action_status": "accepted",
        "reason_code": "multi_step_execution_resolved",
        "reason": "Governed multi-step execution resolved deterministically.",
        "retryable": False,
        "next_retry_at": None,
        "provider_reference": None,
        "correlation_id": correlation_id,
        "idempotency_key": idempotency_key,
        "trace_id": trace_id,
    }


def _build_step_summary(
    step_results: list[MultiStepExecutionStepResult],
) -> MultiStepExecutionStepSummary:
    resolved_steps = sum(1 for step in step_results if step["step_status"] == "resolved")
    blocked_steps = sum(1 for step in step_results if step["step_status"] == "blocked")
    rejected_steps = sum(1 for step in step_results if step["step_status"] == "rejected")
    pending_steps = sum(
        1
        for step in step_results
        if isinstance(step.get("mapped_result"), dict)
        and cast(dict[str, object], step["mapped_result"]).get("action_status") == "pending"
    )
    accepted_steps = sum(
        1
        for step in step_results
        if isinstance(step.get("mapped_result"), dict)
        and cast(dict[str, object], step["mapped_result"]).get("action_status") == "accepted"
    )
    return {
        "total_steps": len(step_results),
        "resolved_steps": resolved_steps,
        "blocked_steps": blocked_steps,
        "rejected_steps": rejected_steps,
        "pending_steps": pending_steps,
        "accepted_steps": accepted_steps,
    }


def _build_step_idempotency_key(
    *,
    idempotency_key: str,
    plan_id: str,
    step_id: str,
) -> str:
    return _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "governed_multi_step_execution_step",
                "idempotency_key": idempotency_key,
                "plan_id": plan_id,
                "step_id": step_id,
            }
        )
    )


def _build_multi_step_execution_id(
    *,
    correlation_id: str,
    idempotency_key: str,
    plan_id: str,
) -> str:
    return _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "governed_multi_step_execution",
                "correlation_id": correlation_id,
                "idempotency_key": idempotency_key,
                "plan_id": plan_id,
            }
        )
    )


def _extract_grounded_evidence(
    adapter_response: dict[str, object],
) -> list[dict[str, object]] | None:
    result_payload = adapter_response.get("result_payload")
    if not isinstance(result_payload, dict):
        return None
    result_payload_dict = cast(dict[str, object], result_payload)
    grounded_evidence = result_payload_dict.get("grounded_evidence")
    if not isinstance(grounded_evidence, list):
        return None
    normalized: list[dict[str, object]] = []
    for item in cast(list[object], grounded_evidence):
        if isinstance(item, Mapping):
            typed_item = cast(Mapping[object, object], item)
            normalized.append({str(key): typed_item[key] for key in typed_item})
    return normalized


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
