"""Integration tests for knowledge adapter with search intelligence."""

from __future__ import annotations

from typing import cast
from typing import Protocol
from datetime import date

import pytest

from services.orchestration.app import action_adapter_registry
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_registry import DeterministicKnowledgeActionAdapter
from services.orchestration.app.action_adapter_registry import filter_grounded_evidence_for_scope


def _make_source_version(
    source_id: str = "statute_001",
    source_version_id: str = "statute_001_v1",
) -> KnowledgeSourceVersionSummaryRecord:
    return KnowledgeSourceVersionSummaryRecord(
        source_version_id=source_version_id,
        source_id=source_id,
        source_family_id="statute_family_001",
        title="Income Tax Act",
        source_class="statute",
        tax_domain="income_tax",
        authority_level="statute",
        publication_state="published",
        source_input_origin="official_source_upload",
        source_version_form="v1.0",
        effective_from="2020-01-01",
        effective_to=None,
        tax_year=2024,
        supersedes_source_version_id=None,
        superseded_by_source_version_id=None,
    )


def _make_statute_record(
    source_id: str = "statute_001",
    anchor_id: str = "section_1",
    title: str = "Income Tax Act - Deductions",
    content: str = "Allowable expenses include business costs",
    tax_year: int = 2024,
) -> KnowledgeSearchRecord:
    return KnowledgeSearchRecord(
        source_id=source_id,
        anchor_id=anchor_id,
        title=title,
        content=content,
        url=f"https://example.com/statute/{source_id}",
        source_type="statute",
        authority_level="statute",
        tax_domain="income_tax",
        effective_from="2020-01-01",
        effective_to=None,
        tax_year=tax_year,
    )


class MockKnowledgeRepository(Protocol):
    """Mock knowledge repository for testing."""

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        ...

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        ...

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
        ...


class TestKnowledgeSearchIntelligenceIntegration:
    """Integration tests for knowledge search with intelligence layer."""

    @pytest.fixture
    def search_results_repository(self) -> MockKnowledgeRepository:
        """Create mock repository with test data."""

        class MockRepo:
            def search_records(
                self,
                *,
                query: str,
                source_type: str | None,
                tax_domain: str | None,
                effective_date: date | None,
            ) -> tuple[KnowledgeSearchRecord, ...]:
                if "allowable" in query.lower() or "deductible" in query.lower():
                    return (_make_statute_record(),)
                return ()

            def retrieve_records(
                self,
                *,
                source_ids: tuple[str, ...],
                anchor_ids: tuple[str, ...],
            ) -> tuple[KnowledgeSearchRecord, ...]:
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
                return (_make_source_version(),)

        return cast(MockKnowledgeRepository, MockRepo())

    @pytest.fixture
    def adapter(
        self,
        search_results_repository: MockKnowledgeRepository,
    ) -> DeterministicKnowledgeActionAdapter:
        """Create adapter with mock repository."""
        return DeterministicKnowledgeActionAdapter(
            repository=search_results_repository,
        )

    def test_adapter_with_original_query_match(
        self,
        adapter: DeterministicKnowledgeActionAdapter,
    ) -> None:
        """Adapter should use original query and rank results."""
        request: ActionAdapterRequest = {
            "action_type": "knowledge_search_knowledge",
            "correlation_id": "test-123",
            "submission_payload_ref": "payload-ref",
            "capability_context": {
                "tax_year": 2024,
                "historical_version_id": "v1",
                "supported_lane_id": "lane-1",
            },
            "route_payload": {
                "query": "what are allowable business deductions",
                "source_type": None,
                "tax_domain": "income_tax",
                "effective_date": None,
            },
        }

        response = adapter.dispatch(request)

        assert response["adapter_status"] == "accepted"
        assert "result_payload" in response
        result_payload = response["result_payload"]
        assert result_payload["grounding_status"] == "grounded"
        assert len(result_payload["grounded_evidence"]) > 0

    def test_adapter_filters_to_max_results(
        self,
        adapter: DeterministicKnowledgeActionAdapter,
    ) -> None:
        """Adapter should limit results to max_results."""

        class LargeResultRepository:
            def search_records(
                self,
                *,
                query: str,
                source_type: str | None,
                tax_domain: str | None,
                effective_date: date | None,
            ) -> tuple[KnowledgeSearchRecord, ...]:
                return tuple(
                    _make_statute_record(
                        source_id=f"statute_{i:03d}",
                        anchor_id=f"section_{i}",
                        title=f"Source {i}",
                        content="allowable deductible content",
                    )
                    for i in range(20)
                )

            def retrieve_records(
                self,
                *,
                source_ids: tuple[str, ...],
                anchor_ids: tuple[str, ...],
            ) -> tuple[KnowledgeSearchRecord, ...]:
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
                return ()

        large_adapter = DeterministicKnowledgeActionAdapter(
            repository=cast(MockKnowledgeRepository, LargeResultRepository()),
        )

        request: ActionAdapterRequest = {
            "action_type": "knowledge_search_knowledge",
            "correlation_id": "test-123",
            "submission_payload_ref": "payload-ref",
            "capability_context": {
                "tax_year": 2024,
                "historical_version_id": "v1",
                "supported_lane_id": "lane-1",
            },
            "route_payload": {
                "query": "allowable deductions",
                "source_type": None,
                "tax_domain": "income_tax",
                "effective_date": None,
            },
        }

        response = large_adapter.dispatch(request)

        if response["adapter_status"] == "accepted":
            result_payload = response["result_payload"]
            assert len(result_payload["grounded_evidence"]) <= 10

    def test_adapter_stops_on_original_query_match(
        self,
        adapter: DeterministicKnowledgeActionAdapter,
    ) -> None:
        """Adapter should not fire expansion queries when original results are strong."""
        call_count = {"original": 0, "expanded": 0}

        class TrackingRepository:
            def search_records(
                self,
                *,
                query: str,
                source_type: str | None,
                tax_domain: str | None,
                effective_date: date | None,
            ) -> tuple[KnowledgeSearchRecord, ...]:
                if query == "allowable deductions":
                    call_count["original"] += 1
                else:
                    call_count["expanded"] += 1

                if "allowable" in query.lower():
                    return (_make_statute_record(content="allowable deductible content"),)
                return ()

            def retrieve_records(
                self,
                *,
                source_ids: tuple[str, ...],
                anchor_ids: tuple[str, ...],
            ) -> tuple[KnowledgeSearchRecord, ...]:
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
                return ()

        tracking_adapter = DeterministicKnowledgeActionAdapter(
            repository=cast(MockKnowledgeRepository, TrackingRepository()),
        )

        request: ActionAdapterRequest = {
            "action_type": "knowledge_search_knowledge",
            "correlation_id": "test-123",
            "submission_payload_ref": "payload-ref",
            "capability_context": {
                "tax_year": 2024,
                "historical_version_id": "v1",
                "supported_lane_id": "lane-1",
            },
            "route_payload": {
                "query": "allowable deductions",
                "source_type": None,
                "tax_domain": "income_tax",
                "effective_date": None,
            },
        }

        tracking_adapter.dispatch(request)

        assert call_count["original"] == 1
        assert call_count["expanded"] == 0

    def test_scope_filter_retains_title_domain_and_synonym_body(self) -> None:
        evidence = [
            {
                "source_id": "web:kra.go.ke",
                "source_version_id": "web:kra.go.ke",
                "anchor_id": "https://www.kra.go.ke/income-tax",
                "title": "Income Tax filing deadline",
                "url": "https://www.kra.go.ke/income-tax",
                "source_type": "web",
                "authority_level": "primary",
                "tax_domain": "income_tax",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "tax_year": 2026,
                "publication_state": "published",
                "source_version_form": "web",
                "grounding_status": "grounded",
                "content": "The return due date is 30 June for resident taxpayers.",
            }
        ]

        filtered = filter_grounded_evidence_for_scope(
            evidence,
            tax_domain_hint="income_tax",
            resolved_entity="filing deadline",
            query_text="When is the KRA tax filing deadline?",
        )

        assert len(filtered) == 1
        assert filtered[0]["scope_diagnostic"] in {
            "retained_title_domain_body_entity",
            "retained_adjacent_passage_match",
            "retained_domain_marker_entity_not_required",
        }

    def test_scope_filter_retains_paye_bands_synonym_match(self) -> None:
        evidence = [
            {
                "source_id": "web:kra.go.ke",
                "source_version_id": "web:kra.go.ke",
                "anchor_id": "https://www.kra.go.ke/paye",
                "title": "Income Tax Act guidance",
                "url": "https://www.kra.go.ke/paye",
                "source_type": "web",
                "authority_level": "primary",
                "tax_domain": "income_tax",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "tax_year": 2026,
                "publication_state": "published",
                "source_version_form": "web",
                "grounding_status": "grounded",
                "content": "Graduated tax rates apply to employment income.",
            }
        ]

        filtered = filter_grounded_evidence_for_scope(
            evidence,
            tax_domain_hint="paye_generalized",
            resolved_entity="PAYE bands",
            query_text="What is the current PAYE tax band for someone earning 80,000 shillings a month?",
        )

        assert len(filtered) == 1

    def test_adapter_handles_no_results(
        self,
        adapter: DeterministicKnowledgeActionAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Adapter should fall back to web search when corpus results are empty."""
        calls: list[dict[str, object]] = []

        class WebSearchStub:
            def search_tax_topic(self, **kwargs: object) -> list[dict[str, object]]:
                calls.append(kwargs)
                return [
                    {
                        "answer_text": "Income tax filing deadlines are published by KRA.",
                        "source_url": "https://www.kra.go.ke/income-tax",
                        "title": "Income Tax filing deadline",
                        "authority_level": "primary",
                        "publication_date": None,
                        "domain": "kra.go.ke",
                    }
                ]

        monkeypatch.setattr(action_adapter_registry, "TavilyWebSearchClient", WebSearchStub)
        request: ActionAdapterRequest = {
            "action_type": "knowledge_search_knowledge",
            "correlation_id": "test-123",
            "submission_payload_ref": "payload-ref",
            "capability_context": {
                "tax_year": 2024,
                "historical_version_id": "v1",
                "supported_lane_id": "lane-1",
            },
            "route_payload": {
                "query": "When is the income tax filing deadline?",
                "source_type": None,
                "tax_domain": "income_tax",
                "effective_date": None,
            },
        }

        response = adapter.dispatch(request)

        assert calls
        assert response["adapter_status"] == "accepted"
        assert response["result_payload"]["grounding_status"] == "web_grounded"
        assert response["result_payload"]["grounded_evidence"]


class TestAdapterWithNoRepository:
    """Test adapter behavior when repository is not configured."""

    def test_dispatch_with_no_repository(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Adapter should fall back to web search when repository is None."""
        calls: list[str] = []

        class WebSearchStub:
            def search_tax_topic(self, **_: object) -> list[dict[str, object]]:
                calls.append("search")
                return [
                    {
                        "answer_text": "Income tax filing deadlines are published by KRA.",
                        "source_url": "https://www.kra.go.ke/income-tax",
                        "title": "Income Tax filing deadline",
                        "authority_level": "primary",
                        "publication_date": None,
                        "domain": "kra.go.ke",
                    }
                ]

        monkeypatch.setattr(action_adapter_registry, "TavilyWebSearchClient", WebSearchStub)
        adapter = DeterministicKnowledgeActionAdapter(repository=None)

        request: ActionAdapterRequest = {
            "action_type": "knowledge_search_knowledge",
            "correlation_id": "test-123",
            "submission_payload_ref": "payload-ref",
            "capability_context": {
                "tax_year": 2024,
                "historical_version_id": "v1",
                "supported_lane_id": "lane-1",
            },
            "route_payload": {
                "query": "What is the VAT filing deadline?",
                "source_type": None,
                "tax_domain": "vat",
                "effective_date": None,
            },
        }

        response = adapter.dispatch(request)

        assert calls == ["search"]
        assert response["adapter_status"] == "accepted"
        assert response["result_payload"]["grounding_status"] == "web_grounded"


class TestWebSearchFallback:
    """Web search must run before a corpus-search evidence failure is returned."""

    @staticmethod
    def _request() -> ActionAdapterRequest:
        return {
            "action_type": "knowledge_search_knowledge",
            "correlation_id": "test-123",
            "submission_payload_ref": "payload-ref",
            "capability_context": {
                "tax_year": 2026,
                "historical_version_id": None,
                "supported_lane_id": None,
            },
            "route_payload": {
                "query": "what is VAT?",
                "source_type": None,
                "tax_domain": "vat",
                "effective_date": None,
                "tax_year": 2026,
            },
        }

    def test_empty_repository_search_uses_web_before_evidence_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, object]] = []

        class EmptyRepository:
            def search_records(self, **_: object) -> tuple[KnowledgeSearchRecord, ...]:
                return ()

        class WebSearchStub:
            def search_tax_topic(self, **kwargs: object) -> list[dict[str, object]]:
                calls.append(kwargs)
                return [
                    {
                        "answer_text": "VAT is charged on taxable supplies.",
                        "source_url": "https://www.kra.go.ke/vat",
                        "title": "Value Added Tax",
                        "authority_level": "primary",
                        "publication_date": None,
                        "domain": "kra.go.ke",
                    }
                ]

        monkeypatch.setattr(action_adapter_registry, "TavilyWebSearchClient", WebSearchStub)
        response = DeterministicKnowledgeActionAdapter(
            repository=cast(MockKnowledgeRepository, EmptyRepository())
        ).dispatch(self._request())

        assert calls == [
            {
                "query": "what is VAT?",
                "tax_year": 2026,
                "jurisdiction": "Kenya",
                "tax_domain_hint": "vat",
            }
        ]
        assert response["adapter_status"] == "accepted"
        assert response["result_payload"]["grounding_status"] == "web_grounded"
        assert response["result_payload"]["grounded_evidence"][0]["source_id"] == "web:kra.go.ke"

    def test_missing_repository_uses_web_before_evidence_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        class WebSearchStub:
            def search_tax_topic(self, **_: object) -> list[dict[str, object]]:
                calls.append("search")
                return [
                    {
                        "answer_text": "VAT is charged on taxable supplies.",
                        "source_url": "https://www.kra.go.ke/vat",
                        "title": "Value Added Tax",
                        "authority_level": "primary",
                        "publication_date": None,
                        "domain": "kra.go.ke",
                    }
                ]

        monkeypatch.setattr(action_adapter_registry, "TavilyWebSearchClient", WebSearchStub)
        response = DeterministicKnowledgeActionAdapter(repository=None).dispatch(self._request())

        assert calls == ["search"]
        assert response["adapter_status"] == "accepted"
        assert response["result_payload"]["grounding_status"] == "web_grounded"
