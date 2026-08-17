"""Deterministic runtime tests for orchestration prompt-ingestion endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.orchestration.app.main import create_app


def test_prompt_ingestion_accepts_valid_payload_with_traceability() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/ingest",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-orchestration-ingest-001",
            TRACE_ID_HEADER_NAME: "trace-orchestration-ingest-001",
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-ingest-001",
            "channel": "chat",
            "prompt": {
                "text": "Show my PAYE summary for 2025.",
                "format": "plain_text",
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["service"] == "orchestration"
    assert payload["correlation_id"] == "corr-orchestration-ingest-001"
    assert payload["trace_id"] == "trace-orchestration-ingest-001"
    assert payload["ingestion_id"] == payload["prompt_checksum"]
    assert payload["tenant_id"] == "pilot_tenant_alpha"
    assert payload["conversation_id"] == "conv-ingest-001"
    assert payload["channel"] == "chat"
    assert payload["prompt_format"] == "plain_text"


def test_prompt_ingestion_rejects_malformed_payload_deterministically() -> None:
    client = TestClient(create_app())
    first = client.post(
        "/v1/orchestration/prompt/ingest",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-ingest-002",
            "channel": "chat",
            "prompt": "freeform-string-not-object",
        },
    )
    second = client.post(
        "/v1/orchestration/prompt/ingest",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-ingest-002",
            "channel": "chat",
            "prompt": "freeform-string-not-object",
        },
    )
    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "invalid_orchestration_request"
    assert first_detail["reason"] == "invalid_orchestration_request"
    assert first_detail["reason_code"] == "invalid_orchestration_request"
    assert second_detail["error_code"] == first_detail["error_code"]
    assert second_detail["reason"] == first_detail["reason"]
    assert second_detail["reason_code"] == first_detail["reason_code"]
    assert set(first_detail.keys()) == set(second_detail.keys())


def test_prompt_ingestion_rejects_unsupported_prompt_shape_deterministically() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/ingest",
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-ingest-003",
            "channel": "chat",
            "prompt": {
                "text": "Summarize my deductions",
                "format": "markdown",
            },
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason"] == "invalid_orchestration_request"
    assert detail["reason_code"] == "invalid_orchestration_request"
