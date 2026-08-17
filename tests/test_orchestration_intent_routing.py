"""Deterministic intent routing tests for orchestration prompt decision pipeline."""

from __future__ import annotations

import json
from typing import cast
from pathlib import Path

from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.orchestration.app.main import create_app

_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


def test_supported_prompt_resolves_to_deterministic_route_selection() -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-route-001",
        TRACE_ID_HEADER_NAME: "trace-orch-route-001",
    }
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-route-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
    }
    first = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)
    second = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["status"] == "resolved"
    assert first_payload["service"] == "orchestration"
    assert first_payload["correlation_id"] == "corr-orch-route-001"
    assert first_payload["trace_id"] == "trace-orch-route-001"
    assert first_payload["intent_class"] == "compute_income_tax"
    assert first_payload["tax_domain_hint"] == "income_tax"
    assert first_payload["gate_status"] == "allowed"
    assert first_payload["plan"]["planning_mode"] == "single_step"
    assert first_payload["plan"]["execution_ready"] is True
    assert first_payload["selected_route"] == {
        "route_id": "income_tax_compute_route_v1",
        "target_service": "tax_core",
        "target_operation": "execute_computation",
    }
    assert first_payload["decision_id"] == second_payload["decision_id"]
    assert first_payload["selected_route"] == second_payload["selected_route"]


def test_supported_health_prompt_resolves_to_governed_health_route_selection() -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-health-route-001",
        TRACE_ID_HEADER_NAME: "trace-orch-health-route-001",
    }
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-health-route-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute health contribution for sha/shif salaried lane in tax year 2024 "
                "under HCH-VER-20241001-A."
            ),
            "format": "plain_text",
        },
    }

    first = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)
    second = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["status"] == "resolved"
    assert first_payload["tax_domain_hint"] == "health_contribution"
    assert first_payload["intent_class"] == "compute_health_contribution"
    assert first_payload["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"
    assert first_payload["historical_version_id"] == "HCH-VER-20241001-A"
    assert first_payload["regime_identifier"] == "sha_shif"
    assert first_payload["plan"]["planning_mode"] == "single_step"
    assert first_payload["plan"]["execution_ready"] is True
    assert first_payload["selected_route"] == {
        "route_id": "health_contribution_compute_route_v1",
        "target_service": "tax_core",
        "target_operation": "execute_computation",
    }
    assert first_payload["decision_id"] == second_payload["decision_id"]


def test_non_ready_health_prompt_rejects_with_canonical_window_reason() -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-health-route-002",
        TRACE_ID_HEADER_NAME: "trace-orch-health-route-002",
    }
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-health-route-002",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute health contribution for nhif legacy lane in tax year 2009 "
                "under HCH-VER-20031205-A."
            ),
            "format": "plain_text",
        },
    }

    response = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "unsupported_health_version_window"
    assert detail["reason_code"] == "unsupported_health_version_window"


def test_supported_knowledge_authority_prompt_resolves_to_governed_knowledge_route_selection() -> (
    None
):
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-knowledge-route-001",
        TRACE_ID_HEADER_NAME: "trace-orch-knowledge-route-001",
    }
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-knowledge-route-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "lookup statutory authority for allowable deductions in income tax "
                "effective 2024-12-27."
            ),
            "format": "plain_text",
        },
    }

    first = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)
    second = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["intent_class"] == "lookup_grounded_knowledge"
    assert first_payload["tax_domain_hint"] == "income_tax"
    assert first_payload["plan"]["planning_mode"] == "single_step"
    assert first_payload["plan"]["execution_ready"] is True
    assert first_payload["selected_route"] == {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }
    assert first_payload["decision_id"] == second_payload["decision_id"]


def test_supported_paye_authority_prompt_resolves_to_plan_only_governed_plan() -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-plan-only-001",
        TRACE_ID_HEADER_NAME: "trace-orch-plan-only-001",
    }
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-plan-only-001",
        "channel": "chat",
        "prompt": {
            "text": "lookup statutory authority for paye withholding bands in paye.",
            "format": "plain_text",
        },
    }

    response = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["gate_status"] == "allowed"
    assert body["intent_class"] == "lookup_grounded_knowledge"
    assert body["tax_domain_hint"] == "paye_generalized"
    assert body["plan"]["planning_mode"] == "single_step"
    assert body["plan"]["execution_ready"] is True
    assert body["selected_route"] == {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }


def test_ambiguous_prompt_returns_clarification_required_outcome() -> None:
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-clarify-001",
        TRACE_ID_HEADER_NAME: "trace-orch-clarify-001",
    }
    payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-clarify-001",
        "channel": "chat",
        "prompt": {
            "text": "generate form and report for income tax 2023.",
            "format": "plain_text",
        },
    }

    response = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification_required"
    assert body["gate_status"] == "clarification_required"
    assert body["selected_route"] is None
    assert body["plan"]["plan_status"] == "clarification_required"
    assert body["plan"]["planning_mode"] == "clarification_required"
    assert body["plan"]["execution_ready"] is False
    assert body["clarification"]["reason_code"] == "ambiguous_service_family"
    assert body["clarification"]["candidate_service_families"] == ["forms", "reports"]


def test_unsupported_knowledge_domain_prompt_fails_closed_canonically() -> None:
    fixture = _load_fixture("knowledge_lookup_unsupported_scope_rejected.json")
    client = TestClient(create_app())
    headers = {
        CORRELATION_ID_HEADER_NAME: "corr-orch-knowledge-route-002",
        TRACE_ID_HEADER_NAME: "trace-orch-knowledge-route-002",
    }
    payload = cast(dict[str, object], fixture["prompt_payload"])

    response = client.post("/v1/orchestration/prompt/decide", headers=headers, json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["intent_class"] == "lookup_grounded_knowledge"
    assert body["tax_domain_hint"] == "vat"
    assert body["selected_route"] == {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }
    assert body["clarification"] is None


def _load_fixture(filename: str) -> dict[str, object]:
    loaded = json.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _normalized_error_detail(detail: dict[str, object], status_code: int) -> dict[str, object]:
    return {
        "status_code": status_code,
        "error_code": detail["error_code"],
        "reason": detail["reason"],
        "reason_code": detail["reason_code"],
    }
