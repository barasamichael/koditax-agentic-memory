"""Prompt-understanding tests for governed orchestration planning."""

from __future__ import annotations

from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers


def test_income_tax_form_prompt_resolves_to_forms_plan() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-forms-plan-001",
            TRACE_ID_HEADER_NAME: "trace-forms-plan-001",
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-forms-plan-001",
            "channel": "chat",
            "prompt": {
                "text": "generate form for income tax return preparation.",
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["gate_status"] == "allowed"
    assert body["intent_class"] == "generate_form_artifact"
    assert body["selected_route"] == {
        "route_id": "income_tax_form_generation_route_v1",
        "target_service": "forms",
        "target_operation": "generate_income_tax_form_artifact",
    }


def test_income_tax_report_prompt_resolves_to_reports_plan() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-reports-plan-001",
            TRACE_ID_HEADER_NAME: "trace-reports-plan-001",
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-reports-plan-001",
            "channel": "chat",
            "prompt": {
                "text": "generate report for income tax audit trail.",
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["gate_status"] == "allowed"
    assert body["intent_class"] == "generate_report_artifact"
    assert body["selected_route"] == {
        "route_id": "income_tax_report_generation_route_v1",
        "target_service": "reports",
        "target_operation": "create_income_tax_report_artifact",
    }


def test_document_extraction_prompt_resolves_to_document_ai_plan() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-doc-plan-001",
            TRACE_ID_HEADER_NAME: "trace-doc-plan-001",
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-doc-plan-001",
            "channel": "chat",
            "prompt": {
                "text": "extract document for income tax filing support.",
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["intent_class"] == "extract_document"
    assert body["selected_route"] == {
        "route_id": "income_tax_document_evidence_route_v1",
        "target_service": "document_ai",
        "target_operation": "search_document_evidence",
    }


def test_mixed_compute_plus_grounding_prompt_resolves_to_execution_ready_multi_step_plan() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-mixed-plan-001",
            TRACE_ID_HEADER_NAME: "trace-mixed-plan-001",
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-mixed-plan-001",
            "channel": "chat",
            "prompt": {
                "text": (
                    "compute income tax for resident employment lane in tax year 2023 under "
                    "KIT-VER-20230701-A with legal basis."
                ),
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["gate_status"] == "allowed"
    assert body["intent_class"] == "compute_plus_grounding"
    assert body["selected_route"] is None
    assert body["plan"]["planning_mode"] == "multi_step"
    assert body["plan"]["execution_ready"] is True
    assert len(body["plan"]["steps"]) == 2
    assert body["plan"]["steps"][1]["depends_on"] == [body["plan"]["steps"][0]["step_id"]]


def test_natural_language_resident_employee_prompt_resolves_to_supported_plan() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-natural-income-tax-plan-001",
            TRACE_ID_HEADER_NAME: "trace-natural-income-tax-plan-001",
            **orchestration_auth_headers(tenant_id="pilot_tenant_alpha"),
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-natural-income-tax-plan-001",
            "channel": "chat",
            "prompt": {
                "text": (
                    "Calculate income tax for a resident employee for tax year 2023 "
                    "and explain the legal basis."
                ),
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["gate_status"] == "allowed"
    assert body["intent_class"] == "compute_plus_grounding"
    assert body["supported_lane_id"] == "resident_employment_income_2023_07_01"
    assert body["historical_version_id"] == "KIT-VER-20230701-A"


def test_compute_prompt_missing_tax_year_returns_clarification_required() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/orchestration/prompt/decide",
        headers={
            CORRELATION_ID_HEADER_NAME: "corr-clarify-tax-year-001",
            TRACE_ID_HEADER_NAME: "trace-clarify-tax-year-001",
        },
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-clarify-tax-year-001",
            "channel": "chat",
            "prompt": {
                "text": "compute income tax for resident employment lane under KIT-VER-20230701-A.",
                "format": "plain_text",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification_required"
    assert body["gate_status"] == "clarification_required"
    assert body["clarification"]["reason_code"] == "missing_tax_year"
    assert body["clarification"]["required_context_fields"] == ["tax_year"]
