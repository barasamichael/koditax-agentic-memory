"""Deterministic grounded explanation rendering tests for orchestration knowledge output."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path
from datetime import date

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.grounded_explanation_renderer import GroundedExplanationError
from services.orchestration.app.grounded_explanation_renderer import render_grounded_explanation

HEADERS = {
    "X-Correlation-ID": "corr-phase13-grounded-explanation-001",
    "X-Trace-ID": "trace-phase13-grounded-explanation-001",
}
_FIXTURES_DIR = Path("tests/fixtures/orchestration_prompt")


def test_render_grounded_explanation_returns_deterministic_current_effective_payload() -> None:
    payload = render_grounded_explanation(grounded_evidence=[_grounded_evidence_item()])

    assert payload["explanation_status"] == "grounded"
    assert payload["explanation_items"] == [
        {
            "explanation_text": (
                "Income Tax Act (Cap. 470), Section 15(2) provides statute-level "
                "tax_law authority for income_tax. Temporal applicability: current-effective."
            ),
            "source_id": "KNW-ITA-15-2",
            "source_version_id": "123e4567-e89b-12d3-a456-426614174100",
            "anchor_id": "income-tax-act-15-2",
            "authority_level": "statute",
            "source_type": "tax_law",
            "temporal_applicability": "current-effective",
        }
    ]
    assert payload["citations"] == [
        {
            "citation_index": 1,
            "source_id": "KNW-ITA-15-2",
            "source_version_id": "123e4567-e89b-12d3-a456-426614174100",
            "anchor_id": "income-tax-act-15-2",
            "title": "Income Tax Act (Cap. 470), Section 15(2)",
            "url": "https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
            "source_type": "tax_law",
            "authority_level": "statute",
            "tax_domain": "income_tax",
            "temporal_applicability": "current-effective",
            "effective_from": "1974-01-01",
            "effective_to": None,
            "tax_year": None,
        }
    ]
    assert payload["authority_summary"] == {
        "highest_authority_level": "statute",
        "source_types": ["tax_law"],
        "citation_count": 1,
    }
    assert payload["temporal_applicability"] == {
        "scope": "current-effective",
        "effective_from": "1974-01-01",
        "effective_to": None,
        "tax_year": None,
        "disclosure_text": "Grounded explanation is current-effective from 1974-01-01.",
    }


def test_render_grounded_explanation_returns_tax_year_scoped_disclosure() -> None:
    payload = render_grounded_explanation(
        grounded_evidence=[_grounded_evidence_item(tax_year=2024)]
    )

    temporal_applicability = payload["temporal_applicability"]
    assert temporal_applicability["scope"] == "tax-year-scoped"
    assert temporal_applicability["tax_year"] == 2024
    assert (
        temporal_applicability["disclosure_text"]
        == "Grounded explanation is tax-year-scoped for tax year 2024."
    )


def test_render_grounded_explanation_rejects_insufficient_evidence() -> None:
    try:
        render_grounded_explanation(grounded_evidence=[])
    except GroundedExplanationError as error:
        assert error.error_code == "unsupported_prompt_scope"
        assert error.reason == "insufficient_grounded_evidence"
    else:
        raise AssertionError("Expected deterministic grounded explanation failure.")


def test_render_grounded_explanation_rejects_conflicting_top_authority_evidence() -> None:
    grounded_evidence = [
        _grounded_evidence_item(source_version_id="123e4567-e89b-12d3-a456-426614174100"),
        _grounded_evidence_item(
            source_version_id="123e4567-e89b-12d3-a456-426614174101",
            anchor_id="income-tax-act-16-1",
            title="Income Tax Act (Cap. 470), Section 16(1)",
            url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2025-01-01",
        ),
    ]

    try:
        render_grounded_explanation(grounded_evidence=grounded_evidence)
    except GroundedExplanationError as error:
        assert error.error_code == "unsupported_prompt_scope"
        assert error.reason == "conflicting_grounded_evidence"
    else:
        raise AssertionError("Expected conflicting grounded evidence to fail closed.")


def test_orchestration_fails_closed_when_grounded_evidence_is_conflicting() -> None:
    fixture = _load_fixture("knowledge_lookup_conflicting_grounding_rejected.json")
    client = TestClient(create_app(knowledge_repository=_ConflictingKnowledgeRepository()))
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

    expected_error = cast(dict[str, object], fixture["expected_error"])
    assert first.status_code == expected_error["status_code"]
    assert second.status_code == expected_error["status_code"]
    assert _normalized_error_detail(_detail(first), first.status_code) == expected_error
    assert _detail(first) == _detail(second)


def test_orchestration_historical_grounded_explanation_matches_fixture() -> None:
    fixture = _load_fixture("knowledge_lookup_historical_explanation_success.json")
    client = TestClient(create_app(knowledge_repository=_HistoricalKnowledgeRepository()))
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
    assert first.content == second.content
    body = cast(dict[str, object], first.json())
    expected = cast(dict[str, object], fixture["expected"])
    assert _normalized_success_payload(body) == expected


def _grounded_evidence_item(
    *,
    source_id: str = "KNW-ITA-15-2",
    source_version_id: str = "123e4567-e89b-12d3-a456-426614174100",
    anchor_id: str = "income-tax-act-15-2",
    title: str = "Income Tax Act (Cap. 470), Section 15(2)",
    url: str = "https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
    source_type: str = "tax_law",
    authority_level: str = "statute",
    tax_domain: str = "income_tax",
    effective_from: str = "1974-01-01",
    effective_to: str | None = None,
    tax_year: int | None = None,
    publication_state: str = "published",
    source_version_form: str = "point_in_time_consolidation",
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_version_id": source_version_id,
        "anchor_id": anchor_id,
        "title": title,
        "url": url,
        "source_type": source_type,
        "authority_level": authority_level,
        "tax_domain": tax_domain,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "tax_year": tax_year,
        "publication_state": publication_state,
        "source_version_form": source_version_form,
        "grounding_status": "grounded",
    }


class _ConflictingKnowledgeRepository:
    def __init__(self) -> None:
        self._records = (
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
            KnowledgeSearchRecord(
                source_id="KNW-ITA-16-1",
                title="Income Tax Act (Cap. 470), Section 16(1)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2025-01-01",
                source_type="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="income-tax-act-16-1",
                content="Disallowable deductions under section 16(1).",
            ),
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
        assert source_type == "tax_law"
        assert tax_domain == "income_tax"
        assert effective_date == date(2024, 12, 27)
        return self._records

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
        _ = (publication_state, source_family_id, limit, offset, sort_by, sort_order)
        if tax_domain != "income_tax" or source_class != "tax_law":
            return ()
        if source_id == "KNW-ITA-15-2":
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
        if source_id == "KNW-ITA-16-1":
            return (
                KnowledgeSourceVersionSummaryRecord(
                    source_version_id="123e4567-e89b-12d3-a456-426614174101",
                    source_id="KNW-ITA-16-1",
                    source_family_id="KNW-ITA-FAMILY",
                    title="Income Tax Act (Cap. 470), Section 16(1)",
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
        return ()


class _HistoricalKnowledgeRepository:
    def __init__(self) -> None:
        self._record = KnowledgeSearchRecord(
            source_id="KNW-ITA-15-2-HIST",
            title="Income Tax Act (Cap. 470), Section 15(2) (Historical)",
            url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2023-12-31",
            source_type="tax_law",
            tax_domain="income_tax",
            authority_level="statute",
            effective_from="1974-01-01",
            effective_to="2023-12-31",
            tax_year=None,
            anchor_id="income-tax-act-15-2-historical",
            content="Historical allowable deductions in production of income under section 15(2).",
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
        assert source_type == "tax_law"
        assert tax_domain == "income_tax"
        assert effective_date == date(2024, 12, 27)
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
                source_version_id="123e4567-e89b-12d3-a456-426614174102",
                source_id=self._record.source_id,
                source_family_id="KNW-ITA-FAMILY",
                title=self._record.title,
                source_class=self._record.source_type,
                tax_domain=self._record.tax_domain,
                authority_level=self._record.authority_level,
                publication_state="superseded",
                source_input_origin="official_source_upload",
                source_version_form="point_in_time_consolidation",
                effective_from=self._record.effective_from,
                effective_to=self._record.effective_to,
                tax_year=self._record.tax_year,
                supersedes_source_version_id="123e4567-e89b-12d3-a456-426614174090",
                superseded_by_source_version_id="123e4567-e89b-12d3-a456-426614174110",
            ),
        )


def _execute_prompt(
    *,
    client: TestClient,
    prompt_text: str,
    idempotency_key: str,
) -> Any:
    decide_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "conversation_id": "conv-phase13-grounded-explanation-001",
        "channel": "chat",
        "prompt": {
            "text": prompt_text,
            "format": "plain_text",
        },
    }
    decide = client.post("/v1/orchestration/prompt/decide", headers=HEADERS, json=decide_payload)
    assert decide.status_code == 200
    decision_body = cast(dict[str, object], decide.json())
    execute_payload = {
        "tenant_id": "pilot_tenant_alpha",
        "user_id": "user_phase13_grounded_explanation_001",
        "conversation_id": "conv-phase13-grounded-explanation-001",
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


def _detail(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    payload_dict = cast(dict[str, object], payload)
    detail = payload_dict.get("detail")
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _load_fixture(filename: str) -> dict[str, object]:
    loaded = json.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _normalized_success_payload(payload: dict[str, object]) -> dict[str, object]:
    return {
        "selected_route": payload["selected_route"],
        "grounding_status": payload["grounding_status"],
        "explanation_status": payload["explanation_status"],
        "grounded_evidence": payload["grounded_evidence"],
        "explanation_items": payload["explanation_items"],
        "citations": payload["citations"],
        "authority_summary": payload["authority_summary"],
        "temporal_applicability": payload["temporal_applicability"],
    }


def _normalized_error_detail(detail: dict[str, object], status_code: int) -> dict[str, object]:
    normalized = {
        "status_code": status_code,
        "error_code": detail["error_code"],
        "reason": detail["reason"],
        "reason_code": detail["reason_code"],
    }
    context = detail.get("context")
    if isinstance(context, dict):
        normalized["context"] = {
            key: context[key]
            for key in ("route_id", "target_service", "target_operation")
            if key in context
        }
    return normalized
