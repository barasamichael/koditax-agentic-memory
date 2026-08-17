"""Negative-path guardrail tests for orchestration prompt runtime boundaries."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.orchestration.app.main import create_app

HEADERS = {
    CORRELATION_ID_HEADER_NAME: "corr-orch-guardrails-001",
    TRACE_ID_HEADER_NAME: "trace-orch-guardrails-001",
}


def _supported_prompt_payload() -> dict[str, object]:
    return {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": f"conv-guardrails-{uuid4().hex}",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
    }


def _execution_payload_from_decision() -> dict[str, object]:
    client = TestClient(create_app())
    payload = _supported_prompt_payload()
    decide = client.post(
        "/v1/orchestration/prompt/decide",
        headers=HEADERS,
        json=payload,
    )
    assert decide.status_code == 200
    decision = decide.json()
    return {
        **payload,
        "user_id": "user_guardrails_001",
        "idempotency_key": "idem-guardrails-001",
        "intent_class": decision["intent_class"],
        "tax_domain_hint": decision["tax_domain_hint"],
        "decision_id": decision["decision_id"],
        "selected_route": decision["selected_route"],
    }


def test_supported_safe_execution_path_requires_clarification_instead_of_execution() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers=HEADERS,
        json=_execution_payload_from_decision(),
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason"] == "prompt_context_mismatch"
    assert detail["reason_code"] == "prompt_context_mismatch"


def test_unsupported_scope_is_rejected_before_execution() -> None:
    client = TestClient(create_app())
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": f"conv-guardrails-vat-{uuid4().hex}",
        "channel": "chat",
        "prompt": {
            "text": "Compute VAT filing output for Q3 and submit to regulator.",
            "format": "plain_text",
        },
        "user_id": "user_guardrails_001",
        "idempotency_key": "idem-guardrails-vat-001",
        "intent_class": "compute_income_tax",
        "tax_domain_hint": "income_tax",
        "decision_id": "a" * 64,
        "selected_route": {
            "route_id": "income_tax_compute_route_v1",
            "target_service": "tax_core",
            "target_operation": "execute_computation",
        },
    }
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "invalid_orchestration_request"
    assert first_detail["reason"] == "prompt_context_mismatch"
    assert first_detail["reason_code"] == "prompt_context_mismatch"
    assert set(first_detail.keys()) == set(second_detail.keys())
    assert first_detail == second_detail


def test_malformed_prompt_context_is_rejected_deterministically() -> None:
    client = TestClient(create_app())
    payload = _execution_payload_from_decision()
    payload["intent_class"] = "unsupported_domain_request"
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "invalid_orchestration_request"
    assert first_detail["reason"] == "prompt_context_mismatch"
    assert first_detail["reason_code"] == "prompt_context_mismatch"
    assert set(first_detail.keys()) == set(second_detail.keys())
    assert first_detail == second_detail


def test_unsafe_route_override_is_rejected_deterministically() -> None:
    client = TestClient(create_app())
    payload = _execution_payload_from_decision()
    payload["selected_route"] = {
        "route_id": "income_tax_form_generation_route_v1",
        "target_service": "forms",
        "target_operation": "generate_income_tax_form_artifact",
    }
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "invalid_orchestration_request"
    assert first_detail["reason"] == "prompt_context_mismatch"
    assert first_detail["reason_code"] == "prompt_context_mismatch"
    assert set(first_detail.keys()) == set(second_detail.keys())
    assert first_detail == second_detail


def test_unsafe_high_risk_action_without_prerequisites_is_rejected() -> None:
    client = TestClient(create_app())
    payload = _execution_payload_from_decision()
    payload["action_context"] = {
        "risk_class": "high",
        "confirmation_state": "pending",
        "step_up_proof_state": "unbound",
    }
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "invalid_orchestration_request"
    assert first_detail["reason"] == "prompt_context_mismatch"
    assert first_detail["reason_code"] == "prompt_context_mismatch"
    assert set(first_detail.keys()) == set(second_detail.keys())
    assert first_detail == second_detail
