from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.orchestration.app.conversation_turn_resolution import ConversationTurnCandidate
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionError
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionInput
from services.orchestration.app.conversation_turn_resolution import OpenAIConversationTurnResolver


class _FakeCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(outcomes))


def _payload(**changes: object) -> ConversationTurnResolutionInput:
    data: dict[str, object] = {
        "today": "2026-07-28",
        "trusted_jurisdiction": "Kenya",
        "tenant_product_context": {},
        "current_prompt": "What is VAT?",
        "recent_candidates": [],
        "supported_intents": ["lookup_grounded_knowledge"],
        "supported_knowledge_domains": ["vat", "general_tax"],
        "supported_computations": [],
        "supported_artifact_operations": [],
        "external_action_considered": False,
        "immediately_preceding_clarification": None,
        "prior_failure_metadata": None,
    }
    data.update(changes)
    return ConversationTurnResolutionInput.model_validate(data)


def _resolution_json(**changes: object) -> str:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "relationship": "standalone",
        "operation_mode": "informational",
        "raw_prompt": "What is VAT?",
        "contextualized_prompt": "What is VAT in Kenya?",
        "intent_class": "lookup_grounded_knowledge",
        "tax_domain_hint": "vat",
        "retrieval_tax_domain_filter": "vat",
        "jurisdiction_hint": "Kenya",
        "tax_year_hint": None,
        "answerability": "answerable",
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
        "assumptions": [],
        "confidence": 0.99,
        "audit_summary": "standalone lookup",
    }
    data.update(changes)
    return ConversationTurnResolution.model_validate(data).model_dump_json()


def test_openai_turn_resolver_retries_once_on_transport_failure() -> None:
    client = _FakeClient([
        RuntimeError("transient"),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=_resolution_json()))]),
    ])
    resolver = OpenAIConversationTurnResolver(client=client, model="gpt-test")
    result = resolver.resolve_turn(_payload())
    assert result.tax_domain_hint == "vat"
    assert client.chat.completions.calls == 2


def test_openai_turn_resolver_stops_after_two_transport_failures() -> None:
    client = _FakeClient([RuntimeError("first"), RuntimeError("second")])
    resolver = OpenAIConversationTurnResolver(client=client, model="gpt-test")
    with pytest.raises(ConversationTurnResolutionError, match="temporarily unavailable"):
        resolver.resolve_turn(_payload())
    assert client.chat.completions.calls == 2


def test_openai_turn_resolver_rejects_empty_response_without_retry() -> None:
    client = _FakeClient([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="   "))])
    ])
    resolver = OpenAIConversationTurnResolver(client=client, model="gpt-test")
    with pytest.raises(ConversationTurnResolutionError, match="empty response"):
        resolver.resolve_turn(_payload())
    assert client.chat.completions.calls == 1


def test_openai_turn_resolver_rejects_malformed_json_without_retry() -> None:
    client = _FakeClient([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{not-json"))])
    ])
    resolver = OpenAIConversationTurnResolver(client=client, model="gpt-test")
    with pytest.raises(ConversationTurnResolutionError, match="malformed structured data"):
        resolver.resolve_turn(_payload())
    assert client.chat.completions.calls == 1


def test_openai_turn_resolver_rejects_invariant_violations_without_retry() -> None:
    bad_json = _resolution_json(
        relationship="standalone",
        referenced_candidate_ids=["invented:user"],
    )
    client = _FakeClient([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=bad_json))])
    ])
    candidate = ConversationTurnCandidate(
        candidate_id="known:user",
        execution_id="exec-1",
        role="user",
        prompt_text="What is VAT?",
        answer_summary=None,
        intent_class="lookup_grounded_knowledge",
        tax_domain_hint="vat",
        tax_year=None,
        selected_route=None,
        turn_outcome_kind=None,
        clarification_requested_fields=[],
        created_at=None,
    )
    resolver = OpenAIConversationTurnResolver(client=client, model="gpt-test")
    with pytest.raises(ConversationTurnResolutionError, match="validation"):
        resolver.resolve_turn(_payload(recent_candidates=[candidate]))
    assert client.chat.completions.calls == 1
