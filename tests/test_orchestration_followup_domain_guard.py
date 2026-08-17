"""Regression coverage for deterministic binding after semantic resolution."""
from __future__ import annotations

from services.orchestration.app.action_adapter_registry import filter_grounded_evidence_for_scope
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.followup_resolution import build_bounded_candidates, build_followup_resolution


def _resolution(*, candidate_id: str) -> ConversationTurnResolution:
    return ConversationTurnResolution.model_validate({"schema_version":"1.0","relationship":"continuation","operation_mode":"informational","raw_prompt":"Which acts govern it?","contextualized_prompt":"Which laws govern VAT in Kenya?","intent_class":"lookup_grounded_knowledge","tax_domain_hint":"vat","retrieval_tax_domain_filter":"vat","jurisdiction_hint":"Kenya","tax_year_hint":None,"answerability":"answerable","clarification_reason_code":None,"clarification_question":None,"required_context_fields":[],"provided_context_fields":[],"missing_required_context_fields":[],"needs_knowledge_retrieval":True,"needs_computation":False,"needs_external_action":False,"needs_artifact_operation":False,"referenced_candidate_ids":[candidate_id],"resolved_references":[],"retained_fields":["vat_subject"],"corrected_fields":[],"reuse_prior_semantic_facts":True,"reuse_prior_computation_result":False,"reuse_prior_evidence":True,"reuse_prior_artifact":False,"assumptions":[],"confidence":0.9,"audit_summary":"VAT continuation"})


def test_role_separated_candidates_bind_exact_referenced_execution() -> None:
    records = [
        {"execution_id":"old", "tenant_id":"t", "conversation_id":"c", "user_id":"u", "context_payload":{"prompt_text":"What is VAT?", "answer_summary":"VAT answer", "intent_class":"lookup_grounded_knowledge", "tax_domain_hint":"vat"}},
        {"execution_id":"new", "tenant_id":"t", "conversation_id":"c", "user_id":"u", "context_payload":{"prompt_text":"What is PAYE?", "answer_summary":"PAYE answer", "intent_class":"lookup_grounded_knowledge", "tax_domain_hint":"paye_generalized"}},
    ]
    candidates = build_bounded_candidates(records)
    assert [candidate.role for candidate in candidates] == ["user", "assistant", "user", "assistant"]
    result = build_followup_resolution(turn_resolution=_resolution(candidate_id="old:assistant"), recent_conversation_state=records)
    assert result is not None
    assert result["primary_referenced_execution_id"] == "old"
    assert result["effective_prompt_text"] == "Which laws govern VAT in Kenya?"


def test_scope_filter_keeps_the_relevant_vat_passage() -> None:
    evidence = [{"source_id":"web:kra.go.ke", "tax_domain":"vat", "content_excerpt":"Excise duty is charged on specified goods. VAT is charged on taxable supplies.", "title":"KRA Types of Taxes"}]
    assert len(filter_grounded_evidence_for_scope(evidence, tax_domain_hint="vat", resolved_entity="VAT")) == 1
