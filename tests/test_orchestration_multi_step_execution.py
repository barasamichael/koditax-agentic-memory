"""Focused tests for governed multi-step orchestration execution."""

from __future__ import annotations

from typing import cast

import pytest

from services.orchestration.app.multi_step_execution import execute_governed_multi_step_plan
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import ActionExecutionEnvelope
from services.orchestration.app.action_execution_envelope import OrchestrationExecutionPlan


def test_supported_compute_plus_grounding_multi_step_executes_deterministically() -> None:
    dispatched: list[str] = []

    def dispatch_with_envelope(
        request: ActionExecutionRequest,
    ) -> ActionExecutionEnvelope:
        step_id = request.get("step_id")
        assert isinstance(step_id, str)
        dispatched.append(step_id)
        if request["action_type"] == "tax_core_execute_computation":
            return _execution_envelope(
                execution_id="exec-step-compute-001",
                mapped_result={
                    "action_status": "pending",
                    "reason_code": "tax_core_action_mock_pending",
                    "reason": "Tax-core route accepted by deterministic adapter registry.",
                    "retryable": False,
                    "next_retry_at": None,
                    "provider_reference": None,
                    "correlation_id": "corr-multi-step-001",
                    "idempotency_key": request["idempotency_key"],
                    "trace_id": "trace-multi-step-001",
                },
                adapter_response={
                    "adapter_status": "mock_pending",
                    "provider_reference": None,
                    "action_result_code": "tax_core_action_mock_pending",
                    "message": "Tax-core route accepted by deterministic adapter registry.",
                    "trace": {
                        "correlation_id": "corr-multi-step-001",
                        "trace_id": "trace-multi-step-001",
                        "adapter_request_id": "adapter-compute-001",
                        "adapter_name": "deterministic_tax_core_adapter_v1",
                        "submission_payload_ref": "prompt-checksum-001",
                    },
                    "error": None,
                },
            )
        return _execution_envelope(
            execution_id="exec-step-knowledge-001",
            mapped_result={
                "action_status": "accepted",
                "reason_code": "knowledge_lookup_resolved",
                "reason": (
                    "Knowledge route resolved to governed grounded evidence deterministically."
                ),
                "retryable": False,
                "next_retry_at": None,
                "provider_reference": None,
                "correlation_id": "corr-multi-step-001",
                "idempotency_key": request["idempotency_key"],
                "trace_id": "trace-multi-step-001",
            },
            adapter_response={
                "adapter_status": "accepted",
                "provider_reference": None,
                "action_result_code": "knowledge_lookup_resolved",
                "message": (
                    "Knowledge route resolved to governed grounded evidence deterministically."
                ),
                "trace": {
                    "correlation_id": "corr-multi-step-001",
                    "trace_id": "trace-multi-step-001",
                    "adapter_request_id": "adapter-knowledge-001",
                    "adapter_name": "deterministic_knowledge_adapter_v1",
                    "submission_payload_ref": "prompt-checksum-001",
                },
                "error": None,
                "result_payload": {
                    "grounding_status": "grounded",
                    "grounded_evidence": [
                        {
                            "source_id": "KNW-ITA-15-2",
                            "source_version_id": "123e4567-e89b-12d3-a456-426614174100",
                            "anchor_id": "income-tax-act-15-2",
                            "title": "Income Tax Act (Cap. 470), Section 15(2)",
                            "url": "https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                            "source_type": "tax_law",
                            "authority_level": "statute",
                            "tax_domain": "income_tax",
                            "effective_from": "1974-01-01",
                            "effective_to": None,
                            "tax_year": None,
                            "publication_state": "published",
                            "source_version_form": "point_in_time_consolidation",
                            "grounding_status": "grounded",
                        }
                    ],
                },
            },
        )

    aggregate = execute_governed_multi_step_plan(
        plan=_compute_plus_grounding_plan("income_tax"),
        intent_class="compute_plus_grounding",
        tax_domain_hint="income_tax",
        idempotency_key="idem-multi-step-001",
        correlation_id="corr-multi-step-001",
        trace_id="trace-multi-step-001",
        submission_payload_ref="prompt-checksum-001",
        capability_context={
            "supported_lane_id": "income_tax_resident_employment_v1_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
        auth_context={"tenant_id": "pilot_tenant_alpha", "user_id": "user_alpha_001"},
        knowledge_route_payload={
            "query": "allowable deductions",
            "source_type": "tax_law",
            "tax_domain": "income_tax",
            "effective_date": "2023-07-01",
        },
        resolve_action_type=_resolve_action_type,
        dispatch_with_envelope=dispatch_with_envelope,
    )

    assert [step["route_id"] for step in aggregate["step_results"]] == [
        "income_tax_compute_route_v1",
        "knowledge_search_route_v1",
    ]
    assert dispatched == [
        aggregate["step_results"][0]["step_id"],
        aggregate["step_results"][1]["step_id"],
    ]
    assert aggregate["step_results"][0]["step_status"] == "resolved"
    assert aggregate["step_results"][1]["step_status"] == "resolved"
    assert aggregate["step_results"][1]["depends_on"] == [aggregate["step_results"][0]["step_id"]]
    assert aggregate["mapped_result"]["action_status"] == "pending"
    assert aggregate["grounding_status"] == "grounded"
    assert aggregate["grounded_evidence"] is not None
    assert aggregate["step_summary"] == {
        "total_steps": 2,
        "resolved_steps": 2,
        "blocked_steps": 0,
        "rejected_steps": 0,
        "pending_steps": 1,
        "accepted_steps": 1,
    }


def test_failed_upstream_step_blocks_downstream_execution() -> None:
    dispatched: list[str] = []

    def dispatch_with_envelope(
        request: ActionExecutionRequest,
    ) -> ActionExecutionEnvelope:
        step_id = request.get("step_id")
        assert isinstance(step_id, str)
        dispatched.append(step_id)
        return _execution_envelope(
            execution_id="exec-step-compute-failed-001",
            mapped_result={
                "action_status": "rejected",
                "reason_code": "provider_rejected",
                "reason": "Tax-core adapter rejected the governed route.",
                "retryable": False,
                "next_retry_at": None,
                "provider_reference": None,
                "correlation_id": "corr-multi-step-failed-001",
                "idempotency_key": request["idempotency_key"],
                "trace_id": "trace-multi-step-failed-001",
            },
            adapter_response={
                "adapter_status": "unsupported",
                "provider_reference": None,
                "action_result_code": "provider_rejected",
                "message": "Tax-core adapter rejected the governed route.",
                "trace": {
                    "correlation_id": "corr-multi-step-failed-001",
                    "trace_id": "trace-multi-step-failed-001",
                    "adapter_request_id": "adapter-compute-failed-001",
                    "adapter_name": "deterministic_tax_core_adapter_v1",
                    "submission_payload_ref": "prompt-checksum-failed-001",
                },
                "error": {
                    "error_code": "provider_rejected",
                    "message": "Tax-core adapter rejected the governed route.",
                    "reason_code": "provider_rejected",
                    "reason": "provider_rejected",
                    "rejected_context": {
                        "action_type": "tax_core_execute_computation",
                        "supported_lane_id": None,
                        "historical_version_id": None,
                        "tax_year": None,
                        "correlation_id": "corr-multi-step-failed-001",
                    },
                    "required_controls": ["revise_prompt_scope"],
                    "next_allowed_actions": ["revise_input", "reject"],
                    "trace_id": "trace-multi-step-failed-001",
                },
            },
        )

    aggregate = execute_governed_multi_step_plan(
        plan=_compute_plus_grounding_plan("income_tax"),
        intent_class="compute_plus_grounding",
        tax_domain_hint="income_tax",
        idempotency_key="idem-multi-step-failed-001",
        correlation_id="corr-multi-step-failed-001",
        trace_id="trace-multi-step-failed-001",
        submission_payload_ref="prompt-checksum-failed-001",
        capability_context={
            "supported_lane_id": "income_tax_resident_employment_v1_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
        auth_context={"tenant_id": "pilot_tenant_alpha", "user_id": "user_alpha_001"},
        knowledge_route_payload={"query": "allowable deductions"},
        resolve_action_type=_resolve_action_type,
        dispatch_with_envelope=dispatch_with_envelope,
    )

    assert dispatched == [aggregate["step_results"][0]["step_id"]]
    assert aggregate["step_results"][0]["step_status"] == "rejected"
    assert aggregate["step_results"][1]["step_status"] == "blocked"
    assert aggregate["mapped_result"]["action_status"] == "rejected"
    assert aggregate["mapped_result"]["failed_step_id"] == aggregate["step_results"][0]["step_id"]


def test_missing_route_target_is_rejected_canonically() -> None:
    aggregate = execute_governed_multi_step_plan(
        plan=_compute_plus_grounding_plan("income_tax"),
        intent_class="compute_plus_grounding",
        tax_domain_hint="income_tax",
        idempotency_key="idem-multi-step-unsupported-001",
        correlation_id="corr-multi-step-unsupported-001",
        trace_id="trace-multi-step-unsupported-001",
        submission_payload_ref="prompt-checksum-unsupported-001",
        capability_context={
            "supported_lane_id": "income_tax_resident_employment_v1_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
        auth_context={"tenant_id": "pilot_tenant_alpha", "user_id": "user_alpha_001"},
        knowledge_route_payload={"query": "allowable deductions"},
        resolve_action_type=_unsupported_action_type,
        dispatch_with_envelope=lambda request: pytest.fail(
            f"dispatch should not run for unsupported route target: {request}"
        ),
    )

    assert aggregate["step_results"][0]["step_status"] == "rejected"
    assert aggregate["step_results"][0]["error"] is not None
    assert aggregate["step_results"][0]["error"]["reason_code"] == "unsupported_route_target"
    assert aggregate["mapped_result"]["action_status"] == "rejected"


def test_invalid_dependency_graph_is_rejected_before_execution() -> None:
    invalid_plan = _compute_plus_grounding_plan("income_tax")
    invalid_plan["steps"][1]["depends_on"] = ["missing-step"]

    with pytest.raises(ValueError, match="broken_dependency_graph"):
        execute_governed_multi_step_plan(
            plan=invalid_plan,
            intent_class="compute_plus_grounding",
            tax_domain_hint="income_tax",
            idempotency_key="idem-multi-step-invalid-graph-001",
            correlation_id="corr-multi-step-invalid-graph-001",
            trace_id="trace-multi-step-invalid-graph-001",
            submission_payload_ref="prompt-checksum-invalid-graph-001",
            capability_context={
                "supported_lane_id": "income_tax_resident_employment_v1_2023_07_01",
                "historical_version_id": "KIT-VER-20230701-A",
                "tax_year": 2023,
            },
            auth_context={"tenant_id": "pilot_tenant_alpha", "user_id": "user_alpha_001"},
            knowledge_route_payload={"query": "allowable deductions"},
            resolve_action_type=_resolve_action_type,
            dispatch_with_envelope=lambda request: pytest.fail(
                f"dispatch should not run for invalid graph: {request}"
            ),
        )


def _compute_plus_grounding_plan(tax_domain_hint: str) -> OrchestrationExecutionPlan:
    compute_route_id = (
        "income_tax_compute_route_v1"
        if tax_domain_hint == "income_tax"
        else "health_contribution_compute_route_v1"
    )
    return {
        "plan_id": f"plan-{tax_domain_hint}-compute-plus-grounding-001",
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": "multi_step",
        "execution_ready": True,
        "steps": [
            {
                "step_id": f"step-{tax_domain_hint}-compute-001",
                "route_id": compute_route_id,
                "target_service": "tax_core",
                "target_operation": "execute_computation",
                "step_status": "planned",
                "depends_on": [],
                "step_purpose": "tax_computation",
            },
            {
                "step_id": f"step-{tax_domain_hint}-knowledge-001",
                "route_id": "knowledge_search_route_v1",
                "target_service": "knowledge",
                "target_operation": "search_knowledge",
                "step_status": "planned",
                "depends_on": [f"step-{tax_domain_hint}-compute-001"],
                "step_purpose": "grounded_authority_lookup",
            },
        ],
    }


def _resolve_action_type(*, target_service: str, target_operation: str) -> str | None:
    mapping = {
        ("tax_core", "execute_computation"): "tax_core_execute_computation",
        ("knowledge", "search_knowledge"): "knowledge_search_knowledge",
    }
    return mapping.get((target_service, target_operation))


def _unsupported_action_type(*, target_service: str, target_operation: str) -> str | None:
    _ = (target_service, target_operation)
    return None


def _execution_envelope(
    *,
    execution_id: str,
    mapped_result: dict[str, object],
    adapter_response: dict[str, object],
) -> ActionExecutionEnvelope:
    return cast(
        ActionExecutionEnvelope,
        {
            "execution_id": execution_id,
            "idempotency_key": cast(str, mapped_result["idempotency_key"]),
            "correlation_id": cast(str, mapped_result["correlation_id"]),
            "request_fingerprint": f"fingerprint-{execution_id}",
            "plan": {
                "plan_id": f"plan-{execution_id}",
                "plan_version": "2.0.0",
                "plan_status": "planned",
                "planning_mode": "single_step",
                "execution_ready": True,
                "steps": [],
            },
            "action_context": {
                "action_type": "route_dispatch",
                "supported_lane_id": None,
                "historical_version_id": None,
                "tax_year": None,
            },
            "execution_status": "resolved",
            "adapter_response": adapter_response,
            "mapped_result": mapped_result,
            "error": None,
            "trace": {
                "execution_envelope_id": execution_id,
                "correlation_id": cast(str, mapped_result["correlation_id"]),
                "trace_id": cast(str, mapped_result["trace_id"]),
                "idempotency_key": cast(str, mapped_result["idempotency_key"]),
                "request_fingerprint": f"fingerprint-{execution_id}",
            },
        },
    )
