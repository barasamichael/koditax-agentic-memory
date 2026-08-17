"""Additional deterministic orchestration regression coverage."""
from __future__ import annotations

from typing import cast

from services.orchestration.app.intent_plan_validator import validate_governed_orchestration_plan
from services.orchestration.app.intent_to_plan import build_governed_orchestration_plan
from services.orchestration.app.prompt_intent_envelope import parse_income_tax_prompt_intent_envelope


def test_legacy_income_tax_endpoint_parser_remains_isolated() -> None:
    envelope = parse_income_tax_prompt_intent_envelope("What is VAT?")
    assert envelope["tax_domain_hint"] == "vat"
    assert envelope["intent_class"] == "lookup_grounded_knowledge"


def test_general_tax_uses_normal_knowledge_plan() -> None:
    envelope = {"normalized_prompt_text":"what about fish tax?", "tax_domain_hint":"general_tax", "requested_lane_hint":None, "historical_version_hint":None, "tax_year_hint":None, "intent_class":"lookup_grounded_knowledge", "parsing_status":"parsed_with_turn_resolution", "prompt_class":"orchestration_prompt_flow", "correlation_id":"corr", "trace_id":"trace", "knowledge_route_mode_hint":"search"}
    plan = build_governed_orchestration_plan(cast(dict[str, object], envelope))
    assert plan["steps"][0]["route_id"] == "knowledge_search_route_v1"


def test_illegal_plan_shape_maps_to_plan_contract_violation() -> None:
    plan = {"plan_id":"plan-1", "plan_version":"2.0.0", "plan_status":"planned", "planning_mode":"multi_step", "execution_ready":False, "steps":[{"step_id":"step-1", "route_id":"knowledge_search_route_v1", "target_service":"knowledge", "target_operation":"search_knowledge", "step_status":"planned", "depends_on":[], "step_purpose":"grounded_authority_lookup"}]}
    result = validate_governed_orchestration_plan(plan=plan, intent_class="lookup_grounded_knowledge", tax_domain_hint="vat", for_execution=False)
    assert result["validation_status"] == "rejected"
    assert result["error"]["reason"] == "illegal_plan_shape"
