"""Rollout control checks for orchestration synthesis and conversation continuity."""

from __future__ import annotations

import json
from typing import cast
from datetime import date
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.config import OrchestrationRuntimeRolloutConfig
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.feature_flags import reset_runtime_safety_control_config
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator
from services.orchestration.app.pilot_tenant_guardrails import (
    evaluate_orchestration_pilot_tenant_feature,
)
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.orchestration_eval_harness import load_orchestration_eval_cases


@pytest.fixture(autouse=True)
def _reset_runtime_safety_controls() -> (
    Generator[None, None, None]
):  # pyright: ignore[reportUnusedFunction]
    reset_runtime_safety_control_config()
    yield
    reset_runtime_safety_control_config()


class _KnowledgeRouteStub:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return (_knowledge_record(),)

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (source_ids, anchor_ids)
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
            source_id,
            source_family_id,
            tax_domain,
            source_class,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        return (_knowledge_version_summary(),)


def test_synthesis_disable_returns_structured_execution_envelope_canonically() -> None:
    app = create_app(
        knowledge_repository=_KnowledgeRouteStub(),
        llm_response_generator=_build_generator("This response should not be used."),
        runtime_rollout_config=OrchestrationRuntimeRolloutConfig(
            response_synthesis_enabled=False,
            conversation_continuity_enabled=True,
        ),
    )
    client = TestClient(app)
    payload = _knowledge_execute_payload(client, id_suffix="synthesis-disabled")

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-rollout-synthesis-disabled-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["grounded_evidence"][0]["source_id"] == "KNW-ITA-15-2"
    assert body["response"]["status"] == "failed"
    assert body["response"]["answer_text"] is None
    assert body["response"]["answer_mode"] == "grounded_knowledge"
    assert body["response"]["citations"][0]["source_id"] == "KNW-ITA-15-2"
    assert (
        "OpenAI response synthesis is disabled by orchestration rollout control."
        in body["response"]["warnings"]
    )
    assert body["errors"][0]["reason_code"] == "response_synthesis_disabled"


def test_continuity_disable_blocks_followup_but_keeps_single_turn_execution_working() -> None:
    store = InMemoryConversationStateStore()
    app = create_app(
        conversation_state_store=store,
        llm_response_generator=_build_generator(
            "The governed single-turn compute request is accepted and pending completion."
        ),
        runtime_rollout_config=OrchestrationRuntimeRolloutConfig(
            response_synthesis_enabled=True,
            conversation_continuity_enabled=False,
        ),
    )
    client = TestClient(app)

    _execute_prompt(
        client,
        conversation_id="conv-rollout-continuity-001",
        user_id="user_rollout_continuity_001",
        idempotency_key="idem-rollout-continuity-seed-001",
        prompt_text=(
            "compute income tax for resident employment lane in tax year 2021 "
            "under KIT-VER-20210101-A."
        ),
    )

    followup_decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-rollout-continuity-followup-001"},
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-rollout-continuity-001",
            "channel": "chat",
            "prompt": {
                "text": "what about 2023?",
                "format": "plain_text",
            },
        },
    )

    assert followup_decide.status_code == 200
    followup_body = followup_decide.json()
    assert followup_body["status"] == "clarification_required"
    assert followup_body["clarification"]["reason_code"] == "conversation_continuity_disabled"
    assert followup_body["clarification"]["required_context_fields"] == ["prior_execution_context"]


def test_rollout_eval_corpus_includes_disabled_synthesis_and_continuity_cases() -> None:
    case_ids = {case["case_id"] for case in load_orchestration_eval_cases("adversarial")}
    golden_case_ids = {case["case_id"] for case in load_orchestration_eval_cases("golden")}

    assert "adversarial_blocked_continuity_rollout" in case_ids
    assert "adversarial_blocked_synthesis_rollout" in case_ids
    assert "golden_synthesis_disabled_fallback" in golden_case_ids


def test_canary_tenant_is_allowed_for_governed_orchestration_feature_when_lane_is_supported() -> (
    None
):
    decision = evaluate_orchestration_pilot_tenant_feature(
        tenant_id="pilot_tenant_limited",
        feature_key="response_synthesis",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="corr-rollout-canary-allowed-001",
    )

    assert decision["guard_status"] == "allowed"
    assert decision["reason_code"] == "pilot_tenant_allow"
    assert decision["rollout_state"] == "canary"


def test_blocked_tenant_is_rejected_canonically_for_governed_orchestration_feature() -> None:
    decision = evaluate_orchestration_pilot_tenant_feature(
        tenant_id="pilot_tenant_disabled",
        feature_key="response_synthesis",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="corr-rollout-blocked-tenant-001",
    )

    assert decision["guard_status"] == "blocked"
    assert decision["reason_code"] == "tenant_disabled"
    assert decision["rollout_state"] is None


def _execute_prompt(
    client: TestClient,
    *,
    conversation_id: str,
    user_id: str,
    idempotency_key: str,
    prompt_text: str,
) -> None:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": conversation_id,
        "channel": "chat",
        "prompt": {
            "text": prompt_text,
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": f"corr-{conversation_id}-decide"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    execute = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": f"corr-{conversation_id}-execute"},
        json={
            **decide_payload,
            "user_id": user_id,
            "idempotency_key": idempotency_key,
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        },
    )
    assert execute.status_code == 200


def _knowledge_execute_payload(client: TestClient, *, id_suffix: str) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": f"conv-rollout-knowledge-{id_suffix}",
        "channel": "chat",
        "prompt": {
            "text": (
                "lookup statutory authority for allowable deductions in income tax "
                "effective 2024-12-27."
            ),
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-rollout-knowledge-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "user_id": "user_rollout_knowledge_001",
        "idempotency_key": f"idem-rollout-knowledge-{id_suffix}",
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def _build_generator(answer_text: str) -> OpenAIResponsesLLMResponseGenerator:
    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        assert request_payload["model"] == "gpt-test-orchestration"
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": answer_text,
                        "cited_indices": [1],
                    },
                    sort_keys=True,
                )
            }
        )

    return OpenAIResponsesLLMResponseGenerator(
        config=OrchestrationOpenAIResponseSynthesisConfig(
            api_key="test-key",
            model="gpt-test-orchestration",
            base_url="https://api.openai.test/v1",
            timeout_seconds=5.0,
            max_retries=0,
        ),
        transport=cast(TransportCallable, transport),
    )


def _knowledge_record() -> KnowledgeSearchRecord:
    return KnowledgeSearchRecord(
        source_id="KNW-ITA-15-2",
        title="Income Tax Act (Cap. 470), Section 15(2)",
        url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
        source_type="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        effective_from="1974-01-01",
        effective_to=None,
        tax_year=None,
        anchor_id="income-tax-act-15-2",
        content="Allowable deductions in production of income under section 15(2).",
    )


def _knowledge_version_summary() -> KnowledgeSourceVersionSummaryRecord:
    return KnowledgeSourceVersionSummaryRecord(
        source_version_id="123e4567-e89b-12d3-a456-426614174100",
        source_id="KNW-ITA-15-2",
        source_family_id="KNW-ITA-FAMILY",
        title="Income Tax Act (Cap. 470), Section 15(2)",
        source_class="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        publication_state="published",
        source_input_origin="official_source_upload",
        source_version_form="point_in_time_consolidation",
        effective_from="1974-01-01",
        effective_to=None,
        tax_year=None,
        supersedes_source_version_id=None,
        superseded_by_source_version_id=None,
    )
