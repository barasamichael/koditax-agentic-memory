"""Governed orchestration integration tests for knowledge lookup routing."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path
from datetime import date

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord

HEADERS = {
    "X-Correlation-ID": "corr-phase13-knowledge-orch-001",
    "X-Trace-ID": "trace-phase13-knowledge-orch-001",
}
_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


class _StubKnowledgeRepository:
    def __init__(self, *, customer_lineage: bool = False) -> None:
        self._customer_lineage = customer_lineage
        self._record = KnowledgeSearchRecord(
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
        )

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        assert query
        assert source_type in {None, "tax_law"}
        assert tax_domain in {None, "income_tax"}
        _ = effective_date
        return (self._record,)

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        if self._record.source_id in source_ids and self._record.anchor_id in anchor_ids:
            return (self._record,)
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
        _ = (publication_state, source_family_id, limit, offset, sort_by, sort_order)
        if (
            source_id != self._record.source_id
            or tax_domain != "income_tax"
            or source_class != "tax_law"
        ):
            return ()
        return (
            KnowledgeSourceVersionSummaryRecord(
                source_version_id="123e4567-e89b-12d3-a456-426614174100",
                source_id=self._record.source_id,
                source_family_id="KNW-ITA-FAMILY",
                title=self._record.title,
                source_class=self._record.source_type,
                tax_domain=self._record.tax_domain,
                authority_level=self._record.authority_level,
                publication_state="published",
                source_input_origin=(
                    "customer_uploaded_document"
                    if self._customer_lineage
                    else "official_source_upload"
                ),
                source_version_form="point_in_time_consolidation",
                effective_from=self._record.effective_from,
                effective_to=self._record.effective_to,
                tax_year=self._record.tax_year,
                supersedes_source_version_id=None,
                superseded_by_source_version_id=None,
            ),
        )


def test_supported_authority_lookup_routes_to_grounded_knowledge_deterministically() -> None:
    fixture = _load_fixture("knowledge_lookup_grounded_explanation_success.json")
    client = TestClient(create_app(knowledge_repository=_StubKnowledgeRepository()))
    prompt_payload = cast(dict[str, object], fixture["prompt_payload"])
    execution_context = cast(dict[str, object], fixture["execution_context"])
    prompt_text = cast(dict[str, object], prompt_payload["prompt"])["text"]

    first = _execute_prompt(
        client=client,
        prompt_text=str(prompt_text),
        idempotency_key=str(execution_context["idempotency_key"]),
    )
    second = _execute_prompt(
        client=client,
        prompt_text=str(prompt_text),
        idempotency_key=str(execution_context["idempotency_key"]),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    body = _json(first)
    assert body["selected_route"] == {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
    }
    assert body["grounding_status"] == "grounded"
    assert body["explanation_status"] == "grounded"
    authority_summary = cast(dict[str, object], body["authority_summary"])
    assert isinstance(authority_summary["citation_count"], int)
    assert authority_summary["citation_count"] >= 1
    assert cast(dict[str, object], body["temporal_applicability"])["scope"] == "current-effective"
    citations = cast(list[dict[str, object]], body["citations"])
    assert citations
    assert all(citation["source_type"] == "tax_law" for citation in citations)
    mapped_result = cast(dict[str, object], body["mapped_result"])
    final_outcome = cast(dict[str, object], body["final_outcome"])
    assert mapped_result["action_status"] == "accepted"
    assert final_outcome["outcome_status"] == "success"
    evidence = cast(list[dict[str, object]], body["grounded_evidence"])
    assert evidence[0]["source_id"] == "KNW-ITA-15-2"
    assert evidence[0]["source_version_id"] == "123e4567-e89b-12d3-a456-426614174100"
    assert evidence[0]["anchor_id"] == "income-tax-act-15-2"
    assert body["explanation_status"] == "grounded"
    explanation_items = cast(list[dict[str, object]], body["explanation_items"])
    citations = cast(list[dict[str, object]], body["citations"])
    authority_summary = cast(dict[str, object], body["authority_summary"])
    temporal_applicability = cast(dict[str, object], body["temporal_applicability"])
    assert explanation_items[0]["source_version_id"] == "123e4567-e89b-12d3-a456-426614174100"
    assert citations[0]["source_id"] == "KNW-ITA-15-2"
    assert citations[0]["citation_index"] == 1
    assert authority_summary["highest_authority_level"] == "statute"
    assert temporal_applicability["scope"] == "current-effective"
    final_outcome_trace = cast(dict[str, object], final_outcome["trace"])
    lineage_refs = cast(dict[str, object], final_outcome_trace["lineage_refs"])
    assert lineage_refs["source_id"] == "KNW-ITA-15-2"
    assert lineage_refs["source_version_id"] == "123e4567-e89b-12d3-a456-426614174100"
    assert lineage_refs["anchor_id"] == "income-tax-act-15-2"
    assert lineage_refs["explanation_status"] == "grounded"


def test_supported_direct_grounded_retrieval_routes_deterministically() -> None:
    fixture = _load_fixture("knowledge_retrieve_grounded_explanation_success.json")
    client = TestClient(create_app(knowledge_repository=_StubKnowledgeRepository()))
    prompt_payload = cast(dict[str, object], fixture["prompt_payload"])
    prompt_text = cast(dict[str, object], prompt_payload["prompt"])["text"]

    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-knw-orch-001",
        "channel": "chat",
        "prompt": {
            "text": str(prompt_text),
            "format": "plain_text",
        },
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 400
    detail = _detail(_json(decide))
    assert detail["error_code"] == "unsupported_prompt_scope"
    assert detail["reason"] == "unsupported_domain"
    assert detail["reason_code"] == "unsupported_domain"


def test_invalid_direct_retrieval_identifier_fails_closed_canonically() -> None:
    fixture = _load_fixture("knowledge_retrieve_invalid_identifier_rejected.json")
    client = TestClient(create_app(knowledge_repository=_StubKnowledgeRepository()))
    prompt_payload = cast(dict[str, object], fixture["prompt_payload"])
    prompt_text = cast(dict[str, object], prompt_payload["prompt"])["text"]

    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-knw-orch-001",
        "channel": "chat",
        "prompt": {
            "text": str(prompt_text),
            "format": "plain_text",
        },
    }
    first = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    second = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)

    assert first.status_code == 400
    assert second.status_code == 400
    first_detail = _detail(_json(first))
    second_detail = _detail(_json(second))
    assert first_detail["error_code"] == "unsupported_prompt_scope"
    assert first_detail["reason"] == "unsupported_domain"
    assert first_detail["reason_code"] == "unsupported_domain"
    assert first_detail == second_detail


def test_customer_document_lineage_cannot_surface_through_orchestration_grounding() -> None:
    client = TestClient(
        create_app(knowledge_repository=_StubKnowledgeRepository(customer_lineage=True))
    )
    prompt_text = (
        "lookup statutory authority for allowable deductions in income tax effective 2024-12-27."
    )

    response = _execute_prompt(
        client=client,
        prompt_text=prompt_text,
        idempotency_key="idem-knw-orch-004",
    )

    assert response.status_code == 200
    body = _json(response)
    assert body["grounding_status"] == "grounded"
    evidence = cast(list[dict[str, object]], body["grounded_evidence"])
    assert all(item["source_type"] != "customer_uploaded_document" for item in evidence)


def _execute_prompt(
    *,
    client: TestClient,
    prompt_text: str,
    idempotency_key: str,
) -> Any:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-knw-orch-001",
        "channel": "chat",
        "prompt": {
            "text": prompt_text,
            "format": "plain_text",
        },
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decision_body = _json(decide)
    execute_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "user_id": "user_knw_orch_001",
        "conversation_id": "conv-knw-orch-001",
        "channel": "chat",
        "prompt": {
            "text": prompt_text,
            "format": "plain_text",
        },
        "idempotency_key": idempotency_key,
        "intent_class": decision_body["intent_class"],
        "tax_domain_hint": decision_body["tax_domain_hint"],
        "decision_id": decision_body["decision_id"],
        "selected_route": decision_body["selected_route"],
    }
    return client.post("/v1/orchestration/prompt/execute", headers=HEADERS, json=execute_payload)


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _load_fixture(filename: str) -> dict[str, object]:
    loaded = json.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)
