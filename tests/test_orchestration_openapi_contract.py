"""OpenAPI contract checks for orchestration runtime boundary."""

from __future__ import annotations

from typing import cast
from pathlib import Path

import yaml

from services.orchestration.app.main import create_app

CONTRACT_PATH = Path("contracts/openapi/orchestration.yaml")
REQUIRED_PATHS = {
    "/healthz",
    "/readyz",
    "/v1/orchestration/income-tax/execute",
    "/v1/orchestration/prompt/ingest",
    "/v1/orchestration/prompt/decide",
    "/v1/orchestration/prompt/execute",
}
REQUIRED_SCHEMAS = {
    "ErrorEnvelope",
    "GroundedAuthoritySummary",
    "GroundedExplanationCitation",
    "GroundedExplanationItem",
    "GroundedKnowledgeEvidence",
    "GroundedTemporalApplicability",
    "OrchestrationExecuteRequest",
    "OrchestrationExecuteResponse",
    "PromptIngestionRequest",
    "PromptIngestionResponse",
    "PromptDecisionResponse",
    "PromptClarificationRequirement",
    "PromptExecutionRequest",
    "PromptExecutionResponse",
    "ContradictionFinding",
    "FactMismatch",
    "ResponseIntegritySignals",
    "UnifiedAnswerCitation",
    "UnifiedAnswerSourceLocation",
    "UnifiedAnswerSourceReference",
    "UnifiedAnswerResponse",
    "OrchestrationActionSafetyContext",
    "OrchestrationExecutionPlan",
    "OrchestrationFinalOutcomeEnvelope",
    "OrchestrationFinalOutcomeTrace",
    "OrchestrationFinalOutcomeAudit",
    "OrchestrationPlanStep",
    "OrchestrationStepExecutionResult",
    "OrchestrationStepExecutionSummary",
}


def test_orchestration_openapi_contract_parses_and_declares_required_paths() -> None:
    document = _load_contract()
    assert document.get("openapi") == "3.1.0"
    description = cast(dict[str, object], document["info"])["description"]
    assert isinstance(description, str)
    assert "health_contribution routing" in description
    assert "governed knowledge lookup and timeline routing" in description
    assert "same-conversation follow-up resolution from prior governed" in description
    paths = _paths(document)
    missing = sorted(REQUIRED_PATHS - set(paths))
    assert not missing
    assert "post" in paths["/v1/orchestration/income-tax/execute"]
    assert "post" in paths["/v1/orchestration/prompt/ingest"]
    assert "post" in paths["/v1/orchestration/prompt/decide"]
    assert "post" in paths["/v1/orchestration/prompt/execute"]


def test_orchestration_openapi_contract_contains_canonical_error_envelope_fields() -> None:
    schemas = _schemas(_load_contract())
    error_schema = cast(dict[str, object], schemas["ErrorEnvelope"])
    required_list = cast(list[object], error_schema["required"])
    required = {str(item) for item in required_list}
    expected_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "correlation_id",
        "trace_id",
    }
    assert expected_fields.issubset(required)


def test_orchestration_openapi_contract_contains_required_schemas() -> None:
    schemas = _schemas(_load_contract())
    missing = sorted(REQUIRED_SCHEMAS - set(schemas))
    assert not missing


def test_orchestration_runtime_routes_match_required_openapi_surface() -> None:
    runtime_routes = _runtime_route_methods()
    assert "/healthz" in runtime_routes
    assert "get" in runtime_routes["/healthz"]
    assert "/readyz" in runtime_routes
    assert "get" in runtime_routes["/readyz"]
    assert "/v1/orchestration/income-tax/execute" in runtime_routes
    assert "post" in runtime_routes["/v1/orchestration/income-tax/execute"]
    assert "/v1/orchestration/prompt/ingest" in runtime_routes
    assert "post" in runtime_routes["/v1/orchestration/prompt/ingest"]
    assert "/v1/orchestration/prompt/decide" in runtime_routes
    assert "post" in runtime_routes["/v1/orchestration/prompt/decide"]
    assert "/v1/orchestration/prompt/execute" in runtime_routes
    assert "post" in runtime_routes["/v1/orchestration/prompt/execute"]


def test_orchestration_openapi_prompt_response_schemas_allow_health_identity_fields() -> None:
    schemas = _schemas(_load_contract())
    prompt_decision = cast(dict[str, object], schemas["PromptDecisionResponse"])
    prompt_execution = cast(dict[str, object], schemas["PromptExecutionResponse"])
    decision_properties = cast(dict[str, object], prompt_decision["properties"])
    execution_properties = cast(dict[str, object], prompt_execution["properties"])

    for property_name in ("supported_lane_id", "historical_version_id", "regime_identifier"):
        assert property_name in decision_properties
        assert property_name in execution_properties


def test_orchestration_openapi_prompt_execution_schema_allows_grounded_knowledge_fields() -> None:
    schemas = _schemas(_load_contract())
    prompt_execution = cast(dict[str, object], schemas["PromptExecutionResponse"])
    execution_properties = cast(dict[str, object], prompt_execution["properties"])

    assert "plan" in execution_properties
    assert "grounding_status" in execution_properties
    assert "grounded_evidence" in execution_properties
    assert "explanation_status" in execution_properties
    assert "explanation_items" in execution_properties
    assert "citations" in execution_properties
    assert "source_references" in execution_properties
    assert "authority_summary" in execution_properties
    assert "temporal_applicability" in execution_properties
    assert "step_results" in execution_properties
    assert "step_summary" in execution_properties
    assert "validation" in execution_properties
    assert "response" in execution_properties


def test_orchestration_openapi_prompt_execution_schema_allows_governed_validation_envelope() -> (
    None
):
    schemas = _schemas(_load_contract())
    prompt_execution = cast(dict[str, object], schemas["PromptExecutionResponse"])
    validation_schema = cast(
        dict[str, object],
        cast(dict[str, object], prompt_execution["properties"])["validation"],
    )
    validation_one_of = cast(list[object], validation_schema["oneOf"])
    assert {"$ref": "#/components/schemas/GovernedValidationEnvelope"} in validation_one_of


def test_orchestration_openapi_execution_plan_schema_declares_required_fields() -> None:
    schemas = _schemas(_load_contract())
    execution_plan = cast(dict[str, object], schemas["OrchestrationExecutionPlan"])
    step = cast(dict[str, object], schemas["OrchestrationPlanStep"])

    assert set(cast(list[object], execution_plan["required"])) == {
        "plan_id",
        "plan_version",
        "plan_status",
        "planning_mode",
        "execution_ready",
        "steps",
    }
    assert set(cast(list[object], step["required"])) == {
        "step_id",
        "route_id",
        "target_service",
        "target_operation",
        "step_status",
        "depends_on",
    }


def test_orchestration_openapi_prompt_decision_schema_supports_plan_only_and_clarification() -> (
    None
):
    schemas = _schemas(_load_contract())
    prompt_decision = cast(dict[str, object], schemas["PromptDecisionResponse"])
    decision_properties = cast(dict[str, object], prompt_decision["properties"])

    assert decision_properties["status"] == {
        "type": "string",
        "enum": ["resolved", "clarification_required"],
    }
    assert decision_properties["gate_status"] == {
        "type": "string",
        "enum": ["allowed", "plan_only", "clarification_required"],
    }
    assert "plan" in decision_properties
    assert "clarification" in decision_properties


def test_orchestration_openapi_multi_step_execution_schemas_allow_nullable_selected_route() -> None:
    schemas = _schemas(_load_contract())
    prompt_execution = cast(dict[str, object], schemas["PromptExecutionResponse"])
    prompt_request = cast(dict[str, object], schemas["PromptExecutionRequest"])
    step_result = cast(dict[str, object], schemas["OrchestrationStepExecutionResult"])
    step_summary = cast(dict[str, object], schemas["OrchestrationStepExecutionSummary"])
    response_schema = cast(dict[str, object], schemas["UnifiedAnswerResponse"])
    execution_properties = cast(dict[str, object], prompt_execution["properties"])
    request_properties = cast(dict[str, object], prompt_request["properties"])
    assert "user_id" not in request_properties
    request_selected_route = cast(dict[str, object], request_properties["selected_route"])
    execution_selected_route = cast(dict[str, object], execution_properties["selected_route"])
    adapter_response = cast(dict[str, object], execution_properties["adapter_response"])
    step_results = cast(dict[str, object], execution_properties["step_results"])
    step_results_one_of = cast(list[object], step_results["oneOf"])

    assert cast(list[object], request_selected_route["oneOf"])[1] == {"type": "null"}
    assert cast(list[object], execution_selected_route["oneOf"])[1] == {"type": "null"}
    assert cast(list[object], adapter_response["oneOf"])[1] == {"type": "null"}
    assert cast(dict[str, object], cast(dict[str, object], step_results_one_of[0])["items"]) == {
        "$ref": "#/components/schemas/OrchestrationStepExecutionResult"
    }
    assert "step_status" in cast(dict[str, object], step_result["properties"])
    assert "status" in cast(dict[str, object], response_schema["properties"])
    assert set(cast(list[object], step_summary["required"])) == {
        "total_steps",
        "resolved_steps",
        "blocked_steps",
        "rejected_steps",
        "pending_steps",
        "accepted_steps",
    }


def test_orchestration_openapi_prompt_execution_schema_requires_unified_response_section() -> None:
    schemas = _schemas(_load_contract())
    prompt_execution = cast(dict[str, object], schemas["PromptExecutionResponse"])
    execution_properties = cast(dict[str, object], prompt_execution["properties"])
    required = set(cast(list[object], prompt_execution["required"]))
    errors = cast(dict[str, object], execution_properties["errors"])
    errors_one_of = cast(list[object], errors["oneOf"])

    assert "response" in required
    assert execution_properties["response"] == {
        "$ref": "#/components/schemas/UnifiedAnswerResponse"
    }
    assert cast(dict[str, object], cast(dict[str, object], errors_one_of[0])["items"]) == {
        "$ref": "#/components/schemas/ErrorEnvelope"
    }
    response_schema = cast(dict[str, object], schemas["UnifiedAnswerResponse"])
    response_properties = cast(dict[str, object], response_schema["properties"])
    answer_mode = cast(dict[str, object], response_properties["answer_mode"])
    integrity_signals = cast(dict[str, object], response_properties["integrity_signals"])
    assert answer_mode == {
        "type": "string",
        "enum": [
            "compute_execution",
            "grounded_knowledge",
            "compute_plus_grounding",
            "forms_execution",
            "reports_execution",
            "document_extraction",
            "unsupported",
        ],
    }
    assert integrity_signals == {"$ref": "#/components/schemas/ResponseIntegritySignals"}


def test_orchestration_openapi_unified_response_requires_integrity_signal_defaults() -> None:
    schemas = _schemas(_load_contract())
    response_schema = cast(dict[str, object], schemas["UnifiedAnswerResponse"])
    integrity_schema = cast(dict[str, object], schemas["ResponseIntegritySignals"])
    response_required = set(cast(list[object], response_schema["required"]))
    integrity_required = set(cast(list[object], integrity_schema["required"]))
    integrity_properties = cast(dict[str, object], integrity_schema["properties"])

    assert "integrity_signals" in response_required
    assert integrity_required == {
        "verification_is_verified",
        "verification_confidence",
        "unsupported_claims",
        "contradictions_found",
        "grounding_contradictions",
        "unverified_or_contradicting_user_facts",
        "synthesis_tool_iterations_used",
        "confidence_flag",
    }
    assert integrity_properties["verification_is_verified"] == {
        "type": "boolean",
        "default": True,
    }
    assert integrity_properties["verification_confidence"] == {
        "type": "number",
        "default": 1.0,
    }
    assert integrity_properties["unsupported_claims"] == {
        "type": "array",
        "items": {"type": "string"},
        "default": [],
    }
    assert integrity_properties["contradictions_found"] == {
        "type": "array",
        "items": {"type": "string"},
        "default": [],
    }
    assert integrity_properties["synthesis_tool_iterations_used"] == {
        "type": "integer",
        "default": 0,
    }
    assert integrity_properties["confidence_flag"] == {
        "type": "string",
        "enum": ["high", "medium", "low"],
        "default": "high",
    }


def test_orchestration_openapi_grounded_schemas_capture_explanation_fields() -> None:
    schemas = _schemas(_load_contract())
    grounded_evidence = cast(dict[str, object], schemas["GroundedKnowledgeEvidence"])
    grounded_evidence_properties = cast(dict[str, object], grounded_evidence["properties"])
    assert "title" in grounded_evidence_properties
    assert "url" in grounded_evidence_properties
    assert "publication_state" in grounded_evidence_properties
    assert "source_version_form" in grounded_evidence_properties

    explanation_item = cast(dict[str, object], schemas["GroundedExplanationItem"])
    explanation_item_properties = cast(dict[str, object], explanation_item["properties"])
    assert "explanation_text" in explanation_item_properties
    assert "source_version_id" in explanation_item_properties
    assert "anchor_id" in explanation_item_properties

    citation = cast(dict[str, object], schemas["GroundedExplanationCitation"])
    citation_properties = cast(dict[str, object], citation["properties"])
    assert "citation_index" in citation_properties
    assert "temporal_applicability" in citation_properties

    temporal_applicability = cast(
        dict[str, object],
        schemas["GroundedTemporalApplicability"],
    )
    temporal_properties = cast(dict[str, object], temporal_applicability["properties"])
    assert temporal_properties["scope"] == {
        "type": "string",
        "enum": [
            "current-effective",
            "historical-effective",
            "tax-year-scoped",
            "timeline-multi-period",
        ],
    }


def _load_contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = document.get("paths")
    assert isinstance(raw, dict)
    return cast(dict[str, dict[str, object]], raw)


def _schemas(document: dict[str, object]) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    raw = components.get("schemas")
    assert isinstance(raw, dict)
    return cast(dict[str, object], raw)


def _runtime_route_methods() -> dict[str, set[str]]:
    app = create_app()
    route_methods: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not isinstance(methods, set):
            continue
        normalized = {str(method).lower() for method in cast(set[object], methods)}
        route_methods.setdefault(path, set()).update(normalized)
    return route_methods
