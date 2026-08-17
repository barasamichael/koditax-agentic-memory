"""OpenAI-backed response synthesis checks for orchestration runtime."""

from __future__ import annotations

import json
from uuid import uuid4
from typing import NoReturn
from typing import cast
from pathlib import Path
from datetime import date
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.orchestration.app import main as orchestration_main
from services.orchestration.app import llm_response_generator as llm_response_generator_module
from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.config import SelfCritiqueConfig
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.config import load_orchestration_openai_response_synthesis_config
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.audit_events import InMemoryOrchestrationAuditEventStore
from services.orchestration.app.audit_events import set_default_orchestration_audit_event_store
from services.orchestration.app.audit_events import reset_default_orchestration_audit_event_store
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import LLMResponseStreamEvent
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator
from services.orchestration.app.answer_verification_engine import AnswerVerificationEngine
from services.orchestration.app.response_integrity_signals import ResponseIntegritySignals
from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)
from services.orchestration.app.synthesis_integrity_constants import MAX_VERIFICATION_RETRIES
from services.orchestration.app.synthesis_integrity_constants import MAX_SYNTHESIS_TOOL_ITERATIONS
from services.orchestration.app.synthesis_integrity_constants import FACT_EXTRACTION_MIN_CONFIDENCE

_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


@pytest.fixture(autouse=True)
def _load_openai_environment() -> None:  # pyright: ignore[reportUnusedFunction]
    load_dotenv(Path(".env"))


def _test_client(app: FastAPI) -> TestClient:
    app.state.conversation_state_protector = LocalAesGcmConversationStateProtector(key=b"a" * 32)
    return TestClient(app, headers=orchestration_auth_headers())


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


class _SignalGenerator:
    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_text="The computation request is accepted and pending completion.",
            answer_mode=context["answer_mode"],
            integrity_signals=ResponseIntegritySignals(
                unsupported_claims=["A retained unsupported claim."],
                contradictions_found=["A model-declared contradiction."],
            ),
        )

    def stream_generate(
        self, context: GovernedSynthesisContext
    ) -> Iterator[LLMResponseStreamEvent]:
        _ = context
        return iter(())


class _VerificationRetryGenerator:
    def __init__(self) -> None:
        self.contexts: list[GovernedSynthesisContext] = []

    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        self.contexts.append(context)
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_text="The governed answer is available.",
            answer_mode=context["answer_mode"],
        )

    def stream_generate(
        self, context: GovernedSynthesisContext
    ) -> Iterator[LLMResponseStreamEvent]:
        _ = context
        return iter(())


class _VerificationSequence:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self._results = results
        self.calls = 0

    def verify_answer(
        self,
        *,
        answer: UnifiedAnswerResponseModel,
        grounded_evidence: list[dict[str, object]],
        original_prompt: str,
    ) -> dict[str, object]:
        _ = (answer, grounded_evidence, original_prompt)
        result = self._results[self.calls]
        self.calls += 1
        return result


def test_compute_execution_returns_synthesized_answer_section() -> None:
    app = create_app(
        llm_response_generator=_build_generator(
            answer_text=(
                "The computation request is accepted and currently pending downstream completion."
            ),
            cited_indices=[],
        )
    )
    client = _test_client(app)
    payload = _compute_execute_payload(client)

    first = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-compute-001"},
        json=payload,
    )
    second = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-compute-001"},
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    body = first.json()
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "compute_execution"
    assert body["response"]["answer_text"].startswith("The computation request is accepted")
    assert body["response"]["citations"] == []
    assert body["response"]["integrity_signals"] == _default_integrity_signals()
    assert body["mapped_result"]["action_status"] == "pending"
    assert body["final_outcome"]["result"]["response"]["status"] == "generated"
    assert body["final_outcome"]["result"]["response"]["integrity_signals"] == (
        _default_integrity_signals()
    )
    assert "response_synthesis_resolved" in body["final_outcome"]["audit"]["event_types"]
    assert body == second.json()


def test_resolved_audit_payload_matches_nonempty_response_integrity_signals() -> None:
    app = create_app(llm_response_generator=_SignalGenerator())
    client = _test_client(app)
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-integrity-audit-resolved-001"},
        json=_compute_execute_payload(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["integrity_signals"]["confidence_flag"] == "medium"
    assert "response_synthesis_resolved" in body["final_outcome"]["audit"]["event_types"]


def test_answer_verification_reports_the_failed_check_category() -> None:
    answer = UnifiedAnswerResponseModel.model_validate(
        {
            "status": "generated",
            "answer_text": "The governed answer is available.",
            "answer_mode": "grounded_knowledge",
            "citations": [
                {
                    "citation_index": 1,
                    "source_id": "unknown-source",
                    "source_version_id": "version-1",
                    "anchor_id": "anchor-1",
                    "title": "Unknown source",
                    "url": "https://example.test/source",
                    "authority_level": "statute",
                    "temporal_applicability": "current",
                }
            ],
        }
    )

    result = AnswerVerificationEngine().verify_answer(
        answer=answer,
        grounded_evidence=[],
        original_prompt="What is the governed answer?",
    )

    assert result["failed_checks"] == ["citation_validity"]


def test_verification_failure_retries_once_with_targeted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _VerificationRetryGenerator()
    verifier = _VerificationSequence(
        [
            {
                "is_verified": False,
                "confidence_score": 0.2,
                "issues_found": ["Citation references unknown source: invalid-source"],
                "failed_checks": ["citation_validity"],
                "verification_type": "composite",
            },
            {
                "is_verified": True,
                "confidence_score": 0.9,
                "issues_found": [],
                "failed_checks": [],
                "verification_type": "composite",
            },
        ]
    )
    monkeypatch.setattr(orchestration_main, "_answer_verification_engine", verifier)
    set_default_orchestration_audit_event_store(InMemoryOrchestrationAuditEventStore())
    try:
        client = _test_client(create_app(llm_response_generator=generator))
        response = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-verification-retry-once-001"},
            json=_compute_execute_payload(client),
        )
        events = list_income_tax_audit_events(correlation_id="corr-verification-retry-once-001")
    finally:
        reset_default_orchestration_audit_event_store()

    assert response.status_code == 200
    assert verifier.calls == 2
    assert len(generator.contexts) == 2
    assert "citation_validity" in generator.contexts[1]["warnings"][-1]
    assert "invalid-source" in generator.contexts[1]["warnings"][-1]
    assert response.json()["response"]["integrity_signals"] == {
        **_default_integrity_signals(),
        "verification_is_verified": True,
        "verification_confidence": 0.9,
    }
    resolved = [event for event in events if event["event_type"] == "response_synthesis_resolved"]
    assert resolved[0]["context"]["verification_retry_used"] is True


def test_verification_failure_after_retry_returns_normal_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _VerificationRetryGenerator()
    verifier = _VerificationSequence(
        [
            {
                "is_verified": False,
                "confidence_score": 0.2,
                "issues_found": ["Answer text is empty or None"],
                "failed_checks": ["evidence_consistency"],
                "verification_type": "composite",
            },
            {
                "is_verified": False,
                "confidence_score": 0.3,
                "issues_found": ["Answer text is empty or None"],
                "failed_checks": ["evidence_consistency"],
                "verification_type": "composite",
            },
        ]
    )
    monkeypatch.setattr(orchestration_main, "_answer_verification_engine", verifier)
    client = _test_client(create_app(llm_response_generator=generator))
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-verification-retry-fails-001"},
        json=_compute_execute_payload(client),
    )

    assert response.status_code == 200
    assert verifier.calls == 2
    assert len(generator.contexts) == 2
    assert response.json()["response"]["integrity_signals"]["verification_is_verified"] is False
    assert response.json()["response"]["integrity_signals"]["verification_confidence"] == 0.3
    assert response.json()["response"]["integrity_signals"]["confidence_flag"] == "low"


def test_verified_answer_does_not_retry_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _VerificationRetryGenerator()
    verifier = _VerificationSequence(
        [
            {
                "is_verified": True,
                "confidence_score": 1.0,
                "issues_found": [],
                "failed_checks": [],
                "verification_type": "composite",
            }
        ]
    )
    monkeypatch.setattr(orchestration_main, "_answer_verification_engine", verifier)
    client = _test_client(create_app(llm_response_generator=generator))
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-verification-no-retry-001"},
        json=_compute_execute_payload(client),
    )

    assert response.status_code == 200
    assert verifier.calls == 1
    assert len(generator.contexts) == 1


def test_forms_execution_returns_synthesized_answer_section() -> None:
    app = create_app(
        llm_response_generator=_build_generator(
            answer_text="The governed income tax form artifact is ready for downstream use.",
            cited_indices=[],
        )
    )
    client = _test_client(app)
    payload = _single_step_execute_payload(
        client,
        conversation_id="conv-llm-forms-001",
        prompt_text="generate form for income tax return preparation.",
        user_id="user_llm_forms_001",
        idempotency_key="idem-llm-forms-001-v145",
    )

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-forms-execute-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "forms_execution"
    assert body["response"]["citations"] == []
    assert body["mapped_result"]["action_status"] == "accepted"
    assert body["adapter_response"]["result_payload"]["artifact_id"]


def test_reports_execution_returns_synthesized_answer_section() -> None:
    app = create_app(
        llm_response_generator=_build_generator(
            answer_text="The governed report artifact has been generated successfully.",
            cited_indices=[],
        )
    )
    client = _test_client(app)
    payload = _single_step_execute_payload(
        client,
        conversation_id="conv-llm-reports-001",
        prompt_text="generate report for income tax audit trail.",
        user_id="user_llm_reports_001",
        idempotency_key="idem-llm-reports-001-v145",
    )

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-llm-reports-execute-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "reports_execution"
    assert body["response"]["citations"] == []
    assert body["mapped_result"]["action_status"] == "accepted"
    assert body["adapter_response"]["result_payload"]["report_id"]


def test_default_openai_sdk_transport_uses_official_library_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_client_kwargs: dict[str, object] = {}
    seen_request_payload: dict[str, object] = {}

    class _FakeSDKResponse:
        output_text = json.dumps(
            {
                "answer_text": "The governed answer was produced through the SDK client.",
                "cited_indices": [],
                "unverified_or_contradicting_user_facts": [],
            },
            sort_keys=True,
        )

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "id": "resp_test_001",
                "output_text": self.output_text,
            }

    class _FakeResponsesClient:
        def create(self, **kwargs: object) -> _FakeSDKResponse:
            seen_request_payload.update(kwargs)
            return _FakeSDKResponse()

    class _FakeOpenAIClient:
        def __init__(
            self,
            *,
            api_key: str | None,
            base_url: str,
            timeout: float,
            max_retries: int,
        ) -> None:
            seen_client_kwargs.update(
                {
                    "api_key": api_key,
                    "base_url": base_url,
                    "timeout": timeout,
                    "max_retries": max_retries,
                }
            )
            self.responses = _FakeResponsesClient()

    monkeypatch.setattr(
        llm_response_generator_module,
        "OpenAI",
        _FakeOpenAIClient,
    )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=OrchestrationOpenAIResponseSynthesisConfig(
            api_key="sdk-test-key",
            model="gpt-test-orchestration",
            base_url="https://api.openai.test/v1",
            timeout_seconds=7.5,
            max_retries=2,
        ),
    )
    result = generator.generate(_minimal_synthesis_context())

    assert seen_client_kwargs == {
        "api_key": "sdk-test-key",
        "base_url": "https://api.openai.test/v1",
        "timeout": 7.5,
        "max_retries": 2,
    }
    assert seen_request_payload["model"] == "gpt-test-orchestration"
    response_format = cast(dict[str, object], seen_request_payload["text"])["format"]
    schema = cast(dict[str, object], cast(dict[str, object], response_format)["schema"])
    assert cast(dict[str, object], response_format)["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(cast(list[str], schema["required"])) == {
        "answer_text",
        "cited_indices",
        "unverified_or_contradicting_user_facts",
    }
    fact_gap_items = cast(
        dict[str, object],
        cast(dict[str, object], schema["properties"])["unverified_or_contradicting_user_facts"],
    )["items"]
    assert cast(dict[str, object], fact_gap_items)["enum"] == [
        "income",
        "turnover",
        "residency",
        "filing_status",
    ]
    assert result.status == "generated"
    assert result.answer_text == "The governed answer was produced through the SDK client."


def test_synthesis_draft_requires_post_grounding_fact_gap_field() -> None:
    with pytest.raises(llm_response_generator_module.LLMResponseGenerationError) as error:
        llm_response_generator_module._parse_answer_draft(  # pyright: ignore[reportPrivateUsage]
            {
                "output_text": json.dumps(
                    {
                        "answer_text": "A draft without the required fact-gap field.",
                        "cited_indices": [],
                    },
                    sort_keys=True,
                )
            }
        )

    assert error.value.reason_code == "invalid_synthesis_response_shape"


def test_zero_based_live_citation_indices_are_normalized_to_governed_evidence() -> None:
    context = _minimal_grounded_synthesis_context()

    citations = llm_response_generator_module._map_citations(  # pyright: ignore[reportPrivateUsage]
        context=context,
        cited_indices=[0],
    )

    assert [citation.citation_index for citation in citations] == [1]
    assert [citation.source_id for citation in citations] == ["KNW-ITA-15-2"]


def test_invalid_live_citation_indices_still_fail_validation() -> None:
    context = _minimal_grounded_synthesis_context()

    with pytest.raises(llm_response_generator_module.LLMResponseGenerationError) as error:
        llm_response_generator_module._map_citations(  # pyright: ignore[reportPrivateUsage]
            context=context,
            cited_indices=[99],
        )

    assert error.value.error_code == "response_synthesis_failed"
    assert error.value.reason_code == "invalid_response_citations"


def test_strict_json_answer_text_extractor_emits_only_displayable_deltas() -> None:
    extractor = llm_response_generator_module._AnswerTextStreamExtractor()  # pyright: ignore[reportPrivateUsage]

    assert extractor.push('{"cited_indices":[],"answer_') == ""
    assert extractor.push('text":"VAT is charged') == "VAT is charged"
    assert extractor.push(' on taxable supplies\\u') == " on taxable supplies"
    assert (
        extractor.push('2014 not on exempt supplies","cited_indices":[]}')
        == "— not on exempt supplies"
    )


def test_stream_generate_emits_only_answer_text_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Event:
        def __init__(self, delta: str) -> None:
            self.type = "response.output_text.delta"
            self.delta = delta

    class _FinalResponse:
        output_text = json.dumps(
            {
                "answer_text": "VAT is charged on taxable supplies.",
                "cited_indices": [],
                "unverified_or_contradicting_user_facts": [],
            }
        )

        @staticmethod
        def model_dump(*, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"output": []}

    class _Stream:
        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        @staticmethod
        def __iter__() -> object:
            return iter(
                [
                    _Event('{"answer_text":"VAT is '),
                    _Event('charged on taxable supplies.","cited_indices":[]}'),
                ]
            )

        @staticmethod
        def get_final_response() -> _FinalResponse:
            return _FinalResponse()

    class _Responses:
        @staticmethod
        def stream(**_: object) -> _Stream:
            return _Stream()

    class _Client:
        responses = _Responses()

    def _create_client(_: object) -> _Client:
        return _Client()

    monkeypatch.setattr(
        llm_response_generator_module,
        "_create_openai_client",
        _create_client,
    )
    generator = OpenAIResponsesLLMResponseGenerator(config=_test_synthesis_config())

    events = list(generator.stream_generate(_minimal_synthesis_context()))

    assert [event.delta for event in events if event.event_type == "delta"] == [
        "VAT is ",
        "charged on taxable supplies.",
    ]
    assert events[-1].response is not None
    assert events[-1].response.answer_text == "VAT is charged on taxable supplies."


def test_grounded_synthesis_dispatches_governed_tool_and_audits_result() -> None:
    request_payloads: list[dict[str, object]] = []

    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        request_payloads.append(request_payload)
        if len(request_payloads) == 1:
            tools = cast(list[dict[str, object]], request_payload["tools"])
            assert [tool["name"] for tool in tools] == [
                "search_records",
                "retrieve_records",
                "timeline_search_records",
            ]
            assert all(tool["strict"] is True for tool in tools)
            tool_parameters = {
                str(tool["name"]): cast(dict[str, object], tool["parameters"]) for tool in tools
            }
            assert tool_parameters["search_records"]["required"] == [
                "query",
                "source_type",
                "tax_domain",
                "effective_date",
            ]
            assert tool_parameters["retrieve_records"]["required"] == [
                "source_ids",
                "anchor_ids",
            ]
            assert tool_parameters["timeline_search_records"]["required"] == [
                "query",
                "source_type",
                "tax_domain",
                "start_date",
                "end_date",
            ]
            assert all(
                parameters["additionalProperties"] is False
                for parameters in tool_parameters.values()
            )
            return ResponsesTransportResult(
                payload={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-search-001",
                            "name": "search_records",
                            "arguments": json.dumps(
                                {
                                    "query": "allowable deductions",
                                    "source_type": None,
                                    "tax_domain": "income_tax",
                                    "effective_date": None,
                                },
                                sort_keys=True,
                            ),
                        }
                    ]
                }
            )
        conversation = cast(list[dict[str, object]], request_payload["input"])
        function_outputs = [
            item for item in conversation if item.get("type") == "function_call_output"
        ]
        assert len(function_outputs) == 1
        tool_output = json.loads(cast(str, function_outputs[0]["output"]))
        assert tool_output["citation_projection"]["citations"][0]["source_id"] == "KNW-ITA-15-2"
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": "Section 15(2) supports allowable deductions [1].",
                        "cited_indices": [1],
                        "unverified_or_contradicting_user_facts": [],
                    },
                    sort_keys=True,
                )
            }
        )

    set_default_orchestration_audit_event_store(InMemoryOrchestrationAuditEventStore())
    try:
        generator = OpenAIResponsesLLMResponseGenerator(
            config=_test_synthesis_config(),
            critique_config=_disabled_self_critique_config(),
            transport=cast(TransportCallable, transport),
            knowledge_repository_provider=lambda: _KnowledgeRouteStub(),
        )
        response = generator.generate(_tool_enabled_grounded_synthesis_context())
        events = list_income_tax_audit_events(correlation_id="corr-synthesis-tool-001")
    finally:
        reset_default_orchestration_audit_event_store()

    assert response.integrity_signals.synthesis_tool_iterations_used == 1
    assert response.citations[0].source_id == "KNW-ITA-15-2"
    tool_events = [
        event for event in events if event["event_type"] == "response_synthesis_tool_call_requested"
    ]
    assert len(tool_events) == 1
    assert tool_events[0]["context"] == {
        "execution_id": "execution-synthesis-tool-001",
        "iteration_number": 1,
        "resource_id": "execution-synthesis-tool-001",
        "result_summary": "1 governed evidence record(s): KNW-ITA-15-2",
        "tenant_id": "pilot_tenant_alpha",
        "tool_arguments": {
            "effective_date": None,
            "query": "allowable deductions",
            "source_type": None,
            "tax_domain": "income_tax",
        },
        "tool_name": "search_records",
        "user_id": "user-synthesis-tool-001",
    }


def test_grounded_synthesis_stops_tools_at_hard_iteration_limit() -> None:
    request_payloads: list[dict[str, object]] = []

    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        request_payloads.append(request_payload)
        if len(request_payloads) <= MAX_SYNTHESIS_TOOL_ITERATIONS:
            return ResponsesTransportResult(
                payload={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": f"call-search-{len(request_payloads)}",
                            "name": "search_records",
                            "arguments": json.dumps(
                                {
                                    "query": "allowable deductions",
                                    "source_type": None,
                                    "tax_domain": "income_tax",
                                    "effective_date": None,
                                },
                                sort_keys=True,
                            ),
                        }
                    ]
                }
            )
        assert "tools" not in request_payload
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": "Section 15(2) supports allowable deductions [1].",
                        "cited_indices": [1],
                        "unverified_or_contradicting_user_facts": [],
                    },
                    sort_keys=True,
                )
            }
        )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=_test_synthesis_config(),
        critique_config=_disabled_self_critique_config(),
        transport=cast(TransportCallable, transport),
        knowledge_repository_provider=lambda: _KnowledgeRouteStub(),
    )
    response = generator.generate(_tool_enabled_grounded_synthesis_context())

    assert response.integrity_signals.synthesis_tool_iterations_used == 3
    assert len(request_payloads) == 4
    assert all("tools" in payload for payload in request_payloads[:3])


def test_grounded_synthesis_retry_does_not_exceed_tool_iteration_limit() -> None:
    request_payloads: list[dict[str, object]] = []
    tool_call_payloads: list[dict[str, object]] = []

    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        request_payloads.append(request_payload)
        if len(request_payloads) == 2:
            raise llm_response_generator_module.LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message="Temporary transport failure.",
                reason_code="openai_transport_failure",
            )
        if "tools" in request_payload:
            tool_call_payloads.append(request_payload)
            return ResponsesTransportResult(
                payload={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": f"call-search-{len(request_payloads)}",
                            "name": "search_records",
                            "arguments": json.dumps(
                                {
                                    "query": "allowable deductions",
                                    "source_type": None,
                                    "tax_domain": "income_tax",
                                    "effective_date": None,
                                },
                                sort_keys=True,
                            ),
                        }
                    ]
                }
            )
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": "Section 15(2) supports allowable deductions [1].",
                        "cited_indices": [1],
                        "unverified_or_contradicting_user_facts": [],
                    },
                    sort_keys=True,
                )
            }
        )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=_test_synthesis_config(max_retries=1),
        critique_config=_disabled_self_critique_config(),
        transport=cast(TransportCallable, transport),
        knowledge_repository_provider=lambda: _KnowledgeRouteStub(),
    )
    response = generator.generate(_tool_enabled_grounded_synthesis_context())

    assert response.integrity_signals.synthesis_tool_iterations_used == 3
    assert len(tool_call_payloads) == 3


def test_compute_synthesis_payload_omits_tools() -> None:
    payload = llm_response_generator_module._build_responses_request_payload(  # pyright: ignore[reportPrivateUsage]
        context=_minimal_synthesis_context(),
        config=_test_synthesis_config(),
    )

    assert "tools" not in payload


def test_grounded_synthesis_rejects_unapproved_tool_request() -> None:
    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        assert "tools" in request_payload
        return ResponsesTransportResult(
            payload={
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-unapproved-001",
                        "name": "unapproved_tool",
                        "arguments": "{}",
                    }
                ]
            }
        )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=_test_synthesis_config(),
        critique_config=_disabled_self_critique_config(),
        transport=cast(TransportCallable, transport),
    )

    with pytest.raises(llm_response_generator_module.LLMResponseGenerationError) as error:
        generator.generate(_tool_enabled_grounded_synthesis_context())

    assert error.value.reason_code == "invalid_synthesis_tool_call"


def test_self_critique_retries_and_clears_unsupported_claims() -> None:
    critique_payloads: list[dict[str, object]] = []
    critique_results: list[dict[str, object]] = [
        {
            "unsupported_claims": ["The draft says a non-cited exemption applies."],
            "contradictions_found": ["Draft conflicts with cited exemption evidence."],
            "revised_answer": "Unsupported first-pass revision.",
        },
        {
            "unsupported_claims": [],
            "contradictions_found": [],
            "revised_answer": "Section 15(2) supports allowable deductions [1].",
        },
    ]

    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        if "messages" in request_payload:
            critique_payloads.append(request_payload)
            return ResponsesTransportResult(
                payload={
                    "output_text": json.dumps(
                        critique_results[len(critique_payloads) - 1],
                        sort_keys=True,
                    )
                }
            )
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": (
                            "Section 15(2) supports allowable deductions [1]. "
                            "A non-cited exemption applies [1]."
                        ),
                        "cited_indices": [1],
                        "unverified_or_contradicting_user_facts": [],
                    },
                    sort_keys=True,
                )
            }
        )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=_test_synthesis_config(),
        critique_config=_test_self_critique_config(max_retries=1),
        transport=cast(TransportCallable, transport),
    )
    result = generator.generate(_minimal_grounded_synthesis_context())

    assert result.answer_text is not None
    assert result.answer_text.startswith("Section 15(2) supports allowable deductions")
    assert "Unsupported first-pass revision" not in result.answer_text
    assert "non-cited exemption" not in result.answer_text
    assert result.integrity_signals.unsupported_claims == []
    assert result.integrity_signals.contradictions_found == []
    assert len(critique_payloads) == 2
    assert _self_critique_required_fields(critique_payloads[0]) == {
        "unsupported_claims",
        "contradictions_found",
        "revised_answer",
    }
    assert "The draft says a non-cited exemption applies." in json.dumps(critique_payloads[1])
    assert "Unsupported first-pass revision." in json.dumps(critique_payloads[1])


def test_self_critique_stops_at_retry_limit_and_surfaces_unresolved_claims() -> None:
    critique_payloads: list[dict[str, object]] = []

    def transport(
        config: OrchestrationOpenAIResponseSynthesisConfig,
        request_payload: dict[str, object],
    ) -> ResponsesTransportResult:
        assert config.model == "gpt-test-orchestration"
        if "messages" in request_payload:
            critique_payloads.append(request_payload)
            return ResponsesTransportResult(
                payload={
                    "output_text": json.dumps(
                        {
                            "unsupported_claims": [
                                "The draft keeps stating an unsupported penalty."
                            ],
                            "contradictions_found": [
                                "The draft conflicts with the cited penalty excerpt."
                            ],
                            "revised_answer": (
                                "Section 15(2) supports allowable deductions, with an "
                                "unsupported penalty still present [1]."
                            ),
                        },
                        sort_keys=True,
                    )
                }
            )
        return ResponsesTransportResult(
            payload={
                "output_text": json.dumps(
                    {
                        "answer_text": (
                            "Section 15(2) supports allowable deductions, with an "
                            "unsupported penalty still present [1]."
                        ),
                        "cited_indices": [1],
                        "unverified_or_contradicting_user_facts": [],
                    },
                    sort_keys=True,
                )
            }
        )

    generator = OpenAIResponsesLLMResponseGenerator(
        config=_test_synthesis_config(),
        critique_config=_test_self_critique_config(max_retries=2),
        transport=cast(TransportCallable, transport),
    )
    result = generator.generate(_minimal_grounded_synthesis_context())

    assert len(critique_payloads) == 3
    assert result.integrity_signals.unsupported_claims == [
        "The draft keeps stating an unsupported penalty."
    ]
    assert result.integrity_signals.contradictions_found == [
        "The draft conflicts with the cited penalty excerpt."
    ]
    assert result.integrity_signals.verification_is_verified is True
    assert result.integrity_signals.confidence_flag == "high"


@pytest.mark.integration
def test_live_openai_grounded_synthesis_returns_structured_citations() -> None:
    generator = _live_openai_generator()
    try:
        result = generator.generate(_minimal_grounded_synthesis_context())
    except llm_response_generator_module.LLMResponseGenerationError as error:
        _skip_live_openai_if_unavailable(error)

    assert result.status == "generated"
    assert result.answer_mode == "grounded_knowledge"
    assert result.answer_text is not None
    assert result.answer_text.strip()
    assert result.citations
    assert all(citation.source_id == "KNW-ITA-15-2" for citation in result.citations)
    assert all(citation.citation_index >= 1 for citation in result.citations)


@pytest.mark.integration
def test_live_openai_stream_generate_emits_draft_and_final_answer() -> None:
    generator = _live_openai_generator()
    try:
        events = list(generator.stream_generate(_minimal_grounded_synthesis_context()))
    except llm_response_generator_module.LLMResponseGenerationError as error:
        _skip_live_openai_if_unavailable(error)

    deltas = [event.delta for event in events if event.event_type == "delta"]
    assert deltas
    assert events[-1].response is not None
    assert events[-1].response.status == "generated"
    assert events[-1].response.answer_text is not None
    assert events[-1].response.answer_text.strip()


def _build_generator(
    *,
    answer_text: str,
    cited_indices: list[int],
) -> OpenAIResponsesLLMResponseGenerator:
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
                        "cited_indices": cited_indices,
                        "unverified_or_contradicting_user_facts": [],
                    },
                    sort_keys=True,
                )
            }
        )

    return OpenAIResponsesLLMResponseGenerator(
        config=_test_synthesis_config(),
        transport=cast(TransportCallable, transport),
    )


def _test_synthesis_config(
    *,
    max_retries: int = 0,
) -> OrchestrationOpenAIResponseSynthesisConfig:
    return OrchestrationOpenAIResponseSynthesisConfig(
        api_key="test-key",
        model="gpt-test-orchestration",
        base_url="https://api.openai.test/v1",
        timeout_seconds=5.0,
        max_retries=max_retries,
    )


def _test_self_critique_config(*, max_retries: int) -> SelfCritiqueConfig:
    return SelfCritiqueConfig(
        api_key="test-key",
        model="gpt-test-orchestration",
        base_url="https://api.openai.test/v1",
        timeout_seconds=5.0,
        max_retries=max_retries,
        enabled=True,
    )


def _disabled_self_critique_config() -> SelfCritiqueConfig:
    return SelfCritiqueConfig(
        api_key=None,
        model=None,
        base_url="https://api.openai.test/v1",
        timeout_seconds=5.0,
        max_retries=0,
        enabled=False,
    )


def _self_critique_required_fields(payload: dict[str, object]) -> set[str]:
    response_format = cast(dict[str, object], payload["response_format"])
    json_schema = cast(dict[str, object], response_format["json_schema"])
    schema = cast(dict[str, object], json_schema["schema"])
    assert json_schema["strict"] is True
    assert schema["additionalProperties"] is False
    return {str(item) for item in cast(list[object], schema["required"])}


def _default_integrity_signals() -> dict[str, object]:
    assert MAX_VERIFICATION_RETRIES == 1
    assert MAX_SYNTHESIS_TOOL_ITERATIONS == 3
    assert FACT_EXTRACTION_MIN_CONFIDENCE == 0.7
    return {
        "verification_is_verified": True,
        "verification_confidence": 1.0,
        "unsupported_claims": [],
        "contradictions_found": [],
        "grounding_contradictions": [],
        "unverified_or_contradicting_user_facts": [],
        "synthesis_tool_iterations_used": 0,
        "confidence_flag": "high",
    }


def _single_step_execute_payload(
    client: TestClient,
    *,
    conversation_id: str,
    prompt_text: str,
    user_id: str,
    idempotency_key: str,
) -> dict[str, object]:
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
    return {
        **decide_payload,
        "idempotency_key": _test_execution_id(idempotency_key),
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def _compute_execute_payload(client: TestClient) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-llm-compute-001-v2",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-llm-compute-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "idempotency_key": _test_execution_id("idem-llm-compute-001-v2"),
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


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
            "plan_id": "plan-sdk-test-001",
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


def _minimal_grounded_synthesis_context() -> GovernedSynthesisContext:
    context = _minimal_synthesis_context()
    context["answer_mode"] = "grounded_knowledge"
    context["grounded_evidence"] = [
        {
            "source_id": "KNW-ITA-15-2",
            "source_version_id": "123e4567-e89b-12d3-a456-426614174100",
            "anchor_id": "income-tax-act-15-2",
            "title": "Income Tax Act (Cap. 470), Section 15(2)",
            "url": "https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
            "source_type": "tax_law",
            "authority_level": "statute",
            "tax_domain": "income_tax",
            "effective_from": "1974-01-01",
            "effective_to": None,
            "tax_year": None,
            "publication_state": "published",
            "source_version_form": "point_in_time_consolidation",
            "content": "Allowable deductions are listed in section 15(2).",
            "canonical_source_ref": "https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
            "knowledge_route_mode": "search",
            "timeline_position": None,
            "temporal_applicability": "current-effective",
        }
    ]
    context["explanation_items"] = [
        {
            "anchor_id": "income-tax-act-15-2",
            "explanation_text": "Allowable deductions are listed in section 15(2).",
        }
    ]
    context["citations"] = [
        {
            "citation_index": 1,
            "source_id": "KNW-ITA-15-2",
            "source_version_id": "123e4567-e89b-12d3-a456-426614174100",
            "anchor_id": "income-tax-act-15-2",
            "title": "Income Tax Act (Cap. 470), Section 15(2)",
            "url": "https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
            "authority_level": "statute",
            "temporal_applicability": "current-effective",
        }
    ]
    context["warnings"] = ["This answer is limited to the cited governed knowledge evidence."]
    return context


def _tool_enabled_grounded_synthesis_context() -> GovernedSynthesisContext:
    context = _minimal_grounded_synthesis_context()
    context["synthesis_tool_runtime"] = {
        "correlation_id": "corr-synthesis-tool-001",
        "trace_id": "trace-synthesis-tool-001",
        "execution_id": "execution-synthesis-tool-001",
        "tenant_id": "pilot_tenant_alpha",
        "user_id": "user-synthesis-tool-001",
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
    }
    return context


def _live_openai_generator() -> OpenAIResponsesLLMResponseGenerator:
    config = load_orchestration_openai_response_synthesis_config()
    if not config.configured:
        pytest.skip("OpenAI response synthesis is not configured in the environment.")
    return OpenAIResponsesLLMResponseGenerator(
        config=config,
        knowledge_repository_provider=lambda: _KnowledgeRouteStub(),
    )


def _skip_live_openai_if_unavailable(
    error: llm_response_generator_module.LLMResponseGenerationError,
) -> NoReturn:
    if error.reason_code in {"missing_openai_configuration", "openai_transport_failure"}:
        pytest.skip(f"Live OpenAI synthesis is unavailable: {error.reason_code}")
    raise error
