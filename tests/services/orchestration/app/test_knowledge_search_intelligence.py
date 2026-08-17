"""Unit tests for knowledge search intelligence layer."""

from __future__ import annotations

import pytest

from services.knowledge.app.repository import KnowledgeSearchRecord
from services.orchestration.app.knowledge_search_intelligence import (
    KnowledgeSearchIntelligence,
    RankedSearchResult,
)


@pytest.fixture
def intelligence() -> KnowledgeSearchIntelligence:
    """Provide KnowledgeSearchIntelligence instance."""
    return KnowledgeSearchIntelligence()


@pytest.fixture
def sample_records() -> list[KnowledgeSearchRecord]:
    """Provide sample knowledge records as dataclass instances."""
    return [
        KnowledgeSearchRecord(
            source_id="statute_001",
            anchor_id="section_1",
            title="Income Tax Act - Deductions",
            content="Allowable expenses include business costs and deductions...",
            url="https://example.com/statute/001",
            source_type="statute",
            authority_level="statute",
            tax_domain="income_tax",
            effective_from="2020-01-01",
            effective_to=None,
            tax_year=2024,
        ),
        KnowledgeSearchRecord(
            source_id="regulation_001",
            anchor_id="rule_1",
            title="Tax Regulations - Capital Allowances",
            content="Capital allowance is a tax relief for business expenses...",
            url="https://example.com/regulation/001",
            source_type="regulation",
            authority_level="regulation",
            tax_domain="income_tax",
            effective_from="2020-01-01",
            effective_to=None,
            tax_year=2024,
        ),
        KnowledgeSearchRecord(
            source_id="guidance_001",
            anchor_id="guide_1",
            title="Tax Office Guidance - Deductible Expenses",
            content="The tax office provides guidance on what expenses are deductible...",
            url="https://example.com/guidance/001",
            source_type="guidance",
            authority_level="guidance",
            tax_domain="income_tax",
            effective_from="2020-01-01",
            effective_to=None,
            tax_year=2024,
        ),
    ]


class TestQueryExpansion:
    """Test query expansion functionality."""

    def test_build_search_queries_returns_original(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Original query should be first in the list."""
        queries = intelligence.build_search_queries(
            original_query="what are allowable deductions",
            tax_domain="income_tax",
        )

        assert len(queries) > 0
        assert queries[0]["text"] == "what are allowable deductions"
        assert queries[0]["variant_type"] == "original"
        assert queries[0]["priority"] == 0

    def test_build_search_queries_includes_expansions(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Expanded queries should be included."""
        queries = intelligence.build_search_queries(
            original_query="deductible expenses",
            tax_domain="income_tax",
        )

        variant_types = [q["variant_type"] for q in queries]
        assert "original" in variant_types
        assert "expanded" in variant_types

    def test_build_search_queries_respects_priority(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Queries should be ordered by priority."""
        queries = intelligence.build_search_queries(
            original_query="tax rate",
            tax_domain="income_tax",
        )

        original = [q for q in queries if q["variant_type"] == "original"][0]
        assert original["priority"] == 0

        expanded = [q for q in queries if q["variant_type"] == "expanded"]
        for q in expanded:
            assert q["priority"] == 1

    def test_build_search_queries_domain_specific_expansion(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Domain-specific terms should be expanded."""
        queries = intelligence.build_search_queries(
            original_query="employer contribution",
            tax_domain="health_contribution",
        )

        texts = [q["text"] for q in queries]
        assert any("NHIF" in text or "SHIF" in text for text in texts)


class TestResultRanking:
    """Test result ranking and scoring functionality."""

    def test_rank_results_by_relevance(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Results should be ranked by relevance."""
        ranked = intelligence.rank_results(
            results=sample_records,
            original_query="deductible expenses",
            tax_domain="income_tax",
        )

        assert len(ranked) > 0
        assert all("relevance_score" in r for r in ranked)
        assert all(0.0 <= r["relevance_score"] <= 1.0 for r in ranked)

    def test_rank_results_by_authority(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Statute sources should rank higher than guidance."""
        ranked = intelligence.rank_results(
            results=sample_records,
            original_query="deductible",
            tax_domain="income_tax",
        )

        statute_results = [r for r in ranked if r["record"].source_type == "statute"]
        guidance_results = [r for r in ranked if r["record"].source_type == "guidance"]

        if statute_results and guidance_results:
            assert statute_results[0]["authority_score"] > guidance_results[0]["authority_score"]

    def test_rank_results_by_composite_score(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Results should be ordered by composite score descending."""
        ranked = intelligence.rank_results(
            results=sample_records,
            original_query="allowable expenses",
            tax_domain="income_tax",
        )

        for i in range(len(ranked) - 1):
            assert ranked[i]["composite_score"] >= ranked[i + 1]["composite_score"]

    def test_rank_results_returns_typed_dict(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Ranked results should have all required fields."""
        ranked = intelligence.rank_results(
            results=sample_records,
            original_query="deductible",
            tax_domain="income_tax",
        )

        for result in ranked:
            assert "record" in result
            assert "relevance_score" in result
            assert "authority_score" in result
            assert "currency_score" in result
            assert "composite_score" in result


class TestResultFiltering:
    """Test result filtering functionality."""

    def test_filter_results_by_confidence(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Low-confidence results should be filtered out."""
        ranked = intelligence.rank_results(
            results=sample_records,
            original_query="deductible",
            tax_domain="income_tax",
        )

        filtered = intelligence.filter_results(
            ranked_results=ranked,
            min_confidence=0.5,
            max_results=10,
        )

        for result in filtered:
            assert result["composite_score"] >= 0.5

    def test_filter_results_respects_max_results(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Result count should not exceed max_results."""
        ranked = intelligence.rank_results(
            results=sample_records,
            original_query="deductible",
            tax_domain="income_tax",
        )

        filtered = intelligence.filter_results(
            ranked_results=ranked,
            min_confidence=0.0,
            max_results=2,
        )

        assert len(filtered) <= 2

    def test_filter_results_excludes_records_without_effective_date(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Records with empty effective_from should be excluded when no tax year is given."""
        records = [
            KnowledgeSearchRecord(
                source_id="dated_001",
                anchor_id="a1",
                title="Dated Source",
                content="content",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2020-01-01",
                effective_to=None,
                tax_year=2024,
            ),
            KnowledgeSearchRecord(
                source_id="undated_001",
                anchor_id="a2",
                title="Undated Source",
                content="content",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="",
                effective_to=None,
                tax_year=None,
            ),
        ]

        ranked = intelligence.rank_results(
            results=records,
            original_query="test",
            tax_domain="income_tax",
        )

        filtered = intelligence.filter_results(
            ranked_results=ranked,
            min_confidence=0.0,
            max_results=10,
        )

        source_ids = [r["record"].source_id for r in filtered]
        assert "undated_001" not in source_ids

    def test_filter_results_with_tax_year_window(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """When query_tax_year is given, only records covering that year pass."""
        records = [
            KnowledgeSearchRecord(
                source_id="current_001",
                anchor_id="a1",
                title="Current Source",
                content="content",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2020-01-01",
                effective_to=None,
                tax_year=2024,
            ),
            KnowledgeSearchRecord(
                source_id="old_001",
                anchor_id="a2",
                title="Old Source",
                content="content",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2010-01-01",
                effective_to="2015-12-31",
                tax_year=2015,
            ),
        ]

        ranked = intelligence.rank_results(
            results=records,
            original_query="test",
            tax_domain="income_tax",
            query_tax_year=2024,
        )

        filtered = intelligence.filter_results(
            ranked_results=ranked,
            min_confidence=0.0,
            max_results=10,
            query_tax_year=2024,
        )

        source_ids = [r["record"].source_id for r in filtered]
        assert "current_001" in source_ids
        assert "old_001" not in source_ids

    def test_filter_results_allows_superseded_in_window(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Superseded records covering the queried tax year should be included."""
        records = [
            KnowledgeSearchRecord(
                source_id="superseded_001",
                anchor_id="a1",
                title="Superseded Source",
                content="content",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2020-01-01",
                effective_to="2023-12-31",
                tax_year=2023,
            ),
        ]

        ranked = intelligence.rank_results(
            results=records,
            original_query="test",
            tax_domain="income_tax",
            query_tax_year=2023,
        )

        filtered = intelligence.filter_results(
            ranked_results=ranked,
            min_confidence=0.0,
            max_results=10,
            query_tax_year=2023,
        )

        assert len(filtered) > 0
        assert filtered[0]["record"].source_id == "superseded_001"


class TestCompositeScoring:
    """Test composite scoring mechanism."""

    def test_composite_score_weights_relevance(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """High relevance should yield high composite score."""
        records = [
            KnowledgeSearchRecord(
                source_id="exact_001",
                anchor_id="a1",
                title="what are allowable deductions",
                content="some content about tax",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2020-01-01",
                effective_to=None,
                tax_year=2024,
            ),
        ]

        ranked = intelligence.rank_results(
            results=records,
            original_query="what are allowable deductions",
            tax_domain="income_tax",
        )

        assert ranked[0]["composite_score"] > 0.7

    def test_composite_score_weights_authority(
        self,
        intelligence: KnowledgeSearchIntelligence,
        sample_records: list[KnowledgeSearchRecord],
    ) -> None:
        """Statute authority_level should yield authority_score above 0.8."""
        statute_record = [r for r in sample_records if r.authority_level == "statute"][0]

        ranked = intelligence.rank_results(
            results=[statute_record],
            original_query="test",
            tax_domain="income_tax",
        )

        assert ranked[0]["authority_score"] >= 0.8


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_rank_results_with_empty_records(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Empty records should return empty ranked list."""
        ranked = intelligence.rank_results(
            results=[],
            original_query="test",
            tax_domain="income_tax",
        )

        assert len(ranked) == 0

    def test_filter_results_with_empty_ranked(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Empty ranked results should return empty filtered list."""
        filtered = intelligence.filter_results(
            ranked_results=[],
            min_confidence=0.5,
            max_results=10,
        )

        assert len(filtered) == 0

    def test_build_search_queries_with_empty_query(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Empty query should still return at least the original query variant."""
        queries = intelligence.build_search_queries(
            original_query="",
            tax_domain="income_tax",
        )

        assert len(queries) >= 1
        assert queries[0]["text"] == ""

    def test_rank_results_with_empty_content(
        self,
        intelligence: KnowledgeSearchIntelligence,
    ) -> None:
        """Records with empty content field should not raise."""
        records = [
            KnowledgeSearchRecord(
                source_id="test",
                anchor_id="a1",
                title="Test",
                content="",
                url="http://example.com",
                source_type="statute",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2020-01-01",
                effective_to=None,
                tax_year=2024,
            ),
        ]

        ranked = intelligence.rank_results(
            results=records,
            original_query="test",
            tax_domain="income_tax",
        )

        assert len(ranked) > 0
