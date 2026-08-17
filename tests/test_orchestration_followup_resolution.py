"""Negative-path checks for orchestration follow-up resolution."""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.conversation_state_store import ConversationStateRecord
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionError
from services.orchestration.app.followup_resolution import build_followup_resolution
from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers


def test_followup_with_no_prior_context_is_rejected_as_off_topic_prompt() -> None:
    app = create_app(conversation_state_store=InMemoryConversationStateStore())
    client = TestClient(app, headers=orchestration_auth_headers(user_reference="followup-none"))

    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-followup-none-decide-001"},
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-followup-none-001",
            "channel": "chat",
            "prompt": {
                "text": "what about it?",
                "format": "plain_text",
            },
        },
    )

    assert decide.status_code == 400
    detail = cast(dict[str, Any], decide.json()["detail"])
    assert detail["error_code"] == "off_topic_prompt"
    assert detail["reason_code"] == "off_topic_prompt"


def test_system_followup_failure_surfaces_as_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestration.app import main as orchestration_main

    def _raise_system_error(*args: object, **kwargs: object) -> None:
        raise ConversationTurnResolutionError(
            error_code="conversation_turn_resolution_failed",
            reason_code="invalid_transition_response_schema",
            message="The request could not be processed because of an internal transition adjudication error.",
        )

    monkeypatch.setattr(orchestration_main, "build_followup_resolution", _raise_system_error)

    app = create_app(conversation_state_store=InMemoryConversationStateStore())
    client = TestClient(app, headers=orchestration_auth_headers(user_reference="followup-system"))

    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-followup-system-decide-001"},
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-followup-system-001",
            "channel": "chat",
            "prompt": {
                "text": "what is vat?",
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 500
    detail = cast(dict[str, Any], response.json()["detail"])
    assert detail["error_code"] == "conversation_turn_resolution_failed"
    assert detail["reason_code"] == "invalid_transition_response_schema"


def test_followup_execute_with_no_prior_context_fails_closed_canonically() -> None:
    app = create_app(conversation_state_store=InMemoryConversationStateStore())
    client = TestClient(
        app,
        headers=orchestration_auth_headers(user_reference="followup-none-exec"),
    )

    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-followup-none-exec-001",
        "channel": "chat",
        "prompt": {
            "text": "what about 2024?",
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-followup-none-exec-decide-001"},
        json=decide_payload,
    )

    assert decide.status_code == 400
    detail = cast(dict[str, Any], decide.json()["detail"])
    assert detail["error_code"] == "off_topic_prompt"
    assert detail["reason_code"] == "off_topic_prompt"


def test_conflicting_conversation_users_yield_exact_binding() -> None:
    records = [
        _record(
            "exec-followup-conflict-a-001",
            record_id="r-a",
            prompt_text="What is VAT?",
            answer_summary="VAT answer",
            tax_domain_hint="vat",
        ),
        _record(
            "exec-followup-conflict-b-001",
            record_id="r-b",
            prompt_text="What is PAYE?",
            answer_summary="PAYE answer",
            tax_domain_hint="income_tax",
        ),
    ]
    result = build_followup_resolution(
        turn_resolution=_resolution(
            refs=["r-a:assistant"],
            reuse_service_result=True,
            reuse_facts=True,
            reuse_evidence=True,
            reuse_artifact=True,
        ),
        recent_conversation_state=records,
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "vat"},
    )
    assert result is not None
    assert result["referenced_execution_ids"] == ["exec-followup-conflict-a-001"]
    assert result["primary_referenced_execution_id"] == "exec-followup-conflict-a-001"
    assert cast(dict[str, Any] | None, result.get("reused_service_result_payload")) == {
        "result": "exec-followup-conflict-a-001"
    }


def test_cross_domain_followup_conflict_binds_the_requested_record() -> None:
    records = [
        _record(
            "exec-followup-cross-domain-001",
            record_id="r-a",
            prompt_text="What is health contribution?",
            answer_summary="Health contribution answer",
            tax_domain_hint="health_contribution",
        ),
        _record(
            "exec-followup-cross-domain-002",
            record_id="r-b",
            prompt_text="What is income tax?",
            answer_summary="Income tax answer",
            tax_domain_hint="income_tax",
        ),
    ]
    result = build_followup_resolution(
        turn_resolution=_resolution(
            refs=["r-a:user"],
            reuse_service_result=True,
            reuse_facts=True,
        ),
        recent_conversation_state=records,
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "health_contribution"},
    )
    assert result is not None
    assert result["referenced_execution_ids"] == ["exec-followup-cross-domain-001"]
    assert result["primary_referenced_execution_id"] == "exec-followup-cross-domain-001"


def test_cross_user_followup_execute_preserves_reference_order() -> None:
    records = [
        _record(
            "exec-followup-cross-user-a-001",
            record_id="r-a",
            prompt_text="What is VAT?",
            answer_summary="VAT answer",
            tax_domain_hint="vat",
        ),
        _record(
            "exec-followup-cross-user-b-001",
            record_id="r-b",
            prompt_text="What is PAYE?",
            answer_summary="PAYE answer",
            tax_domain_hint="income_tax",
        ),
    ]
    result = build_followup_resolution(
        turn_resolution=_resolution(
            refs=["r-a:user", "r-b:assistant"],
            reuse_service_result=True,
        ),
        recent_conversation_state=records,
        current_semantic_frame={"intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "vat"},
    )
    assert result is not None
    assert result["referenced_execution_ids"] == [
        "exec-followup-cross-user-a-001",
        "exec-followup-cross-user-b-001",
    ]
    assert result["primary_referenced_execution_id"] is None


def _record(
    execution_id: str,
    *,
    record_id: str,
    prompt_text: str,
    answer_summary: str,
    tax_domain_hint: str,
) -> ConversationStateRecord:
    return {
        "execution_id": execution_id,
        "tenant_id": "tenant",
        "conversation_id": "conversation",
        "user_id": "user",
        "context_payload": {
            "record_id": record_id,
            "raw_prompt_text": prompt_text,
            "assistant_answer_summary": answer_summary,
            "intent_class": "lookup_grounded_knowledge",
            "tax_domain_hint": tax_domain_hint,
            "turn_outcome_kind": "execution_success",
            "assistant_turn_kind": "answer",
            "selected_route": {
                "route_id": "knowledge_search_route_v1",
                "target_service": "knowledge",
                "target_operation": "search_knowledge",
            },
            "adapter_result_payload": {"result": execution_id},
            "mapped_result_summary": {"action_status": "accepted"},
            "grounded_evidence_summary": [{"source_id": execution_id, "excerpt": answer_summary}],
            "service_artifact_summary": None,
            "stated_facts": {"vat_subject": tax_domain_hint == "vat"},
            "failure_summary": None,
            "reason_code": None,
        },
    }


def _resolution(
    *,
    refs: list[str],
    reuse_service_result: bool = False,
    reuse_facts: bool = False,
    reuse_evidence: bool = False,
    reuse_artifact: bool = False,
    ) -> ConversationTurnResolution:
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
            "audit_summary": "followup resolution test",
        }
    )
