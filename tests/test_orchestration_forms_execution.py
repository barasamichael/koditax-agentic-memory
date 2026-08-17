"""Governed forms execution checks for orchestration runtime."""

from __future__ import annotations

import json
from typing import cast

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator


def test_income_tax_forms_execution_returns_structured_and_synthesized_response() -> None:
    app = create_app(
        llm_response_generator=_build_generator(
            answer_text=(
                "The income tax form artifact is ready and referenced in the governed payload."
            )
        )
    )
    client = TestClient(app)
    payload = _execute_payload_for_prompt(
        client,
        conversation_id="conv-forms-income-001",
        prompt_text="generate form for income tax return preparation.",
        user_id="user_forms_income_001",
        idempotency_key="idem-forms-income-001-v145",
    )

    first = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-forms-income-001"},
        json=payload,
    )
    second = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-forms-income-001"},
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    body = first.json()
    assert body["selected_route"]["target_service"] == "forms"
    assert body["mapped_result"]["action_status"] == "accepted"
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "forms_execution"
    assert body["response"]["citations"] == []
    assert body["adapter_response"]["result_payload"]["artifact_id"]
    assert body == second.json()


def test_health_contribution_forms_execution_returns_form_ready_reference_deterministically() -> (
    None
):
    app = create_app(
        llm_response_generator=_build_generator(
            answer_text=(
                "The health contribution filing output has been mapped into a "
                "governed form-ready reference."
            )
        )
    )
    client = TestClient(app)
    payload = _execute_payload_for_prompt(
        client,
        conversation_id="conv-forms-health-001",
        prompt_text="generate form for health contribution filing.",
        user_id="user_forms_health_001",
        idempotency_key="idem-forms-health-001-v145",
    )

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-forms-health-001"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tax_domain_hint"] == "health_contribution"
    assert body["mapped_result"]["action_status"] == "accepted"
    assert body["response"]["answer_mode"] == "forms_execution"
    assert body["adapter_response"]["result_payload"]["form_ready_reference"]
    assert body["response"]["assumptions"] == [
        "Form artifact details are limited to governed orchestration output."
    ]


def _build_generator(*, answer_text: str) -> OpenAIResponsesLLMResponseGenerator:
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
                        "cited_indices": [],
                        "unverified_or_contradicting_user_facts": [],
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


def _execute_payload_for_prompt(
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
        "user_id": user_id,
        "idempotency_key": idempotency_key,
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }
