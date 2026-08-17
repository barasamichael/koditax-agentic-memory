"""Failure handling checks for orchestration OpenAI response synthesis."""

from __future__ import annotations

import json
from typing import cast
from datetime import date
from collections.abc import Iterator
from uuid import uuid4

import httpx
from openai import APITimeoutError
import pytest
from fastapi.testclient import TestClient

from services.orchestration.app import main as orchestration_main
from services.orchestration.app import llm_response_generator as llm_response_generator_module
from services.orchestration.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_synthesis_context import SynthesisContextError
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.orchestration.app.llm_synthesis_context import build_governed_synthesis_context
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import LLMResponseStreamEvent
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import LLMResponseGenerationError
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator
from services.orchestration.app.action_adapter_registry import KnowledgeRouteRepository
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import ActionExecutionEnvelope


def _test_execution_id(value: str) -> str:
    """Keep persistent test requests distinct across suite invocations."""
    return f"{value}-{uuid4().hex}"


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
        normalized_source_ids = {item.lower() for item in source_ids}
        if "knw-ita-15-2" in normalized_source_ids and "income-tax-act-15-2" in anchor_ids:
            return (_knowledge_record(),)
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
        if source_id != "KNW-ITA-15-2":
            return ()
        return (_knowledge_version_summary(),)

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


class _FailingGenerator:
    def __init__(self, *, reason_code: str) -> None:
        self._reason_code = reason_code

    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        _ = context
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="Response synthesis failed under test.",
            reason_code=self._reason_code,
        )

    def stream_generate(
        self, context: GovernedSynthesisContext
    ) -> Iterator[LLMResponseStreamEvent]:
        _ = context
        raise LLMResponseGenerationError(
            error_code="response_synthesis_failed",
            message="Response synthesis failed under test.",
            reason_code=self._reason_code,
        )


def test_missing_openai_configuration_fails_canonically_without_corrupting_execution_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCHESTRATION_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ORCHESTRATION_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(orchestration_main.ConversationTurnResolutionError) as error:
        create_app(knowledge_repository=_KnowledgeRouteStub())

    assert error.value.error_code == "conversation_turn_resolver_not_configured"
    assert error.value.reason_code == "conversation_turn_resolver_not_configured"


def test_malformed_llm_output_fails_canonically() -> None:
    app = create_app(
        knowledge_repository=_KnowledgeRouteStub(),
        llm_response_generator=OpenAIResponsesLLMResponseGenerator(
            config=OrchestrationOpenAIResponseSynthesisConfig(
                api_key="test-key",
                model="gpt-test-orchestration",
                base_url="https://api.openai.test/v1",
                timeout_seconds=5.0,
                max_retries=0,
            ),
            transport=cast(TransportCallable, _malformed_transport),
        ),
    )
    client = TestClient(app)
    payload = _knowledge_execute_payload(client, id_suffix="malformed")

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-malformed-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] == "failed"
    assert body["errors"][0]["error_code"] == "response_synthesis_failed"
    assert body["errors"][0]["reason_code"] == "invalid_synthesis_response_shape"


def test_llm_cannot_reference_citations_outside_governed_evidence() -> None:
    app = create_app(
        knowledge_repository=_KnowledgeRouteStub(),
        llm_response_generator=OpenAIResponsesLLMResponseGenerator(
            config=OrchestrationOpenAIResponseSynthesisConfig(
                api_key="test-key",
                model="gpt-test-orchestration",
                base_url="https://api.openai.test/v1",
                timeout_seconds=5.0,
                max_retries=0,
            ),
            transport=cast(TransportCallable, _invalid_citation_transport),
        ),
    )
    client = TestClient(app)
    payload = _knowledge_execute_payload(client, id_suffix="invalid-citation")

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-invalid-citation-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] == "failed"
    assert body["errors"][0]["error_code"] == "response_synthesis_failed"
    assert body["errors"][0]["reason_code"] == "invalid_response_citations"


def test_openai_sdk_timeout_maps_to_canonical_timeout_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponsesClient:
        def create(self, **kwargs: object) -> object:
            _ = kwargs
            raise APITimeoutError(
                request=httpx.Request(
                    "POST",
                    "https://api.openai.test/v1/responses",
                )
            )

    class _FakeOpenAIClient:
        def __init__(
            self,
            *,
            api_key: str | None,
            base_url: str,
            timeout: float,
            max_retries: int,
        ) -> None:
            _ = (api_key, base_url, timeout, max_retries)
            self.responses = _FakeResponsesClient()

    monkeypatch.setattr(
        llm_response_generator_module,
        "OpenAI",
        _FakeOpenAIClient,
    )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=OrchestrationOpenAIResponseSynthesisConfig(
            api_key="test-key",
            model="gpt-test-orchestration",
            base_url="https://api.openai.test/v1",
            timeout_seconds=5.0,
            max_retries=0,
        )
    )

    with pytest.raises(LLMResponseGenerationError) as error:
        generator.generate(_minimal_synthesis_context())

    assert error.value.error_code == "response_synthesis_failed"
    assert error.value.reason_code == "openai_timeout"


def test_insufficient_forms_payload_fails_canonically_without_corrupting_execution() -> None:
    original_dispatch = orchestration_main.dispatch_route_action_request_with_envelope

    def malformed_forms_dispatch(
        request: ActionExecutionRequest,
        *,
        knowledge_repository: KnowledgeRouteRepository | None = None,
    ) -> ActionExecutionEnvelope:
        envelope = original_dispatch(
            request,
            knowledge_repository=knowledge_repository,
        )
        adapter_response = envelope["adapter_response"]
        assert adapter_response is not None
        adapter_response["provider_reference"] = None
        adapter_response["result_payload"] = {
            "status": "generated",
            "form_type": "income_tax_return",
        }
        return envelope

    orchestration_main.dispatch_route_action_request_with_envelope = malformed_forms_dispatch
    try:
        app = create_app(
            llm_response_generator=_FailingGenerator(reason_code="should_not_execute"),
        )
        client = TestClient(app)
        payload = {
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-llm-forms-001",
            "channel": "chat",
            "prompt": {
                "text": "generate form for income tax return preparation.",
                "format": "plain_text",
            },
        }
        decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers={"X-Correlation-ID": "corr-llm-forms-decide-001"},
            json=payload,
        )
        assert decide.status_code == 200
        decision_body = decide.json()
        execute = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-llm-forms-execute-001"},
            json={
                **payload,
                "user_id": "user_llm_forms_001",
                "idempotency_key": _test_execution_id("idem-llm-forms-001-v145"),
                "intent_class": decision_body["intent_class"],
                "tax_domain_hint": decision_body["tax_domain_hint"],
                "decision_id": decision_body["decision_id"],
                "selected_route": decision_body["selected_route"],
            },
        )
    finally:
        orchestration_main.dispatch_route_action_request_with_envelope = original_dispatch

    assert execute.status_code == 200
    body = execute.json()
    assert body["response"]["status"] == "failed"
    assert body["response"]["answer_mode"] == "unsupported"
    assert body["errors"][0]["reason_code"] == "missing_service_artifact_reference"
    assert body["mapped_result"]["action_status"] == "accepted"
    assert body["adapter_response"]["result_payload"]["form_type"] == "income_tax_return"


def test_insufficient_grounding_for_grounded_answer_fails_canonically() -> None:
    try:
        build_governed_synthesis_context(
            prompt_text="lookup statutory authority for allowable deductions.",
            tax_domain_hint="income_tax",
            intent_class="lookup_grounded_knowledge",
            plan={
                "plan_id": "plan-knowledge-001",
                "plan_version": "2.0.0",
                "plan_status": "planned",
                "planning_mode": "single_step",
                "execution_ready": True,
                "steps": [],
            },
            mapped_result={
                "action_status": "accepted",
                "reason_code": "knowledge_lookup_resolved",
            },
            final_outcome={"message": "resolved"},
            selected_route={
                "route_id": "knowledge_search_route_v1",
                "target_service": "knowledge",
                "target_operation": "search_knowledge",
            },
            adapter_response=None,
            step_results=None,
            step_summary=None,
            grounded_evidence=[],
            explanation_items=None,
            citations=None,
            authority_summary=None,
            temporal_applicability=None,
        )
    except SynthesisContextError as error:
        assert error.reason_code == "insufficient_grounding_for_synthesis"
    else:
        raise AssertionError("Expected insufficient grounding to fail canonically.")


def test_followup_with_insufficient_governed_artifact_context_fails_without_corrupting_output() -> (
    None
):
    store = InMemoryConversationStateStore()
    store.put(
        {
            "execution_id": "exec-followup-insufficient-artifact-001",
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-followup-insufficient-artifact-001",
            "user_id": "user_followup_insufficient_artifact_001",
            "context_payload": {
                "execution_id": "exec-followup-insufficient-artifact-001",
                "prompt_text": "generate form for income tax return preparation.",
                "prompt_checksum": "a" * 64,
                "intent_class": "generate_form_artifact",
                "tax_domain_hint": "income_tax",
                "selected_route": {
                    "route_id": "income_tax_form_generation_route_v1",
                    "target_service": "forms",
                    "target_operation": "generate_income_tax_form_artifact",
                },
                "plan_summary": {
                    "plan_id": "plan-followup-insufficient-artifact-001",
                    "plan_status": "planned",
                    "planning_mode": "single_step",
                    "execution_ready": True,
                },
                "supported_lane_id": None,
                "historical_version_id": None,
                "regime_identifier": None,
                "tax_year": None,
                "mapped_result_summary": {
                    "action_status": "accepted",
                    "reason_code": "forms_artifact_generated",
                    "provider_reference": None,
                },
                "adapter_result_payload": {
                    "service": "forms",
                    "form_version_id": "FRM-V145-001",
                },
                "grounded_citation_summary": [],
                "grounded_evidence_summary": [],
                "service_artifact_summary": {
                    "service": "forms",
                    "form_version_id": "FRM-V145-001",
                },
            },
        }
    )
    app = create_app(
        conversation_state_store=store,
        llm_response_generator=_FailingGenerator(reason_code="should_not_execute"),
    )
    client = TestClient(app)

    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-followup-insufficient-artifact-001",
        "channel": "chat",
        "prompt": {
            "text": "show me the form version",
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-followup-insufficient-artifact-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 400
    detail = cast(dict[str, object], decide.json()["detail"])
    assert detail["error_code"] == "off_topic_prompt"
    assert detail["reason_code"] == "off_topic_prompt"


def _knowledge_execute_payload(client: TestClient, *, id_suffix: str) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": f"conv-llm-knowledge-failure-{id_suffix}",
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
        headers={"X-Correlation-ID": "corr-llm-knowledge-failure-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "user_id": "user_llm_failure_001",
        "idempotency_key": _test_execution_id(f"idem-llm-failure-{id_suffix}"),
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def _malformed_transport(
    config: OrchestrationOpenAIResponseSynthesisConfig,
    request_payload: dict[str, object],
) -> ResponsesTransportResult:
    _ = (config, request_payload)
    return ResponsesTransportResult(payload={"output_text": "not-json"})


def _invalid_citation_transport(
    config: OrchestrationOpenAIResponseSynthesisConfig,
    request_payload: dict[str, object],
) -> ResponsesTransportResult:
    _ = (config, request_payload)
    return ResponsesTransportResult(
        payload={
            "output_text": json.dumps(
                {
                    "answer_text": "This answer incorrectly references a missing citation.",
                    "cited_indices": [99],
                    "unverified_or_contradicting_user_facts": [],
                },
                sort_keys=True,
            )
        }
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


def _minimal_synthesis_context() -> GovernedSynthesisContext:
    return {
        "answer_mode": "compute_execution",
        "prompt_text": "compute income tax for resident employment lane in tax year 2023.",
        "tax_domain_hint": "income_tax",
        "intent_class": "compute_income_tax",
        "plan_summary": {
            "plan_id": "plan-sdk-timeout-001",
            "plan_status": "planned",
            "planning_mode": "single_step",
            "execution_ready": True,
            "step_count": 1,
            "selected_route": {
                "route_id": "income_tax_compute_route_v1",
                "target_service": "tax_core",
                "target_operation": "execute_computation",
            },
            "step_summary": None,
        },
        "computation_summary": {
            "action_status": "pending",
            "reason_code": "tax_core_action_mock_pending",
            "message": "Pending downstream completion.",
        },
        "service_result_summary": None,
        "grounded_evidence": [],
        "explanation_items": [],
        "citations": [],
        "authority_summary": None,
        "temporal_applicability": None,
        "conversation_context_summary": None,
        "assumptions": [],
        "warnings": [],
        "grounding_contradictions": [],
        "fact_mismatches": [],
        "taxpayer_fact_instructions": [],
    }
