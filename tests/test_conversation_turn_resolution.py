from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution, ConversationTurnResolutionError, ConversationTurnResolutionInput, TurnAnswerability, TurnOperationMode, TurnRelationship, validate_conversation_turn_resolution
from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers


def _resolution(**changes: object) -> ConversationTurnResolution:
    data: dict[str, object] = {"schema_version":"1.0","relationship":"standalone","operation_mode":"informational","raw_prompt":"What is VAT?","contextualized_prompt":"What is VAT?","intent_class":"lookup_grounded_knowledge","tax_domain_hint":"vat","retrieval_tax_domain_filter":"vat","jurisdiction_hint":"Kenya","tax_year_hint":None,"answerability":"answerable","clarification_reason_code":None,"clarification_question":None,"required_context_fields":[],"provided_context_fields":[],"missing_required_context_fields":[],"needs_knowledge_retrieval":True,"needs_computation":False,"needs_external_action":False,"needs_artifact_operation":False,"referenced_candidate_ids":[],"resolved_references":[],"retained_fields":[],"corrected_fields":[],"reuse_prior_semantic_facts":False,"reuse_prior_computation_result":False,"reuse_prior_evidence":False,"reuse_prior_artifact":False,"assumptions":[],"confidence":0.9,"audit_summary":"standalone lookup"}
    data.update(changes); return ConversationTurnResolution.model_validate(data)


def _input() -> ConversationTurnResolutionInput:
    return ConversationTurnResolutionInput(today="2026-07-28", trusted_jurisdiction="Kenya", tenant_product_context={}, current_prompt="What is VAT?", recent_candidates=[], supported_intents=[], supported_knowledge_domains=[], supported_computations=[], supported_artifact_operations=[], external_action_considered=False, immediately_preceding_clarification=None, prior_failure_metadata=None)


def test_turn_resolution_schema_is_strict_and_required() -> None:
    schema = ConversationTurnResolution.model_json_schema(mode="serialization")
    _assert_object_schema_is_strict(schema)
    assert set(schema["required"]) == set(schema["properties"])
    assert not _has_default(schema)


def test_standalone_and_general_tax_contracts_validate() -> None:
    standalone = _resolution()
    validate_conversation_turn_resolution(resolution=standalone, input_payload=_input())
    fish = _resolution(raw_prompt="What about fish tax?", contextualized_prompt="What taxes, levies, licence fees or VAT treatment apply to fishing, aquaculture or fish sales in Kenya?", tax_domain_hint="general_tax", retrieval_tax_domain_filter=None, answerability="answerable_with_assumptions", assumptions=["Fish tax may refer to taxes, levies, licence fees or VAT treatment."], audit_summary="general tax lookup")
    validate_conversation_turn_resolution(resolution=fish, input_payload=_input())


def test_contradictory_clarification_metadata_is_rejected() -> None:
    with pytest.raises(ConversationTurnResolutionError, match="validation"):
        validate_conversation_turn_resolution(resolution=_resolution(clarification_question="Need income", required_context_fields=["income"]), input_payload=_input())


def test_clarification_requires_complete_metadata() -> None:
    with pytest.raises(ConversationTurnResolutionError):
        validate_conversation_turn_resolution(resolution=_resolution(answerability=TurnAnswerability.clarification_required), input_payload=_input())


def test_clarification_answer_requires_supplied_requested_field() -> None:
    clarification_input = ConversationTurnResolutionInput(
        today="2026-07-28",
        trusted_jurisdiction="Kenya",
        tenant_product_context={},
        current_prompt="KES 480,000.",
        recent_candidates=[],
        supported_intents=[],
        supported_knowledge_domains=[],
        supported_computations=[],
        supported_artifact_operations=[],
        external_action_considered=False,
        immediately_preceding_clarification={"required_context_fields": ["income"], "clarification_question": "Please provide your income."},
        prior_failure_metadata=None,
    )
    validate_conversation_turn_resolution(
        resolution=_resolution(
            relationship=TurnRelationship.clarification_answer,
            answerability=TurnAnswerability.answerable,
            provided_context_fields=["income"],
        ),
        input_payload=clarification_input,
    )


def test_clarification_answer_is_rejected_without_immediate_clarification() -> None:
    with pytest.raises(ConversationTurnResolutionError, match="validation"):
        validate_conversation_turn_resolution(
            resolution=_resolution(
                relationship=TurnRelationship.clarification_answer,
                answerability=TurnAnswerability.answerable,
                provided_context_fields=["income"],
            ),
            input_payload=_input(),
        )


def test_answerable_with_assumptions_requires_at_least_one_assumption() -> None:
    with pytest.raises(ConversationTurnResolutionError, match="deterministic validation"):
        validate_conversation_turn_resolution(
            resolution=_resolution(answerability=TurnAnswerability.answerable_with_assumptions, assumptions=[]),
            input_payload=_input(),
        )


class _CountingResolver:
    def __init__(self) -> None: self.calls = 0
    def resolve_turn(self, payload: ConversationTurnResolutionInput) -> ConversationTurnResolution:
        self.calls += 1
        has_history = bool(payload.recent_candidates)
        return _resolution(raw_prompt=payload.current_prompt, contextualized_prompt="Which laws govern VAT in Kenya?", relationship="continuation" if has_history else "standalone", referenced_candidate_ids=[payload.recent_candidates[-1].candidate_id] if has_history else [], reuse_prior_semantic_facts=has_history, retrieval_tax_domain_filter="vat")


def test_decide_uses_injected_resolver_once_for_new_conversation() -> None:
    resolver = _CountingResolver(); client = TestClient(create_app(turn_resolver=resolver))
    response = client.post("/v1/orchestration/prompt/decide", headers=orchestration_auth_headers(), json={"tenant_id":"pilot_tenant_alpha", "conversation_id":"turn-resolution-new", "channel":"chat", "prompt":{"text":"What is VAT?", "format":"plain_text"}})
    assert response.status_code == 200, response.text
    assert resolver.calls == 1
    assert response.json()["turn_resolution"]["contextualized_prompt"] == "Which laws govern VAT in Kenya?"


def test_cached_execute_does_not_resolve_a_second_turn() -> None:
    resolver = _CountingResolver(); client = TestClient(create_app(turn_resolver=resolver))
    body = {"tenant_id":"pilot_tenant_alpha", "conversation_id":"turn-resolution-execute", "channel":"chat", "prompt":{"text":"What is VAT?", "format":"plain_text"}}
    headers = orchestration_auth_headers()
    decision = client.post("/v1/orchestration/prompt/decide", headers=headers, json=body)
    assert decision.status_code == 200, decision.text
    chosen = decision.json()
    execution = client.post("/v1/orchestration/prompt/execute", headers=headers, json={**body, "idempotency_key":"turn-resolution-execute-001", "intent_class":chosen["intent_class"], "tax_domain_hint":chosen["tax_domain_hint"], "decision_id":chosen["decision_id"], "selected_route":chosen["selected_route"]})
    assert execution.status_code in {200, 400, 404, 500}, execution.text
    assert resolver.calls == 1


def _has_default(value: object) -> bool:
    if isinstance(value, dict): return "default" in value or any(_has_default(item) for item in value.values())
    if isinstance(value, list): return any(_has_default(item) for item in value)
    return False


def _assert_object_schema_is_strict(value: object) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            assert value["additionalProperties"] is False
        for item in value.values():
            _assert_object_schema_is_strict(item)
    elif isinstance(value, list):
        for item in value:
            _assert_object_schema_is_strict(item)
