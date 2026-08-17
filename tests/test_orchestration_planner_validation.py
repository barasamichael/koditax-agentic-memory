"""Planner-validation tests for governed orchestration plans."""

from __future__ import annotations

from services.orchestration.app.intent_to_plan import build_governed_orchestration_plan
from services.orchestration.app.intent_plan_validator import validate_governed_orchestration_plan
from services.orchestration.app.prompt_intent_envelope import (
    parse_income_tax_prompt_intent_envelope,
)


def test_governed_planner_accepts_supported_single_step_compute_plan() -> None:
    envelope = parse_income_tax_prompt_intent_envelope(
        "compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    plan = build_governed_orchestration_plan(envelope)

    result = validate_governed_orchestration_plan(
        plan=plan,
        intent_class=envelope["intent_class"],
        tax_domain_hint=envelope["tax_domain_hint"],
        for_execution=True,
    )

    assert result["validation_status"] == "accepted"
    assert result["error"] is None


def test_governed_planner_rejects_missing_tax_core_step_for_compute_plan() -> None:
    plan: dict[str, object] = {
        "plan_id": "plan-001",
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": "single_step",
        "execution_ready": True,
        "steps": [
            {
                "step_id": "step-001",
                "route_id": "knowledge_search_route_v1",
                "target_service": "knowledge",
                "target_operation": "search_knowledge",
                "step_status": "planned",
                "depends_on": [],
                "step_purpose": "grounded_authority_lookup",
            }
        ],
    }

    result = validate_governed_orchestration_plan(
        plan=plan,
        intent_class="compute_income_tax",
        tax_domain_hint="income_tax",
        for_execution=True,
    )

    assert result["validation_status"] == "rejected"
    assert result["error"] is not None
    assert result["error"]["reason"] == "illegal_plan_missing_tax_core"


def test_governed_planner_rejects_route_service_mismatch() -> None:
    plan: dict[str, object] = {
        "plan_id": "plan-002",
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": "single_step",
        "execution_ready": True,
        "steps": [
            {
                "step_id": "step-002",
                "route_id": "income_tax_compute_route_v1",
                "target_service": "knowledge",
                "target_operation": "search_knowledge",
                "step_status": "planned",
                "depends_on": [],
                "step_purpose": "tax_computation",
            }
        ],
    }

    result = validate_governed_orchestration_plan(
        plan=plan,
        intent_class="compute_income_tax",
        tax_domain_hint="income_tax",
        for_execution=True,
    )

    assert result["validation_status"] == "rejected"
    assert result["error"] is not None
    assert result["error"]["reason"] == "route_service_mismatch"


def test_governed_planner_rejects_cyclic_multi_step_plan() -> None:
    plan: dict[str, object] = {
        "plan_id": "plan-003",
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": "multi_step",
        "execution_ready": False,
        "steps": [
            {
                "step_id": "step-a",
                "route_id": "income_tax_compute_route_v1",
                "target_service": "tax_core",
                "target_operation": "execute_computation",
                "step_status": "planned",
                "depends_on": ["step-b"],
                "step_purpose": "tax_computation",
            },
            {
                "step_id": "step-b",
                "route_id": "knowledge_search_route_v1",
                "target_service": "knowledge",
                "target_operation": "search_knowledge",
                "step_status": "planned",
                "depends_on": ["step-a"],
                "step_purpose": "grounded_authority_lookup",
            },
        ],
    }

    result = validate_governed_orchestration_plan(
        plan=plan,
        intent_class="compute_plus_grounding",
        tax_domain_hint="income_tax",
        for_execution=False,
    )

    assert result["validation_status"] == "rejected"
    assert result["error"] is not None
    assert result["error"]["reason"] == "cyclic_dependency_graph"


def test_governed_planner_rejects_cross_domain_route_leakage() -> None:
    plan: dict[str, object] = {
        "plan_id": "plan-004",
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": "single_step",
        "execution_ready": False,
        "steps": [
            {
                "step_id": "step-004",
                "route_id": "income_tax_document_evidence_route_v1",
                "target_service": "document_ai",
                "target_operation": "search_document_evidence",
                "step_status": "planned",
                "depends_on": [],
                "step_purpose": "document_extraction",
            }
        ],
    }

    result = validate_governed_orchestration_plan(
        plan=plan,
        intent_class="extract_document",
        tax_domain_hint="health_contribution",
        for_execution=False,
    )

    assert result["validation_status"] == "rejected"
    assert result["error"] is not None
    assert result["error"]["reason"] == "cross_domain_leakage"
