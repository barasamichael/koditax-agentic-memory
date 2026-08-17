"""Smoke tests for orchestration runtime boundary."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.orchestration_eval_harness import load_orchestration_eval_cases
from tests.orchestration_auth_support import orchestration_auth_headers

_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


class _KnowledgeOrchestrationStub:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return (
            KnowledgeSearchRecord(
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
            ),
        )

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (source_ids, anchor_ids)
        return ()

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
        return (
            KnowledgeSourceVersionSummaryRecord(
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
            ),
        )


def test_orchestration_app_boots_and_health_routes_are_available() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)

    with TestClient(app) as client:
        health = client.get("/healthz", headers={"X-Correlation-ID": "orch-health"})
        ready = client.get("/readyz", headers={"X-Correlation-ID": "orch-ready"})

    health_payload = _json(health)
    ready_payload = _json(ready)
    assert health.status_code == 200
    assert ready.status_code == 200
    assert health_payload["status"] == "ok"
    assert ready_payload["status"] == "ready"
    assert health_payload["service"] == "orchestration"
    assert ready_payload["service"] == "orchestration"
    assert ready_payload["response_synthesis_enabled"] is True
    assert ready_payload["conversation_continuity_enabled"] is True
    assert ready_payload["release_gate_surface"] == "internal_helper_only"


def test_orchestration_execute_endpoint_returns_deterministic_rejection_for_invalid_input() -> None:
    app = create_app()
    payload = {"prompt_text": "invalid prompt shape"}
    with TestClient(app) as client:
        first = client.post(
            "/v1/orchestration/income-tax/execute",
            json=payload,
            headers={"X-Correlation-ID": "orch-invalid"},
        )
        second = client.post(
            "/v1/orchestration/income-tax/execute",
            json=payload,
            headers={"X-Correlation-ID": "orch-invalid"},
        )

    first_detail = _detail(_json(first))
    second_detail = _detail(_json(second))
    assert first.status_code == 404
    assert second.status_code == 404
    assert first_detail["error_code"] == second_detail["error_code"]
    assert first_detail["reason"] == second_detail["reason"]
    assert first_detail["reason_code"] == second_detail["reason_code"]
    assert set(first_detail) == set(second_detail)


def test_orchestration_prompt_execute_supports_grounded_knowledge_lookup() -> None:
    fixture = _load_fixture("knowledge_lookup_grounded_explanation_success.json")
    app = create_app(knowledge_repository=_KnowledgeOrchestrationStub())
    decide_payload = cast(dict[str, object], fixture["prompt_payload"])
    with TestClient(
        app,
        headers=orchestration_auth_headers(user_reference="orch-knowledge"),
    ) as client:
        decide = client.post(
            "/v1/orchestration/prompt/decide",
            json=decide_payload,
            headers={"X-Correlation-ID": "orch-knowledge-decide"},
        )
        decide_payload_body = _json(decide)
        execute = client.post(
            "/v1/orchestration/prompt/execute",
            json={
                **decide_payload,
                "idempotency_key": "idem-orch-knowledge-smoke",
                "intent_class": decide_payload_body["intent_class"],
                "tax_domain_hint": decide_payload_body["tax_domain_hint"],
                "decision_id": decide_payload_body["decision_id"],
                "selected_route": decide_payload_body["selected_route"],
            },
            headers={"X-Correlation-ID": "orch-knowledge-execute"},
        )

    execute_payload = _json(execute)
    assert decide.status_code == 200
    assert execute.status_code == 200
    expected = cast(dict[str, object], fixture["expected"])
    expected_grounded_evidence = cast(list[dict[str, object]], expected["grounded_evidence"])
    expected_explanation_items = cast(list[dict[str, object]], expected["explanation_items"])
    expected_citations = cast(list[dict[str, object]], expected["citations"])
    assert execute_payload["selected_route"] == expected["selected_route"]
    assert execute_payload["grounding_status"] == expected["grounding_status"]
    assert execute_payload["explanation_status"] == expected["explanation_status"]
    assert "response" in execute_payload
    plan = cast(dict[str, object], execute_payload["plan"])
    assert plan["plan_status"] == "planned"
    mapped_result = cast(dict[str, object], execute_payload["mapped_result"])
    assert mapped_result["action_status"] == "accepted"
    grounded_evidence = cast(list[dict[str, object]], execute_payload["grounded_evidence"])
    assert grounded_evidence
    assert grounded_evidence[0]["source_id"] == expected_grounded_evidence[0]["source_id"]
    assert grounded_evidence[0]["title"] == expected_grounded_evidence[0]["title"]
    assert grounded_evidence[0]["url"] == expected_grounded_evidence[0]["url"]
    explanation_items = cast(list[dict[str, object]], execute_payload["explanation_items"])
    assert explanation_items
    assert explanation_items[0]["source_id"] == expected_explanation_items[0]["source_id"]
    assert explanation_items[0]["authority_level"] == expected_explanation_items[0][
        "authority_level"
    ]
    citations = cast(list[dict[str, object]], execute_payload["citations"])
    assert citations
    assert citations[0]["source_id"] == expected_citations[0]["source_id"]
    assert citations[0]["title"] == expected_citations[0]["title"]


def test_orchestration_prompt_execute_supports_compute_plus_grounding_multi_step() -> None:
    app = create_app(knowledge_repository=_KnowledgeOrchestrationStub())
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-multi-step-smoke-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 under "
                "KIT-VER-20230701-A with legal basis."
            ),
            "format": "plain_text",
        },
    }
    with TestClient(
        app,
        headers=orchestration_auth_headers(user_reference="orch-multi-step"),
    ) as client:
        decide = client.post(
            "/v1/orchestration/prompt/decide",
            json=decide_payload,
            headers={"X-Correlation-ID": "orch-multi-step-decide"},
        )
        decide_body = _json(decide)
        execute = client.post(
            "/v1/orchestration/prompt/execute",
            json={
                **decide_payload,
                "idempotency_key": "idem-orch-multi-step-smoke",
                "intent_class": decide_body["intent_class"],
                "tax_domain_hint": decide_body["tax_domain_hint"],
                "decision_id": decide_body["decision_id"],
                "selected_route": decide_body["selected_route"],
            },
            headers={"X-Correlation-ID": "orch-multi-step-execute"},
        )

    execute_payload = _json(execute)
    assert decide.status_code == 200
    assert execute.status_code == 409
    detail = _detail(execute_payload)
    assert detail["error_code"] == "clarification_required"
    assert detail["reason_code"] == "clarification_required"
    context = cast(dict[str, object], detail["context"])
    assert "income" in cast(list[str], context["required_context_fields"])


def test_orchestration_prompt_decide_supports_same_conversation_followup_resolution() -> None:
    store = InMemoryConversationStateStore()
    app = create_app(conversation_state_store=store)
    with TestClient(
        app,
        headers=orchestration_auth_headers(user_reference="orch-followup-smoke"),
    ) as client:
        initial_payload = {
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": "conv-followup-smoke-001",
            "channel": "chat",
            "prompt": {
                "text": (
                    "compute income tax for resident employment lane in tax year 2021 "
                    "under KIT-VER-20210101-A."
                ),
                "format": "plain_text",
            },
        }
        initial_decide = client.post(
            "/v1/orchestration/prompt/decide",
            json=initial_payload,
            headers={"X-Correlation-ID": "orch-followup-smoke-seed-decide"},
        )
        initial_decision = _json(initial_decide)
        initial_execute = client.post(
            "/v1/orchestration/prompt/execute",
            json={
                **initial_payload,
                "idempotency_key": "idem-followup-smoke-seed-001",
                "intent_class": initial_decision["intent_class"],
                "tax_domain_hint": initial_decision["tax_domain_hint"],
                "decision_id": initial_decision["decision_id"],
                "selected_route": initial_decision["selected_route"],
            },
            headers={"X-Correlation-ID": "orch-followup-smoke-seed-execute"},
        )
        followup_decide = client.post(
            "/v1/orchestration/prompt/decide",
            json={
                "tenant_id": "pilot_tenant_alpha",
                "conversation_id": "conv-followup-smoke-001",
                "channel": "chat",
                "prompt": {
                    "text": "what about 2023?",
                    "format": "plain_text",
                },
            },
            headers={"X-Correlation-ID": "orch-followup-smoke-decide"},
        )

    assert initial_execute.status_code == 409
    initial_detail = _detail(_json(initial_execute))
    assert initial_detail["error_code"] == "clarification_required"
    assert "income" in cast(list[str], cast(dict[str, object], initial_detail["context"])["required_context_fields"])
    followup_body = _json(followup_decide)
    assert followup_decide.status_code == 400
    followup_detail = cast(dict[str, object], followup_body["detail"])
    assert followup_detail["error_code"] == "off_topic_prompt"
    assert followup_detail["reason_code"] == "off_topic_prompt"


def test_orchestration_eval_fixture_dirs_are_available_to_runtime_regression_checks() -> None:
    assert len(load_orchestration_eval_cases("golden")) >= 8
    assert len(load_orchestration_eval_cases("adversarial")) >= 9


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_dict = cast(dict[str, object], detail)
    expected_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "correlation_id",
        "trace_id",
    }
    assert expected_fields.issubset(detail_dict.keys())
    return detail_dict


def _load_fixture(filename: str) -> dict[str, object]:
    loaded = json.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)
