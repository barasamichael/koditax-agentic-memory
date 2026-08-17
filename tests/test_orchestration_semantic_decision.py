from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionError
from services.orchestration.app.followup_resolution import build_bounded_candidates
from services.orchestration.app.followup_resolution import build_followup_resolution
from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers
from tests.orchestration_auth_support import orchestration_test_user_id


class _Resolver:
    def __init__(self, resolution: ConversationTurnResolution) -> None:
        self.calls = 0
        self._resolution = resolution

    def resolve_turn(self, payload: object) -> ConversationTurnResolution:
        self.calls += 1
        return self._resolution


def _seed_state_record(
    store: InMemoryConversationStateStore,
    *,
    execution_id: str,
    prompt_text: str,
    answer_summary: str,
    tax_domain_hint: str,
    conversation_id: str,
    user_reference: str,
    assistant_turn_kind: str = "answer",
    turn_outcome_kind: str = "execution_success",
) -> None:
    store.put(
        {
            "execution_id": execution_id,
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": conversation_id,
            "user_id": orchestration_test_user_id(user_reference),
            "context_payload": {
                "record_id": execution_id,
                "raw_prompt_text": prompt_text,
                "assistant_answer_summary": answer_summary,
                "intent_class": "lookup_grounded_knowledge",
                "tax_domain_hint": tax_domain_hint,
                "assistant_turn_kind": assistant_turn_kind,
                "turn_outcome_kind": turn_outcome_kind,
                "selected_route": {
                    "route_id": "knowledge_search_route_v1",
                    "target_service": "knowledge",
                    "target_operation": "search_knowledge",
                },
                "adapter_result_payload": {"result": execution_id},
                "mapped_result_summary": {"action_status": "accepted"},
                "grounded_evidence_summary": [{"source_id": execution_id, "excerpt": answer_summary}],
                "service_artifact_summary": None,
                "stated_facts": {},
                "failure_summary": None,
                "reason_code": None,
            },
        }
    )


def _resolution(**changes: object) -> ConversationTurnResolution:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "relationship": "standalone",
        "operation_mode": "informational",
        "raw_prompt": "What about fish tax?",
        "contextualized_prompt": "What taxes, levies, licence fees or VAT treatment apply to fish tax in Kenya?",
        "intent_class": "lookup_grounded_knowledge",
        "tax_domain_hint": "general_tax",
        "retrieval_tax_domain_filter": None,
        "jurisdiction_hint": "Kenya",
        "tax_year_hint": None,
        "answerability": "answerable_with_assumptions",
        "clarification_reason_code": None,
        "clarification_question": None,
        "required_context_fields": [],
        "provided_context_fields": [],
        "missing_required_context_fields": [],
        "needs_knowledge_retrieval": True,
        "needs_computation": False,
        "needs_external_action": False,
        "needs_artifact_operation": False,
        "referenced_candidate_ids": [],
        "resolved_references": [],
        "retained_fields": [],
        "corrected_fields": [],
        "reuse_prior_semantic_facts": False,
        "reuse_prior_computation_result": False,
        "reuse_prior_evidence": False,
        "reuse_prior_artifact": False,
        "assumptions": ["The phrase may refer to tax, levy, licence fee or VAT treatment."],
        "confidence": 0.99,
        "audit_summary": "general tax lookup",
    }
    data.update(changes)
    return ConversationTurnResolution.model_validate(data)


def test_prompt_decide_does_not_call_legacy_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.orchestration.app import main as orchestration_main

    def _raise(*args: object, **kwargs: object) -> object:
        raise AssertionError("legacy parser was called")

    monkeypatch.setattr(orchestration_main, "parse_income_tax_prompt_intent_envelope", _raise)
    resolver = _Resolver(_resolution())
    client = TestClient(
        create_app(
            conversation_state_store=InMemoryConversationStateStore(),
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-decision"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-decision-001",
            "channel": "chat",
            "prompt": {"text": "What about fish tax?", "format": "plain_text"},
        },
    )
    assert response.status_code == 200, response.text
    assert resolver.calls == 1


def test_prompt_decide_general_tax_routes_broad_lookup() -> None:
    resolver = _Resolver(_resolution())
    client = TestClient(
        create_app(
            conversation_state_store=InMemoryConversationStateStore(),
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-general-tax"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-general-tax-001",
            "channel": "chat",
            "prompt": {"text": "What about fish tax?", "format": "plain_text"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tax_domain_hint"] == "general_tax"
    assert body["selected_route"]["route_id"] == "knowledge_search_route_v1"
    assert body["turn_resolution"]["contextualized_prompt"] == "What taxes, levies, licence fees or VAT treatment apply to fish tax in Kenya?"


def test_prompt_decide_paye_plus_freelance_is_standalone_lookup() -> None:
    resolution = _resolution(
        raw_prompt="I work permanently and PAYE is deducted, but I also earn freelance income through M-Pesa. Do I declare it separately?",
        contextualized_prompt="I work permanently and PAYE is deducted, but I also earn freelance income through M-Pesa. Do I declare it separately?",
        intent_class="lookup_grounded_knowledge",
        tax_domain_hint="income_tax",
        retrieval_tax_domain_filter="income_tax",
        audit_summary="PAYE plus freelance",
    )
    resolver = _Resolver(resolution)
    client = TestClient(
        create_app(
            conversation_state_store=InMemoryConversationStateStore(),
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-paye-freelance"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-paye-freelance-001",
            "channel": "chat",
            "prompt": {
                "text": "I work permanently and PAYE is deducted, but I also earn freelance income through M-Pesa. Do I declare it separately?",
                "format": "plain_text",
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_route"]["route_id"] == "knowledge_search_route_v1"
    assert body["turn_resolution"]["contextualized_prompt"] == resolution.contextualized_prompt


def test_prompt_decide_vat_followup_uses_the_prior_vat_candidate() -> None:
    store = InMemoryConversationStateStore()
    _seed_state_record(
        store,
        execution_id="vat-exec-1",
        prompt_text="What is VAT?",
        answer_summary="VAT answer",
        tax_domain_hint="vat",
        conversation_id="semantic-seeded-001",
        user_reference="semantic-vat-followup",
    )
    recent_state = store.list_recent(
        tenant_id="pilot_tenant_alpha",
        conversation_id="semantic-seeded-001",
        user_id=orchestration_test_user_id("semantic-vat-followup"),
        limit=50,
    )
    assistant_candidate_id = next(
        candidate.candidate_id
        for candidate in build_bounded_candidates(recent_state)
        if candidate.role == "assistant"
    )
    assert assistant_candidate_id == "vat-exec-1:assistant"
    resolution = _resolution(
        relationship="continuation",
        raw_prompt="Which acts govern it?",
        contextualized_prompt="Which laws govern VAT in Kenya?",
        tax_domain_hint="vat",
        retrieval_tax_domain_filter="vat",
        answerability="answerable_with_assumptions",
        referenced_candidate_ids=[assistant_candidate_id],
        reuse_prior_semantic_facts=True,
        assumptions=["The user is asking about VAT based on the prior VAT conversation."],
        audit_summary="VAT follow-up",
    )
    result = build_followup_resolution(
        turn_resolution=resolution,
        recent_conversation_state=recent_state,
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "vat"},
    )
    assert result is not None
    assert result["primary_referenced_execution_id"] == "vat-exec-1"
    assert result["referenced_execution_ids"] == ["vat-exec-1"]
    assert result["effective_prompt_text"] == "Which laws govern VAT in Kenya?"


def test_prompt_decide_married_filing_keeps_the_prompt_natural() -> None:
    store = InMemoryConversationStateStore()
    _seed_state_record(
        store,
        execution_id="vat-exec-2",
        prompt_text="What is VAT?",
        answer_summary="VAT answer",
        tax_domain_hint="vat",
        conversation_id="semantic-married-filing-001",
        user_reference="semantic-married-filing",
    )
    resolution = _resolution(
        raw_prompt="My husband and I got married earlier this year. Should we file jointly or separately?",
        contextualized_prompt="My husband and I got married earlier this year. Should we file jointly or separately?",
        relationship="topic_shift",
        intent_class="lookup_grounded_knowledge",
        tax_domain_hint="income_tax",
        retrieval_tax_domain_filter="income_tax",
        answerability="answerable_with_assumptions",
        assumptions=["Filing status depends on the applicable Kenyan rules and the facts of the couple's situation."],
        audit_summary="married filing",
    )
    resolver = _Resolver(resolution)
    client = TestClient(
        create_app(
            conversation_state_store=store,
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-married-filing"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-married-filing-001",
            "channel": "chat",
            "prompt": {
                "text": "My husband and I got married earlier this year. Should we file jointly or separately?",
                "format": "plain_text",
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["turn_resolution"]["contextualized_prompt"] == resolution.contextualized_prompt
    assert "VAT" not in body["turn_resolution"]["contextualized_prompt"]


def test_prompt_decide_missing_income_requires_clarification() -> None:
    resolution = _resolution(
        raw_prompt="Calculate my tax for 2026.",
        contextualized_prompt="Calculate my tax for 2026.",
        operation_mode="computation",
        intent_class="compute_income_tax",
        tax_domain_hint="income_tax",
        retrieval_tax_domain_filter=None,
        answerability="clarification_required",
        clarification_reason_code="missing_income",
        clarification_question="Please provide taxable income.",
        required_context_fields=["taxable_income_or_income_components"],
        provided_context_fields=[],
        missing_required_context_fields=["taxable_income_or_income_components"],
        needs_knowledge_retrieval=False,
        needs_computation=True,
        audit_summary="missing income",
    )
    resolver = _Resolver(resolution)
    client = TestClient(
        create_app(
            conversation_state_store=InMemoryConversationStateStore(),
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-missing-income"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-missing-income-001",
            "channel": "chat",
            "prompt": {"text": "Calculate my tax for 2026.", "format": "plain_text"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "clarification_required"
    assert body["clarification"]["reason_code"] == "missing_income"
    assert body["clarification"]["required_context_fields"] == ["taxable_income_or_income_components"]


def test_prompt_decide_meta_conversation_routes_to_meta_backend() -> None:
    meta_resolution = _resolution(
        relationship="meta_conversation",
        operation_mode="meta",
        intent_class="meta_conversation",
        tax_domain_hint="general_tax",
        retrieval_tax_domain_filter=None,
        answerability="answerable",
        needs_knowledge_retrieval=False,
        assumptions=[],
        contextualized_prompt="I was testing whether you were giving me junk.",
        raw_prompt="I was testing whether you were giving me junk.",
        audit_summary="meta",
    )
    resolver = _Resolver(meta_resolution)
    client = TestClient(
        create_app(
            conversation_state_store=InMemoryConversationStateStore(),
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-meta"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-meta-001",
            "channel": "chat",
            "prompt": {"text": "I was testing whether you were giving me junk.", "format": "plain_text"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_route"]["route_id"] == "meta_conversation_route_v1"
    assert body["intent_class"] == "meta_conversation"


def test_prompt_decide_previous_failure_question_routes_to_meta_backend() -> None:
    meta_resolution = _resolution(
        relationship="meta_conversation",
        operation_mode="meta",
        intent_class="meta_conversation",
        tax_domain_hint="general_tax",
        retrieval_tax_domain_filter=None,
        answerability="answerable",
        needs_knowledge_retrieval=False,
        assumptions=[],
        contextualized_prompt="Why did that fail?",
        raw_prompt="Why did that fail?",
        audit_summary="meta failure explanation",
    )
    resolver = _Resolver(meta_resolution)
    client = TestClient(
        create_app(
            conversation_state_store=InMemoryConversationStateStore(),
            turn_resolver=resolver,
        ),
        headers=orchestration_auth_headers(user_reference="semantic-meta-failure"),
    )
    response = client.post(
        "/v1/orchestration/prompt/decide",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "semantic-meta-failure-001",
            "channel": "chat",
            "prompt": {"text": "Why did that fail?", "format": "plain_text"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_route"]["route_id"] == "meta_conversation_route_v1"


def test_create_app_raises_when_turn_resolver_configuration_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.orchestration.app import main as orchestration_main

    class _MissingConfig:
        configured = False

    monkeypatch.setattr(
        orchestration_main,
        "load_orchestration_openai_response_synthesis_config",
        lambda: _MissingConfig(),
    )
    with pytest.raises(ConversationTurnResolutionError, match="Conversation turn resolver configuration is required"):
        orchestration_main.create_app()
