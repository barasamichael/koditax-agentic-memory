"""Off-topic deterministic gating tests for orchestration prompt decision pipeline."""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app


def test_off_topic_prompt_is_rejected_deterministically() -> None:
    client = TestClient(create_app())
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-off-topic-001",
        "channel": "chat",
        "prompt": {
            "text": "Tell me the weather in Nairobi tomorrow.",
            "format": "plain_text",
        },
    }
    first = client.post("/v1/orchestration/prompt/decide", json=payload)
    second = client.post("/v1/orchestration/prompt/decide", json=payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "off_topic_prompt"
    assert first_detail["reason"] == "off_topic_prompt"
    assert first_detail["reason_code"] == "off_topic_prompt"
    assert second_detail["error_code"] == first_detail["error_code"]
    assert second_detail["reason"] == first_detail["reason"]
    assert second_detail["reason_code"] == first_detail["reason_code"]
    assert set(first_detail.keys()) == set(second_detail.keys())
