"""Deterministic final-outcome envelope tests for orchestration prompt execution boundary."""

from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.main import create_app

HEADERS = {
    CORRELATION_ID_HEADER_NAME: "corr-orch-final-envelope-001",
    TRACE_ID_HEADER_NAME: "trace-orch-final-envelope-001",
}


def _base_prompt_payload() -> dict[str, object]:
    return {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-final-envelope-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
    }


def _build_execution_payload() -> dict[str, object]:
    client = TestClient(create_app())
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers=HEADERS,
        json=_base_prompt_payload(),
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **_base_prompt_payload(),
        "user_id": "user_final_envelope_001",
        "idempotency_key": "idem-final-envelope-001",
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def test_prompt_execute_returns_structured_final_outcome_with_trace_lineage() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers=HEADERS,
        json=_build_execution_payload(),
    )

    assert response.status_code == 200
    payload = response.json()
    final_outcome = cast(dict[str, object], payload["final_outcome"])
    trace = cast(dict[str, object], final_outcome["trace"])
    lineage_refs = cast(dict[str, object], trace["lineage_refs"])
    result = cast(dict[str, object], final_outcome["result"])

    assert payload["correlation_id"] == HEADERS[CORRELATION_ID_HEADER_NAME]
    assert payload["trace_id"] == HEADERS[TRACE_ID_HEADER_NAME]
    assert set(final_outcome.keys()) == {"outcome_status", "message", "trace", "audit", "result"}
    assert final_outcome["outcome_status"] == "pending"
    assert trace["correlation_id"] == HEADERS[CORRELATION_ID_HEADER_NAME]
    assert trace["trace_id"] == HEADERS[TRACE_ID_HEADER_NAME]
    assert lineage_refs["decision_id"] == payload["decision_id"]
    assert lineage_refs["execution_id"] == payload["execution_id"]
    assert lineage_refs["prompt_checksum"] == payload["prompt_checksum"]
    assert result["decision_id"] == payload["decision_id"]
    assert result["execution_status"] == payload["execution_status"]


def test_prompt_execute_rejection_returns_canonical_error_with_trace_linkage() -> None:
    client = TestClient(create_app())
    payload = _build_execution_payload()
    payload["selected_route"] = {
        "route_id": "unsupported-route-v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers=HEADERS,
        json=payload,
    )

    assert response.status_code == 400
    detail = cast(dict[str, object], response.json()["detail"])
    assert detail["error_code"] == "invalid_route_selection"
    assert detail["reason"] == "route_selection_mismatch"
    assert detail["reason_code"] == "route_selection_mismatch"
    assert detail["correlation_id"] == HEADERS[CORRELATION_ID_HEADER_NAME]
    assert detail["trace_id"] == HEADERS[TRACE_ID_HEADER_NAME]


def test_repeated_identical_prompt_execute_requests_keep_stable_final_outcome_shape() -> None:
    client = TestClient(create_app())
    payload = _build_execution_payload()
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert canonical_json_dumps(second_payload) == canonical_json_dumps(first_payload)
