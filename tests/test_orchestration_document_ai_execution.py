"""Governed document-ai execution checks for orchestration runtime."""

from __future__ import annotations

import json
from typing import cast

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.orchestration.app.config import OrchestrationOpenAIResponseSynthesisConfig
from services.orchestration.app.llm_response_generator import TransportCallable
from services.orchestration.app.llm_response_generator import ResponsesTransportResult
from services.orchestration.app.llm_response_generator import OpenAIResponsesLLMResponseGenerator


def test_document_extraction_execution_returns_structured_and_synthesized_response() -> None:
    app = create_app(
        llm_response_generator=_build_generator(
            answer_text="The governed document extraction request has been queued for processing."
        )
    )
    client = TestClient(app)
    payload = _execute_payload_for_prompt(
        client,
        conversation_id="conv-document-ai-001",
        prompt_text="extract document for income tax filing support.",
        user_id="user_document_ai_001",
        idempotency_key="idem-document-ai-001-v145",
    )

    first = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-document-ai-001"},
        json=payload,
    )
    second = client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": "corr-document-ai-001"},
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    body = first.json()
    assert body["selected_route"]["target_service"] == "document_ai"
    assert body["mapped_result"]["action_status"] == "pending"
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "document_extraction"
    assert body["response"]["citations"] == []
    assert body["adapter_response"]["result_payload"]["extraction_job_id"]
    assert body["adapter_response"]["result_payload"]["lifecycle_status"] == "queued"
    assert body["response"]["warnings"] == [
        "The downstream tool result is pending and not yet a final settled outcome.",
        "Document extraction details are limited to the queued governed extraction payload.",
    ]
    assert body == second.json()


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
