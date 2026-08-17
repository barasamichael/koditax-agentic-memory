"""Safety hardening checks for orchestration execution and synthesis policy gates."""

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
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.feature_flags import set_kill_switch
from services.orchestration.app.feature_flags import set_orchestration_flag
from services.orchestration.app.feature_flags import reset_runtime_safety_control_config
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator


@pytest.fixture(autouse=True)
def _reset_runtime_safety_controls() -> Generator[None, None, None]:  # pyright: ignore[reportUnusedFunction]
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


def test_multi_step_compute_plus_grounding_can_be_blocked_by_orchestration_flag() -> None:
    set_orchestration_flag(feature_key="compute_plus_grounding_execution", enabled=False)
    app = create_app(
        knowledge_repository=_KnowledgeRouteStub(),
        llm_response_generator=_build_generator("This response should not be used."),
    )
    client = TestClient(app)
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-safety-multi-step-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 under "
                "KIT-VER-20230701-A with legal basis."
            ),
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-safety-multi-step-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()

    execute = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-safety-multi-step-execute-001"},
        json={
            **decide_payload,
            "user_id": "user_safety_multi_step_001",
            "idempotency_key": "idem-safety-multi-step-001",
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        },
    )

    assert execute.status_code == 409
    detail = execute.json()["detail"]
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason_code"] == "orchestration_disabled_by_flag"


def test_high_risk_action_context_honors_action_kill_switch() -> None:
    set_kill_switch(switch_key="global_action", enabled=True)
    client = TestClient(create_app())
    payload = _compute_execute_payload(client, id_suffix="high-risk")
    payload["action_context"] = {
        "risk_class": "high",
        "confirmation_state": "confirmed",
        "step_up_proof_state": "bound",
    }

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-safety-high-risk-001"},
        json=payload,
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["error_code"] == "unsafe_action_path"
    assert detail["reason_code"] == "action_kill_switch_active"


def test_grounded_legal_basis_synthesis_can_be_blocked_with_structured_fallback() -> None:
    set_orchestration_flag(feature_key="grounded_legal_basis_synthesis", enabled=False)
    app = create_app(
        knowledge_repository=_KnowledgeRouteStub(),
        llm_response_generator=_build_generator("This grounded answer should not be used."),
    )
    client = TestClient(app)
    payload = _knowledge_execute_payload(client, id_suffix="grounded-gate")

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-safety-grounded-gate-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["status"] == "failed"
    assert body["response"]["answer_mode"] == "grounded_knowledge"
    assert body["response"]["citations"][0]["source_id"] == "KNW-ITA-15-2"
    assert body["errors"][0]["reason_code"] == "orchestration_disabled_by_flag"


def _compute_execute_payload(client: TestClient, *, id_suffix: str) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": f"conv-safety-compute-{id_suffix}",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 under "
                "KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
    }
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": "corr-safety-compute-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "user_id": "user_safety_compute_001",
        "idempotency_key": f"idem-safety-compute-{id_suffix}",
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def _knowledge_execute_payload(client: TestClient, *, id_suffix: str) -> dict[str, object]:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": f"conv-safety-knowledge-{id_suffix}",
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
        headers={"X-Correlation-ID": "corr-safety-knowledge-decide-001"},
        json=decide_payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **decide_payload,
        "user_id": "user_safety_knowledge_001",
        "idempotency_key": f"idem-safety-knowledge-{id_suffix}",
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
