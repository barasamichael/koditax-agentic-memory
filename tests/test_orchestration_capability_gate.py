"""Capability-gate deterministic rejection tests for orchestration prompt decision pipeline."""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app


def test_unsupported_scope_is_rejected_by_capability_gate_deterministically() -> None:
    client = TestClient(create_app())
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-capability-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2022 "
                "under KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
    }
    first = client.post("/v1/orchestration/prompt/decide", json=payload)
    second = client.post("/v1/orchestration/prompt/decide", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["status"] == "clarification_required"
    assert second_body["status"] == "clarification_required"
    assert first_body["gate_status"] == "clarification_required"
    assert second_body["gate_status"] == "clarification_required"
    assert first_body["clarification"]["reason_code"] == "missing_lane_context"
    assert second_body["clarification"]["reason_code"] == "missing_lane_context"
    assert first_body["clarification"]["message"] == second_body["clarification"]["message"]
    assert first_body["clarification"]["required_context_fields"] == ["supported_lane_id"]
