from __future__ import annotations

from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.followup_resolution import build_bounded_candidates
from services.orchestration.app.followup_resolution import build_followup_resolution


def _record(execution_id: str, *, record_id: str | None = None, prompt_text: str, answer_summary: str, outcome: str = "execution_success") -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "tenant_id": "tenant",
        "conversation_id": "conversation",
        "user_id": "user",
        "context_payload": {
            "record_id": record_id or execution_id,
            "raw_prompt_text": prompt_text,
            "assistant_answer_summary": answer_summary,
            "intent_class": "lookup_grounded_knowledge",
            "tax_domain_hint": "vat",
            "turn_outcome_kind": outcome,
            "assistant_turn_kind": "answer",
            "selected_route": {"route_id": "knowledge_search_route_v1", "target_service": "knowledge", "target_operation": "search_knowledge"},
            "adapter_result_payload": {"result": execution_id},
            "mapped_result_summary": {"action_status": "accepted"},
            "grounded_evidence_summary": [{"source_id": execution_id, "excerpt": answer_summary}],
            "service_artifact_summary": {"artifact_id": f"{execution_id}-artifact"},
            "stated_facts": {"vat_subject": True},
            "failure_summary": None,
            "reason_code": None,
        },
    }


def _resolution(*, refs: list[str], reuse_service_result: bool = False, reuse_facts: bool = False, reuse_evidence: bool = False, reuse_artifact: bool = False) -> ConversationTurnResolution:
    return ConversationTurnResolution.model_validate(
        {
            "schema_version": "1.0",
            "relationship": "continuation",
            "operation_mode": "informational",
            "raw_prompt": "Which acts govern it?",
            "contextualized_prompt": "Which laws govern VAT in Kenya?",
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
            "referenced_candidate_ids": refs,
            "resolved_references": [],
            "retained_fields": ["vat_subject"],
            "corrected_fields": [],
            "reuse_prior_semantic_facts": reuse_facts,
            "reuse_prior_computation_result": reuse_service_result,
            "reuse_prior_evidence": reuse_evidence,
            "reuse_prior_artifact": reuse_artifact,
            "assumptions": [],
            "confidence": 0.9,
            "audit_summary": "VAT continuation",
        }
    )


def test_followup_binding_uses_the_exact_referenced_record() -> None:
    records = [
        _record("exec-1", record_id="r1", prompt_text="What is VAT?", answer_summary="VAT answer"),
        _record("exec-2", record_id="r2", prompt_text="What is PAYE?", answer_summary="PAYE answer"),
        _record("exec-3", record_id="r3", prompt_text="What is corporation tax?", answer_summary="Corporation tax answer"),
    ]
    result = build_followup_resolution(
        turn_resolution=_resolution(refs=["r2:assistant"], reuse_service_result=True, reuse_facts=True, reuse_evidence=True, reuse_artifact=True),
        recent_conversation_state=records,
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "vat"},
    )
    assert result is not None
    assert result["referenced_execution_ids"] == ["exec-2"]
    assert result["primary_referenced_execution_id"] == "exec-2"
    assert result["reused_service_result_payload"] == {"result": "exec-2"}
    assert result["reused_semantic_facts_payload"] == {"vat_subject": True}
    assert result["reused_artifact_payload"] == {"artifact_id": "exec-2-artifact"}


def test_followup_binding_preserves_reference_order_across_multiple_records() -> None:
    records = [
        _record("exec-1", record_id="r1", prompt_text="What is VAT?", answer_summary="VAT answer"),
        _record("exec-2", record_id="r2", prompt_text="What is PAYE?", answer_summary="PAYE answer"),
        _record("exec-3", record_id="r3", prompt_text="What is corporation tax?", answer_summary="Corporation tax answer"),
    ]
    result = build_followup_resolution(
        turn_resolution=_resolution(refs=["r3:user", "r2:assistant"], reuse_service_result=True),
        recent_conversation_state=records,
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "vat"},
    )
    assert result is not None
    assert result["referenced_execution_ids"] == ["exec-3", "exec-2"]
    assert result["prior_execution_id"] == "exec-3"
    assert result["primary_referenced_execution_id"] is None


def test_followup_binding_returns_none_without_reuse_flags_or_references() -> None:
    records = [_record("exec-1", record_id="r1", prompt_text="What is VAT?", answer_summary="VAT answer")]
    assert build_followup_resolution(
        turn_resolution=_resolution(refs=[]),
        recent_conversation_state=records,
    ) is None
