"""Focused coverage for deterministic grounding contradiction detection."""

from __future__ import annotations

from datetime import date
from collections.abc import Iterator
from typing import cast

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.orchestration.app.action_adapter_registry import dispatch_route_action_request_with_repository
from services.orchestration.app.action_adapter_contract import (
    ActionAdapterCapabilityContext,
    ActionAdapterRequest,
)
from tests.orchestration_auth_support import orchestration_auth_headers
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.orchestration.app.llm_synthesis_context import detect_grounding_contradictions
from services.orchestration.app.llm_synthesis_context import build_governed_synthesis_context
from services.orchestration.app.llm_response_generator import LLMResponseStreamEvent
from services.orchestration.app.llm_response_generator import (
    _build_structured_input,  # pyright: ignore[reportPrivateUsage]
)


def test_detect_grounding_contradictions_finds_conflicting_turnover_tax_rates() -> None:
    findings = detect_grounding_contradictions(
        [
            _knowledge_record(source_id="source-current", raw_rate_text="3%", rate_value=0.03),
            _knowledge_record(
                source_id="source-historical",
                raw_rate_text="1%",
                rate_value=0.01,
            ),
        ]
    )

    assert findings == [
        {
            "claim_topic": "turnover_tax_rate",
            "source_a_id": "source-current",
            "source_a_value": "3%",
            "source_b_id": "source-historical",
            "source_b_value": "1%",
        }
    ]


def test_synthesis_context_surfaces_contradictions_and_requires_explicit_response() -> None:
    context = _build_context(
        [
            _evidence_item(source_id="source-current", raw_rate_text="3%", rate_value=0.03),
            _evidence_item(
                source_id="source-historical",
                raw_rate_text="1%",
                rate_value=0.01,
            ),
        ]
    )

    assert context["grounding_contradictions"] == [
        {
            "claim_topic": "turnover_tax_rate",
            "source_a_id": "source-current",
            "source_a_value": "3%",
            "source_b_id": "source-historical",
            "source_b_value": "1%",
        }
    ]
    structured_input = _build_structured_input(context)
    assert "=== GROUNDING CONTRADICTIONS TO ADDRESS ===" in structured_input
    assert "Sources disagree on turnover_tax_rate" in structured_input
    assert "Address this disagreement explicitly rather than choosing one silently." in (
        structured_input
    )


def test_non_conflicting_grounding_does_not_change_synthesis_context() -> None:
    context = _build_context(
        [
            _evidence_item(source_id="source-current", raw_rate_text="3%", rate_value=0.03),
            _evidence_item(
                source_id="source-confirming",
                raw_rate_text="3%",
                rate_value=0.03,
            ),
        ]
    )

    assert context["grounding_contradictions"] == []
    assert "=== GROUNDING CONTRADICTIONS TO ADDRESS ===" not in _build_structured_input(context)


def test_response_surfaces_deterministic_grounding_contradictions() -> None:
    client = TestClient(
        create_app(
            knowledge_repository=_ConflictingKnowledgeRepository(),
            llm_response_generator=_ContradictionAwareGenerator(),
        ),
        headers=orchestration_auth_headers(user_reference="grounding-contradictions"),
    )
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-grounding-contradictions-001",
        "channel": "chat",
        "prompt": {
            "text": "What is the turnover tax rate under income tax in Kenya?",
            "format": "plain_text",
        },
    }
    correlation_id = "corr-grounding-contradictions-001"
    headers = {"X-Correlation-ID": correlation_id}
    decide = client.post("/v1/orchestration/prompt/decide", headers=headers, json=decide_payload)

    assert decide.status_code == 200
    decision = decide.json()
    assert decision["selected_route"] is not None
    prompt_text = "What is the turnover tax rate under income tax in Kenya?"
    selected_route = cast(dict[str, str], decision["selected_route"])
    capability_context: ActionAdapterCapabilityContext = {
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
    }
    auth_context: dict[str, str | None] = {
        "tenant_id": "pilot_tenant_alpha",
        "user_id": "grounding-contradictions",
    }
    route_payload: dict[str, object] = {
        "route_mode": "search",
        "query": prompt_text,
        "tax_domain": "income_tax",
    }
    adapter_request: ActionAdapterRequest = {
        "action_type": "knowledge_search_knowledge",
        "correlation_id": correlation_id,
        "trace_id": correlation_id,
        "submission_payload_ref": "idem-grounding-contradictions-001",
        "capability_context": capability_context,
        "route_id": selected_route["route_id"],
        "target_service": selected_route["target_service"],
        "target_operation": selected_route["target_operation"],
        "route_payload": route_payload,
        "auth_context": auth_context,
    }
    adapter_response = dispatch_route_action_request_with_repository(
        adapter_request,
        knowledge_repository=_ConflictingKnowledgeRepository(),
    )
    assert adapter_response["adapter_status"] == "accepted"
    grounded_evidence = cast(
        list[dict[str, object]],
        cast(dict[str, object], adapter_response.get("result_payload"))["grounded_evidence"],
    )
    context = build_governed_synthesis_context(
        prompt_text=prompt_text,
        tax_domain_hint="income_tax",
        intent_class=decision["intent_class"],
        plan={
            "plan_id": "plan-grounding-contradictions-001",
            "plan_status": "planned",
            "planning_mode": "single_step",
            "execution_ready": True,
            "steps": [],
        },
        mapped_result={"action_status": "resolved"},
        final_outcome={"message": "Knowledge lookup completed."},
        selected_route=decision["selected_route"],
        adapter_response=adapter_response,
        step_results=None,
        step_summary=None,
        grounded_evidence=grounded_evidence,
        explanation_items=None,
        citations=None,
        authority_summary=None,
        temporal_applicability=None,
    )
    response = _ContradictionAwareGenerator().generate(context)
    assert "Sources disagree" in cast(str, response.answer_text)
    assert cast(list[dict[str, object]], context["grounding_contradictions"]) == [
        {
            "claim_topic": "turnover_tax_rate",
            "source_a_id": "source-current",
            "source_a_value": "3%",
            "source_b_id": "source-historical",
            "source_b_value": "1%",
        }
    ]


class _ContradictionAwareGenerator:
    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        assert context["grounding_contradictions"]
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_mode=context["answer_mode"],
            answer_text=(
                "**Sources disagree:** the current source states 3%, while the historical "
                "source states 1%."
            ),
        )

    def stream_generate(
        self,
        context: GovernedSynthesisContext,
    ) -> Iterator[LLMResponseStreamEvent]:
        _ = context
        return iter(())


class _ConflictingKnowledgeRepository:
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
            _knowledge_record(source_id="source-current", raw_rate_text="3%", rate_value=0.03),
            _knowledge_record(
                source_id="source-historical",
                raw_rate_text="1%",
                rate_value=0.01,
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
        if source_id not in {"source-current", "source-historical"}:
            return ()
        return (
            KnowledgeSourceVersionSummaryRecord(
                source_version_id=f"{source_id}-version",
                source_id=source_id,
                source_family_id=f"{source_id}-family",
                title=f"{source_id} turnover tax guidance",
                source_class="tax_guidance",
                tax_domain="income_tax",
                authority_level="guidance",
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


def _build_context(grounded_evidence: list[dict[str, object]]) -> GovernedSynthesisContext:
    return build_governed_synthesis_context(
        prompt_text="What is the turnover tax rate?",
        tax_domain_hint="income_tax",
        intent_class="lookup_grounded_knowledge",
        plan={
            "plan_id": "plan-grounding-contradictions-001",
            "plan_status": "planned",
            "planning_mode": "single_step",
            "execution_ready": True,
            "steps": [],
        },
        mapped_result={"action_status": "resolved"},
        final_outcome={"message": "Knowledge lookup completed."},
        selected_route={
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
        },
        adapter_response=None,
        step_results=None,
        step_summary=None,
        grounded_evidence=grounded_evidence,
        explanation_items=None,
        citations=None,
        authority_summary=None,
        temporal_applicability=None,
    )


def _knowledge_record(*, source_id: str, raw_rate_text: str, rate_value: float) -> KnowledgeSearchRecord:
    return KnowledgeSearchRecord(
        source_id=source_id,
        title=f"{source_id} turnover tax guidance",
        url=f"https://example.test/{source_id}",
        source_type="tax_guidance",
        tax_domain="income_tax",
        authority_level="guidance",
        effective_from="2024-01-01",
        effective_to=None,
        tax_year=None,
        anchor_id=f"{source_id}-anchor",
        content=f"The turnover tax rate is {raw_rate_text}.",
        canonical_claims=(
            {
                "entity_type": "regime",
                "entity_label": "turnover tax",
                "predicate": "tax_rate",
                "polarity": "affirms",
                "raw_value_text": raw_rate_text,
                "normalized_value": {
                    "kind": "rate",
                    "raw_text": raw_rate_text,
                    "number_value": rate_value,
                    "unit": "percent",
                    "basis": "percentage",
                },
                "taxpayer_category": "business",
                "tax_domain": "income_tax",
                "jurisdiction": "Kenya",
                "jurisdiction_status": "verified",
                "effective_from": "2024-01-01",
                "effective_to": None,
                "tax_year": 2024,
                "period_type": "annual",
                "current_effective": True,
                "historical_effective": False,
                "authority_level": "guidance",
                "source_type": "tax_guidance",
                "conditions": [],
                "exceptions": [],
                "claim_excerpt": f"The turnover tax rate is {raw_rate_text}.",
                "claim_topic": "turnover_tax_rate",
                "extraction_confidence": 1.0,
                "source_trust_status": "verified_official_source",
                "provenance": {
                    "source_id": source_id,
                    "source_version_id": f"{source_id}-version",
                    "anchor_id": f"{source_id}-anchor",
                    "url": f"https://example.test/{source_id}",
                    "title": f"{source_id} turnover tax guidance",
                    "source_type": "tax_guidance",
                    "authority_level": "guidance",
                    "effective_from": "2024-01-01",
                    "effective_to": None,
                    "tax_year": 2024,
                    "source_trust_status": "verified_official_source",
                },
            },
        ),
    )


def _evidence_item(*, source_id: str, raw_rate_text: str, rate_value: float) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_version_id": f"{source_id}-version",
        "anchor_id": f"{source_id}-anchor",
        "title": f"{source_id} turnover tax guidance",
        "url": f"https://example.test/{source_id}",
        "source_type": "tax_guidance",
        "authority_level": "guidance",
        "tax_domain": "income_tax",
        "effective_from": "2024-01-01",
        "effective_to": None,
        "tax_year": None,
        "publication_state": "published",
        "source_version_form": "point_in_time_consolidation",
        "grounding_status": "grounded",
        "content": f"The turnover tax rate is {raw_rate_text}.",
        "canonical_source_ref": f"https://example.test/{source_id}",
        "knowledge_route_mode": "search",
        "timeline_position": None,
        "canonical_claims": [
            {
                "entity_type": "regime",
                "entity_label": "turnover tax",
                "predicate": "tax_rate",
                "polarity": "affirms",
                "raw_value_text": raw_rate_text,
                "normalized_value": {
                    "kind": "rate",
                    "raw_text": raw_rate_text,
                    "number_value": rate_value,
                    "unit": "percent",
                    "basis": "percentage",
                },
                "taxpayer_category": "business",
                "tax_domain": "income_tax",
                "jurisdiction": "Kenya",
                "jurisdiction_status": "verified",
                "effective_from": "2024-01-01",
                "effective_to": None,
                "tax_year": 2024,
                "period_type": "annual",
                "current_effective": True,
                "historical_effective": False,
                "authority_level": "guidance",
                "source_type": "tax_guidance",
                "conditions": [],
                "exceptions": [],
                "claim_excerpt": f"The turnover tax rate is {raw_rate_text}.",
                "claim_topic": "turnover_tax_rate",
                "extraction_confidence": 1.0,
                "source_trust_status": "verified_official_source",
                "provenance": {
                    "source_id": source_id,
                    "source_version_id": f"{source_id}-version",
                    "anchor_id": f"{source_id}-anchor",
                    "url": f"https://example.test/{source_id}",
                    "title": f"{source_id} turnover tax guidance",
                    "source_type": "tax_guidance",
                    "authority_level": "guidance",
                    "effective_from": "2024-01-01",
                    "effective_to": None,
                    "tax_year": 2024,
                    "source_trust_status": "verified_official_source",
                },
            }
        ],
    }
