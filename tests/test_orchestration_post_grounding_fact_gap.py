"""Focused coverage for post-grounding taxpayer-fact clarification."""

from __future__ import annotations

from datetime import date
from collections.abc import Iterator
from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers
from tests.orchestration_auth_support import orchestration_test_user_id
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.orchestration.app.llm_response_generator import LLMResponseStreamEvent
import services.orchestration.app.prompt_intent_envelope as prompt_intent_envelope
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.prompt_semantic_extractor import ExtractedSemanticContext
from services.orchestration.app.response_integrity_signals import ResponseIntegritySignals
from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)


class _VatKnowledgeRepository:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return (
            KnowledgeSearchRecord(
                source_id="KNW-VAT-REGISTRATION",
                title="VAT registration threshold",
                url="https://example.test/vat-registration",
                source_type="tax_law",
                tax_domain="vat",
                authority_level="statute",
                effective_from="2024-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="vat-registration-threshold",
                content="VAT registration applies when annual turnover exceeds KES 5 million.",
            ),
        )

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (source_ids, anchor_ids)
        return ()

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        _ = (query, source_type, tax_domain, start_date, end_date)
        return ()

    def list_source_versions(
        self,
        *,
        publication_state: str | None,
        source_id: str | None,
        source_family_id: str | None,
        tax_domain: str | None,
        source_class: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]:
        _ = (
            publication_state,
            source_family_id,
            tax_domain,
            source_class,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        if source_id != "KNW-VAT-REGISTRATION":
            return ()
        return (
            KnowledgeSourceVersionSummaryRecord(
                source_version_id="123e4567-e89b-12d3-a456-426614174500",
                source_id="KNW-VAT-REGISTRATION",
                source_family_id="KNW-VAT-REGISTRATION-FAMILY",
                title="VAT registration threshold",
                source_class="tax_law",
                tax_domain="vat",
                authority_level="statute",
                publication_state="published",
                source_input_origin="official_source_upload",
                source_version_form="point_in_time_consolidation",
                effective_from="2024-01-01",
                effective_to=None,
                tax_year=None,
                supersedes_source_version_id=None,
                superseded_by_source_version_id=None,
            ),
        )


class _MissingTurnoverGenerator:
    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        assert context["answer_mode"] == "grounded_knowledge"
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_mode=context["answer_mode"],
            answer_text="Please provide your turnover before a VAT conclusion is given.",
            integrity_signals=ResponseIntegritySignals(
                unverified_or_contradicting_user_facts=["turnover"],
            ),
        )

    def stream_generate(
        self,
        context: GovernedSynthesisContext,
    ) -> Iterator[LLMResponseStreamEvent]:
        _ = context
        return iter(())


class _CompleteTurnoverGenerator(_MissingTurnoverGenerator):
    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        assert context["answer_mode"] == "grounded_knowledge"
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_mode=context["answer_mode"],
            answer_text="Your stated turnover is below the VAT registration threshold.",
        )


class _PriorTurnoverGenerator(_MissingTurnoverGenerator):
    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_mode=context["answer_mode"],
            answer_text="No, your stated KES 4.2 million turnover is below KES 5 million.",
        )


def test_missing_turnover_after_grounding_uses_existing_clarification_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launch_extractor(**_: object) -> Future[ExtractedSemanticContext]:
        future: Future[ExtractedSemanticContext] = Future()
        future.set_result(
            {
                "tax_year": None,
                "regime": None,
                "intent_class": "lookup_grounded_knowledge",
                "tax_domain_hint": "vat",
                "confidence": 0.99,
                "inferred_fields": [],
                "implicit_context": {},
                "extraction_status": "extracted",
                "is_tax_related": True,
                "requires_computation": False,
                "stated_facts": {
                    "income_amount_kes": None,
                    "income_frequency": None,
                    "turnover_amount_kes": None,
                    "residency_status": None,
                    "filing_status": None,
                    "confidence_per_field": {},
                },
            }
        )
        return future

    monkeypatch.setattr(prompt_intent_envelope, "_launch_semantic_extractor", launch_extractor)
    client = TestClient(
        create_app(
            knowledge_repository=_VatKnowledgeRepository(),
            llm_response_generator=_MissingTurnoverGenerator(),
        ),
        headers=orchestration_auth_headers(user_reference="post-grounding-fact-gap"),
    )
    prompt_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-post-grounding-fact-gap-001",
        "channel": "chat",
        "prompt": {"text": "Do I need to register for VAT?", "format": "plain_text"},
    }

    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-post-grounding-fact-gap-decide-001"},
        json=prompt_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-post-grounding-fact-gap-execute-001"},
        json={
            **prompt_payload,
            "idempotency_key": "idem-post-grounding-fact-gap-001",
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "clarification_required"
    assert detail["context"]["required_context_fields"] == ["turnover"]
    assert "turnover" in detail["message"].lower()
    event_types = {
        event["event_type"]
        for event in list_income_tax_audit_events(
            correlation_id="corr-post-grounding-fact-gap-execute-001"
        )
    }
    assert "response_synthesis_resolved" in event_types
    assert "orchestration_request_rejected" not in event_types


def test_stated_turnover_does_not_trigger_post_grounding_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launch_extractor(**_: object) -> Future[ExtractedSemanticContext]:
        future: Future[ExtractedSemanticContext] = Future()
        future.set_result(
            {
                "tax_year": None,
                "regime": None,
                "intent_class": "lookup_grounded_knowledge",
                "tax_domain_hint": "vat",
                "confidence": 0.99,
                "inferred_fields": [],
                "implicit_context": {},
                "extraction_status": "extracted",
                "is_tax_related": True,
                "requires_computation": False,
                "stated_facts": {
                    "income_amount_kes": None,
                    "income_frequency": None,
                    "turnover_amount_kes": 4200000.0,
                    "residency_status": None,
                    "filing_status": None,
                    "confidence_per_field": {"turnover_amount_kes": 0.99},
                },
            }
        )
        return future

    monkeypatch.setattr(prompt_intent_envelope, "_launch_semantic_extractor", launch_extractor)
    client = TestClient(
        create_app(
            knowledge_repository=_VatKnowledgeRepository(),
            llm_response_generator=_CompleteTurnoverGenerator(),
            conversation_state_protector=LocalAesGcmConversationStateProtector(key=b"a" * 32),
        ),
        headers=orchestration_auth_headers(user_reference="post-grounding-known-turnover"),
    )
    prompt_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-post-grounding-known-turnover-001",
        "channel": "chat",
        "prompt": {
            "text": "My turnover is KES 4.2 million. Do I need to register for VAT?",
            "format": "plain_text",
        },
    }

    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-post-grounding-known-turnover-decide-001"},
        json=prompt_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-post-grounding-known-turnover-execute-001"},
        json={
            **prompt_payload,
            "idempotency_key": "idem-post-grounding-known-turnover-001",
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        },
    )

    assert response.status_code == 200
    assert (
        response.json()["response"]["integrity_signals"]["unverified_or_contradicting_user_facts"]
        == []
    )


@pytest.mark.parametrize(
    "current_turnover",
    [None, 6000000.0],
)
def test_prior_turnover_anchor_case_remains_grounded_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    current_turnover: float | None,
) -> None:
    def launch_extractor(**_: object) -> Future[ExtractedSemanticContext]:
        future: Future[ExtractedSemanticContext] = Future()
        future.set_result(
            {
                "tax_year": None,
                "regime": None,
                "intent_class": "lookup_grounded_knowledge",
                "tax_domain_hint": "vat",
                "confidence": 0.99,
                "inferred_fields": [],
                "implicit_context": {},
                "extraction_status": "extracted",
                "is_tax_related": True,
                "requires_computation": False,
                "stated_facts": {
                    "income_amount_kes": None,
                    "income_frequency": None,
                    "turnover_amount_kes": current_turnover,
                    "residency_status": None,
                    "filing_status": None,
                    "confidence_per_field": {"turnover_amount_kes": 0.99},
                },
            }
        )
        return future

    monkeypatch.setattr(prompt_intent_envelope, "_launch_semantic_extractor", launch_extractor)
    protector = LocalAesGcmConversationStateProtector(key=b"a" * 32)
    user_reference = "post-grounding-prior-turnover"
    conversation_id = "conv-post-grounding-prior-turnover-001"
    conversation_store = InMemoryConversationStateStore()
    conversation_store.put(
        {
            "execution_id": "execution-prior-turnover-001",
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": conversation_id,
            "user_id": orchestration_test_user_id(user_reference),
            "context_payload": {
                "stated_facts": protector.protect(
                    {
                        "income_amount_kes": None,
                        "income_frequency": None,
                        "turnover_amount_kes": 4200000.0,
                        "residency_status": None,
                        "filing_status": None,
                        "confidence_per_field": {"turnover_amount_kes": 0.99},
                    }
                )
            },
        }
    )
    client = TestClient(
        create_app(
            knowledge_repository=_VatKnowledgeRepository(),
            llm_response_generator=_PriorTurnoverGenerator(),
            conversation_state_store=conversation_store,
            conversation_state_protector=protector,
        ),
        headers=orchestration_auth_headers(user_reference=user_reference),
    )
    prompt_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": conversation_id,
        "channel": "chat",
        "prompt": {"text": "Do I need to register for VAT?", "format": "plain_text"},
    }

    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-post-grounding-prior-turnover-decide-001"},
        json=prompt_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-post-grounding-prior-turnover-execute-001"},
        json={
            **prompt_payload,
            "idempotency_key": f"idem-post-grounding-prior-turnover-{current_turnover}",
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    signals = body["response"]["integrity_signals"]
    assert body["grounding_status"] == "grounded"
    assert body["selected_route"]["route_id"] == "knowledge_search_route_v1"
    assert body["response"]["answer_mode"] == "grounded_knowledge"
    assert "below kes 5 million" in body["response"]["answer_text"].lower()
    assert signals["unverified_or_contradicting_user_facts"] == []
    assert signals["grounding_contradictions"] == []
    assert signals["confidence_flag"] == "high"
