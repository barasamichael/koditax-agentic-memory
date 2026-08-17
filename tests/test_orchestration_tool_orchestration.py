"""Deterministic orchestration route execution tests for tool-call adapter wiring."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Iterator
import json
from typing import cast
import hashlib
from pathlib import Path
from datetime import date

import pytest
from fastapi.testclient import TestClient

from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.orchestration.app.main import create_app
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionInput
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_response_generator import LLMResponseStreamEvent
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from tests.orchestration_auth_support import orchestration_auth_headers

HEADERS = {
    CORRELATION_ID_HEADER_NAME: "corr-orchestration-exec-001",
    TRACE_ID_HEADER_NAME: "trace-orchestration-exec-001",
}
_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


class _ToolRouteTurnResolver:
    """Static semantic boundary for adapter-contract tests.

    Route execution is deterministic once a turn has been resolved.  These
    tests deliberately inject complete canonical resolutions so they test the
    planner, validator and adapters without coupling their contract to a live
    model's phrasing.  OpenAI resolver behaviour is covered separately.
    """

    _RESOLUTIONS: dict[str, dict[str, object]] = {
        "compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A.": {
            "intent_class": "compute_income_tax", "tax_domain_hint": "income_tax",
            "tax_year_hint": 2023, "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A", "operation_mode": "computation",
            "needs_computation": True,
        },
        "compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A with legal basis.": {
            "intent_class": "compute_plus_grounding", "tax_domain_hint": "income_tax",
            "tax_year_hint": 2023, "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A", "operation_mode": "computation",
            "needs_computation": True, "needs_knowledge_retrieval": True,
        },
        "generate form for income tax return preparation.": {
            "intent_class": "generate_form_artifact", "tax_domain_hint": "income_tax",
            "operation_mode": "artifact", "needs_artifact_operation": True,
        },
        "generate report for income tax audit trail.": {
            "intent_class": "generate_report_artifact", "tax_domain_hint": "income_tax",
            "operation_mode": "artifact", "needs_artifact_operation": True,
        },
        "generate form for health contribution filing.": {
            "intent_class": "generate_form_artifact", "tax_domain_hint": "health_contribution",
            "operation_mode": "artifact", "needs_artifact_operation": True,
        },
        "generate report for health contribution audit trail.": {
            "intent_class": "generate_report_artifact", "tax_domain_hint": "health_contribution",
            "operation_mode": "artifact", "needs_artifact_operation": True,
        },
        "extract document for income tax filing support.": {
            "intent_class": "extract_document", "tax_domain_hint": "income_tax",
            "operation_mode": "artifact", "needs_artifact_operation": True,
        },
        "compute health contribution for sha/shif salaried lane in tax year 2024 under HCH-VER-20241001-A.": {
            "intent_class": "compute_health_contribution", "tax_domain_hint": "health_contribution",
            "tax_year_hint": 2024, "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
            "historical_version_id": "HCH-VER-20241001-A", "regime_identifier": "sha_shif",
            "operation_mode": "computation", "needs_computation": True,
        },
        "compute health contribution for transition boundary sha lane in tax year 2024 under HCH-VER-20241001-A.": {
            "intent_class": "compute_health_contribution", "tax_domain_hint": "health_contribution",
            "tax_year_hint": 2024, "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
            "historical_version_id": "HCH-VER-20241001-A", "regime_identifier": "transition_boundary",
            "operation_mode": "computation", "needs_computation": True,
        },
        "lookup statutory authority for paye withholding bands in paye.": {
            "intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "paye_generalized",
            "operation_mode": "informational", "needs_knowledge_retrieval": True,
            "retrieval_tax_domain_filter": "paye_generalized",
        },
        "lookup statutory authority for allowable deductions in income tax effective 2024-12-27.": {
            "intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "income_tax",
            "operation_mode": "informational", "needs_knowledge_retrieval": True,
            "retrieval_tax_domain_filter": "income_tax",
        },
    }

    def __init__(self) -> None:
        self.calls = 0

    def resolve_turn(self, payload: ConversationTurnResolutionInput) -> ConversationTurnResolution:
        self.calls += 1
        resolved = self._RESOLUTIONS.get(payload.current_prompt)
        if resolved is None:
            raise AssertionError(f"unregistered tool-route test prompt: {payload.current_prompt!r}")
        values: dict[str, object] = {
            "schema_version": "1.0", "relationship": "standalone",
            "operation_mode": "informational", "raw_prompt": payload.current_prompt,
            "contextualized_prompt": payload.current_prompt,
            "intent_class": "lookup_grounded_knowledge", "tax_domain_hint": "general_tax",
            "retrieval_tax_domain_filter": None, "jurisdiction_hint": "Kenya", "tax_year_hint": None,
            "supported_lane_id": None, "historical_version_id": None, "regime_identifier": None,
            "answerability": "answerable", "clarification_reason_code": None,
            "clarification_question": None, "candidate_service_families": [],
            "required_context_fields": [], "needs_knowledge_retrieval": False,
            "needs_computation": False, "needs_external_action": False,
            "needs_artifact_operation": False, "referenced_candidate_ids": [],
            "resolved_references": [], "retained_fields": [], "corrected_fields": [],
            "reuse_prior_semantic_facts": False, "reuse_prior_computation_result": False,
            "reuse_prior_evidence": False, "reuse_prior_artifact": False, "assumptions": [],
            "confidence": 1.0, "audit_summary": "static tool-route test resolution",
            "provided_context_fields": [], "missing_required_context_fields": [],
        }
        values.update(resolved)
        return ConversationTurnResolution.model_validate(values)


class _ToolRouteResponseGenerator:
    """Deterministic synthesis boundary for adapter-contract tests."""

    def generate(self, context: GovernedSynthesisContext) -> UnifiedAnswerResponseModel:
        return UnifiedAnswerResponseModel(
            status="generated",
            answer_text="The governed route result is available.",
            answer_mode=context["answer_mode"],
        )

    def stream_generate(
        self, context: GovernedSynthesisContext
    ) -> Iterator[LLMResponseStreamEvent]:
        _ = context
        return iter(())


def _create_tool_route_app(*, knowledge_repository: _KnowledgeRouteStub | None = None):
    return create_app(
        knowledge_repository=knowledge_repository,
        turn_resolver=_ToolRouteTurnResolver(),
        llm_response_generator=_ToolRouteResponseGenerator(),
    )


class _KnowledgeRouteStub:
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


def _base_payload() -> dict[str, object]:
    return {
        "tenant_id": "pilot_tenant_alpha",
        "user_id": "user_alpha_001",
        "conversation_id": "conv-exec-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 "
                "under KIT-VER-20230701-A."
            ),
            "format": "plain_text",
        },
        "idempotency_key": "idem-exec-001",
    }


def _health_base_payload() -> dict[str, object]:
    return {
        "tenant_id": "pilot_tenant_alpha",
        "user_id": "user_health_001",
        "conversation_id": "conv-health-exec-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute health contribution for sha/shif salaried lane in tax year 2024 "
                "under HCH-VER-20241001-A."
            ),
            "format": "plain_text",
        },
        "idempotency_key": "idem-health-exec-001",
    }


def _execute_payload_for_route(
    *,
    route_id: str,
    target_service: str,
    target_operation: str,
) -> dict[str, object]:
    conversation_id = f"conv-{route_id}-exec"
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-exec")},
    )
    base = _base_payload()
    base["conversation_id"] = conversation_id
    base["idempotency_key"] = f"idem-{route_id}-{conversation_id}"
    prompt_text_by_route = {
        "income_tax_compute_route_v1": (
            "compute income tax for resident employment lane in tax year 2023 "
            "under KIT-VER-20230701-A."
        ),
        "income_tax_form_generation_route_v1": "generate form for income tax return preparation.",
        "income_tax_report_generation_route_v1": "generate report for income tax audit trail.",
            "income_tax_document_evidence_route_v1": (
                "extract document for income tax filing support."
        ),
    }
    cast(dict[str, object], base["prompt"])["text"] = prompt_text_by_route[route_id]
    decide_payload = {
        "tenant_id": base["tenant_id"],
        "conversation_id": conversation_id,
        "channel": base["channel"],
        "prompt": deepcopy(base["prompt"]),
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decide_body = decide.json()
    assert decide_body["selected_route"] == {
        "route_id": route_id,
        "target_service": target_service,
        "target_operation": target_operation,
    }
    return {
        **base,
        "intent_class": decide_body["intent_class"],
        "tax_domain_hint": decide_body["tax_domain_hint"],
        "decision_id": decide_body["decision_id"],
        "selected_route": decide_body["selected_route"],
    }


def _health_execute_payload_for_route(
    *,
    route_id: str,
    target_service: str,
    target_operation: str,
    prompt_text: str | None = None,
) -> dict[str, object]:
    conversation_id = f"conv-{route_id}-health-exec"
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-health")},
    )
    base = _health_base_payload()
    if prompt_text is None:
        prompt_text_by_route = {
            "health_contribution_compute_route_v1": (
                "compute health contribution for sha/shif salaried lane in tax year 2024 "
                "under HCH-VER-20241001-A."
            ),
            "health_contribution_form_mapping_route_v1": (
                "generate form for health contribution filing."
            ),
            "health_contribution_report_generation_route_v1": (
                "generate report for health contribution audit trail."
            ),
        }
        prompt_text = prompt_text_by_route[route_id]
    prompt = cast(dict[str, object], base["prompt"])
    prompt["text"] = prompt_text
    prompt_fingerprint = hashlib.sha256(str(prompt_text).encode("utf-8")).hexdigest()[:12]
    base["conversation_id"] = conversation_id
    base["idempotency_key"] = f"idem-{route_id}-{conversation_id}-{prompt_fingerprint}"
    decide_payload = {
        "tenant_id": base["tenant_id"],
        "conversation_id": conversation_id,
        "channel": base["channel"],
        "prompt": deepcopy(base["prompt"]),
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decide_body = decide.json()
    assert decide_body["selected_route"] == {
        "route_id": route_id,
        "target_service": target_service,
        "target_operation": target_operation,
    }
    return {
        **base,
        "intent_class": decide_body["intent_class"],
        "tax_domain_hint": decide_body["tax_domain_hint"],
        "decision_id": decide_body["decision_id"],
        "selected_route": decide_body["selected_route"],
    }


@pytest.mark.parametrize(
    (
        "route_id",
        "target_service",
        "target_operation",
        "expected_adapter_name",
        "expected_action_status",
    ),
    [
        (
            "income_tax_compute_route_v1",
            "tax_core",
            "execute_computation",
            "deterministic_tax_core_adapter_v1",
            "pending",
        ),
        (
            "income_tax_form_generation_route_v1",
            "forms",
            "generate_income_tax_form_artifact",
            "deterministic_forms_adapter_v1",
            "accepted",
        ),
        (
            "income_tax_report_generation_route_v1",
            "reports",
            "create_income_tax_report_artifact",
            "deterministic_reports_adapter_v1",
            "accepted",
        ),
    ],
)
def test_route_execution_dispatches_to_expected_adapter_deterministically(
    route_id: str,
    target_service: str,
    target_operation: str,
    expected_adapter_name: str,
    expected_action_status: str,
) -> None:
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-exec")},
    )
    payload = _execute_payload_for_route(
        route_id=route_id,
        target_service=target_service,
        target_operation=target_operation,
    )

    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["status"] == "executed"
    assert first_body["execution_status"] == "resolved"
    assert first_body["selected_route"] == payload["selected_route"]
    assert first_body["mapped_result"]["action_status"] == expected_action_status
    assert first_body["adapter_response"]["trace"]["adapter_name"] == expected_adapter_name
    assert first_body["adapter_response"]["trace"]["target_service"] == target_service
    assert first_body["adapter_response"]["trace"]["target_operation"] == target_operation
    assert first_body["adapter_response"]["trace"]["idempotency_key"] == payload["idempotency_key"]
    result_payload = first_body["adapter_response"].get("result_payload")
    if route_id == "income_tax_compute_route_v1":
        assert result_payload is None
        assert first_body["validation"] is None
    else:
        assert isinstance(result_payload, dict)
    if route_id == "income_tax_form_generation_route_v1":
        assert result_payload["artifact_id"]
        validation = cast(dict[str, object], first_body["validation"])
        assert validation["validation_status"] == "accepted"
        assert validation["workflow"] == "orchestration_forms_result"
    elif route_id == "income_tax_report_generation_route_v1":
        assert result_payload["report_id"]
        validation = cast(dict[str, object], first_body["validation"])
        assert validation["validation_status"] == "accepted"
        assert validation["workflow"] == "orchestration_reports_result"
    assert first_body == second_body


def test_document_evidence_route_fails_closed_when_its_service_is_unconfigured() -> None:
    """A document route must not fabricate evidence without its service."""
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-exec")},
    )
    payload = _execute_payload_for_route(
        route_id="income_tax_document_evidence_route_v1",
        target_service="document_ai",
        target_operation="search_document_evidence",
    )

    response = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error_code"] == "unsupported_orchestration_route"
    assert detail["reason_code"] == "document_ai_integration_unconfigured"


def test_unsupported_route_is_rejected_with_canonical_error_envelope() -> None:
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-exec")},
    )
    payload = _execute_payload_for_route(
        route_id="income_tax_compute_route_v1",
        target_service="tax_core",
        target_operation="execute_computation",
    )
    payload["selected_route"] = {
        "route_id": "income_tax_unsupported_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_unsupported_knowledge",
    }
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 403
    assert second.status_code == 403
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "unsafe_action_path"
    assert first_detail["reason"] == "unsafe_route_override"
    assert first_detail["reason_code"] == "unsafe_route_override"
    assert set(first_detail.keys()) == set(second_detail.keys())
    assert first_detail == second_detail


def test_missing_idempotency_key_is_rejected_with_canonical_error_envelope() -> None:
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-exec")},
    )
    payload = _execute_payload_for_route(
        route_id="income_tax_compute_route_v1",
        target_service="tax_core",
        target_operation="execute_computation",
    )
    payload["idempotency_key"] = "   "
    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = first.json()["detail"]
    second_detail = second.json()["detail"]
    assert first_detail["error_code"] == "invalid_orchestration_request"
    assert first_detail["reason"] == "invalid_orchestration_request"
    assert first_detail["reason_code"] == "invalid_orchestration_request"
    assert set(first_detail.keys()) == set(second_detail.keys())
    assert first_detail == second_detail


@pytest.mark.parametrize(
    (
        "route_id",
        "target_service",
        "target_operation",
        "expected_adapter_name",
        "expected_action_status",
    ),
    [
        (
            "health_contribution_compute_route_v1",
            "tax_core",
            "execute_computation",
            "deterministic_tax_core_adapter_v1",
            "pending",
        ),
        (
            "health_contribution_form_mapping_route_v1",
            "forms",
            "map_health_contribution_output_to_form_ready",
            "deterministic_forms_adapter_v1",
            "accepted",
        ),
        (
            "health_contribution_report_generation_route_v1",
            "reports",
            "create_health_contribution_report_artifact",
            "deterministic_reports_adapter_v1",
            "accepted",
        ),
    ],
)
def test_health_route_execution_dispatches_to_expected_adapter_deterministically(
    route_id: str,
    target_service: str,
    target_operation: str,
    expected_adapter_name: str,
    expected_action_status: str,
) -> None:
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-health")},
    )
    payload = _health_execute_payload_for_route(
        route_id=route_id,
        target_service=target_service,
        target_operation=target_operation,
    )

    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["status"] == "executed"
    assert first_body["tax_domain_hint"] == "health_contribution"
    assert first_body["selected_route"] == payload["selected_route"]
    assert first_body["mapped_result"]["action_status"] == expected_action_status
    assert first_body["adapter_response"]["trace"]["adapter_name"] == expected_adapter_name
    lineage_refs = first_body["final_outcome"]["trace"]["lineage_refs"]
    assert lineage_refs["tax_domain_hint"] == "health_contribution"
    if route_id == "health_contribution_compute_route_v1":
        assert first_body["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"
        assert first_body["historical_version_id"] == "HCH-VER-20241001-A"
        assert first_body["regime_identifier"] == "sha_shif"
        assert lineage_refs["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"
        assert lineage_refs["historical_version_id"] == "HCH-VER-20241001-A"
        assert lineage_refs["regime_identifier"] == "sha_shif"
        assert first_body["validation"] is None
    else:
        assert first_body["supported_lane_id"] is None
        assert first_body["historical_version_id"] is None
        assert first_body["regime_identifier"] is None
        validation = cast(dict[str, object], first_body["validation"])
        assert validation["validation_status"] == "accepted"
    assert first_body == second_body


def test_health_transition_route_execution_preserves_transition_regime_identity() -> None:
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-health")},
    )
    payload = _health_execute_payload_for_route(
        route_id="health_contribution_compute_route_v1",
        target_service="tax_core",
        target_operation="execute_computation",
        prompt_text=(
            "compute health contribution for transition boundary sha lane in tax year 2024 "
            "under HCH-VER-20241001-A."
        ),
    )

    response = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"
    assert body["historical_version_id"] == "HCH-VER-20241001-A"
    assert body["regime_identifier"] == "transition_boundary"
    lineage_refs = body["final_outcome"]["trace"]["lineage_refs"]
    assert lineage_refs["regime_identifier"] == "transition_boundary"
    assert lineage_refs["historical_version_id"] == "HCH-VER-20241001-A"


def test_knowledge_route_execution_returns_grounded_evidence_deterministically() -> None:
    fixture = _load_fixture("knowledge_lookup_grounded_explanation_success.json")
    client = TestClient(
        _create_tool_route_app(knowledge_repository=_KnowledgeRouteStub()),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-knowledge")},
    )
    decide_payload = cast(dict[str, object], fixture["prompt_payload"])
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decide_body = decide.json()
    execution_context = cast(dict[str, object], fixture["execution_context"])
    payload = {
        "tenant_id": decide_payload["tenant_id"],
        "user_id": execution_context["user_id"],
        "conversation_id": decide_payload["conversation_id"],
        "channel": decide_payload["channel"],
        "prompt": deepcopy(decide_payload["prompt"]),
        "idempotency_key": f"{execution_context['idempotency_key']}-tool",
        "intent_class": decide_body["intent_class"],
        "tax_domain_hint": decide_body["tax_domain_hint"],
        "decision_id": decide_body["decision_id"],
        "selected_route": decide_body["selected_route"],
    }

    first = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)
    second = client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["mapped_result"]["action_status"] == "accepted"
    assert first_body["grounding_status"] == "grounded"
    assert (
        first_body["adapter_response"]["trace"]["adapter_name"]
        == "deterministic_knowledge_adapter_v1"
    )
    assert first_body["selected_route"] == {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }
    expected = cast(dict[str, object], fixture["expected"])
    expected_evidence = cast(list[dict[str, object]], expected["grounded_evidence"])[0]
    assert first_body["grounded_evidence"]
    evidence = first_body["grounded_evidence"][0]
    assert evidence["source_id"] == expected_evidence["source_id"]
    assert evidence["anchor_id"] == expected_evidence["anchor_id"]
    assert evidence["tax_domain"] == "income_tax"
    assert first_body["citations"]
    assert first_body["citations"][0]["source_id"] == evidence["source_id"]
    assert first_body == second_body


def test_supported_paye_knowledge_prompt_executes_instead_of_being_plan_only() -> None:
    client = TestClient(
        _create_tool_route_app(),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-plan")},
    )
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-plan-only-exec-001",
        "channel": "chat",
        "prompt": {
            "text": "lookup statutory authority for paye withholding bands in paye.",
            "format": "plain_text",
        },
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decide_body = decide.json()

    response = client.post(
        "/v1/orchestration/prompt/execute",
        headers=HEADERS,
        json={
            **decide_payload,
            "user_id": "user_plan_only_001",
            "idempotency_key": "idem-plan-only-001",
            "intent_class": decide_body["intent_class"],
            "tax_domain_hint": decide_body["tax_domain_hint"],
            "decision_id": decide_body["decision_id"],
            "selected_route": decide_body["selected_route"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["selected_route"] == {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }
    assert body["mapped_result"]["action_status"] == "accepted"


def test_compute_plus_grounding_prompt_executes_as_supported_multi_step_plan() -> None:
    client = TestClient(
        _create_tool_route_app(knowledge_repository=_KnowledgeRouteStub()),
        headers={**HEADERS, **orchestration_auth_headers(user_reference="tool-orch-mixed")},
    )
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-mixed-exec-001",
        "channel": "chat",
        "prompt": {
            "text": (
                "compute income tax for resident employment lane in tax year 2023 under "
                "KIT-VER-20230701-A with legal basis."
            ),
            "format": "plain_text",
        },
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decide_body = decide.json()

    first = client.post(
        "/v1/orchestration/prompt/execute",
        headers=HEADERS,
        json={
            **decide_payload,
            "user_id": "user_mixed_exec_001",
            "idempotency_key": "idem-mixed-exec-001",
            "intent_class": decide_body["intent_class"],
            "tax_domain_hint": decide_body["tax_domain_hint"],
            "decision_id": decide_body["decision_id"],
            "selected_route": decide_body["selected_route"],
        },
    )
    second = client.post(
        "/v1/orchestration/prompt/execute",
        headers=HEADERS,
        json={
            **decide_payload,
            "user_id": "user_mixed_exec_001",
            "idempotency_key": "idem-mixed-exec-001",
            "intent_class": decide_body["intent_class"],
            "tax_domain_hint": decide_body["tax_domain_hint"],
            "decision_id": decide_body["decision_id"],
            "selected_route": decide_body["selected_route"],
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["plan"]["planning_mode"] == "multi_step"
    assert first_body["selected_route"] is None
    assert first_body["mapped_result"]["action_status"] == "pending"
    assert first_body["step_summary"] == {
        "total_steps": 2,
        "resolved_steps": 2,
        "blocked_steps": 0,
        "rejected_steps": 0,
        "pending_steps": 1,
        "accepted_steps": 1,
    }
    assert first_body["step_results"][0]["target_service"] == "tax_core"
    assert first_body["step_results"][1]["target_service"] == "knowledge"
    assert first_body["grounding_status"] == "grounded"
    assert first_body == second_body


def _load_fixture(filename: str) -> dict[str, object]:
    loaded = json.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)
