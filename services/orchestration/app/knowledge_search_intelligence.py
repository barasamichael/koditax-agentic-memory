"""Intelligent knowledge search with query optimization, ranking, and filtering."""

from __future__ import annotations

from typing import TypedDict
from datetime import date
from collections.abc import Sequence

from services.knowledge.app.repository import KnowledgeSearchRecord

# Minimum composite score of original-query results before expansion variants are tried.
EXPANSION_TRIGGER_THRESHOLD: float = 0.55


class SearchQuery(TypedDict):
    """Represent one optimized search query variant."""

    text: str
    variant_type: str  # "original", "expanded", "simplified"
    priority: int  # execution order


class RankedSearchResult(TypedDict):
    """Represent one search result with relevance and authority scores."""

    record: KnowledgeSearchRecord
    relevance_score: float
    authority_score: float
    currency_score: float
    composite_score: float


class KnowledgeSearchIntelligence:
    """Intelligent knowledge search with optimization, ranking, and filtering."""

    # Authority level weights (higher = more authoritative)
    _AUTHORITY_WEIGHTS = {
        "statute": 1.0,
        "regulation": 0.9,
        "guidance": 0.7,
        "commentary": 0.4,
    }

    # Source type authority bonuses
    _SOURCE_TYPE_WEIGHTS = {
        "tax_law": 1.0,
        "legislative": 1.0,
        "regulation": 0.95,
        "official_guidance": 0.85,
        "administrative": 0.8,
    }

    def __init__(self) -> None:
        """Initialize knowledge search intelligence."""
        pass

    def build_search_queries(
        self,
        original_query: str,
        tax_domain: str,
        include_expansions: bool = True,
    ) -> list[SearchQuery]:
        """Build optimized search query variants.

        Expansions are only included when ``include_expansions`` is True — callers
        should set this to False when the original query already yielded
        high-confidence results (score >= _EXPANSION_TRIGGER_THRESHOLD).
        """

        queries: list[SearchQuery] = [
            SearchQuery(
                text=original_query,
                variant_type="original",
                priority=0,
            )
        ]

        if include_expansions:
            expanded = self._expand_query(original_query, tax_domain)
            for variant_text in expanded:
                queries.append(
                    SearchQuery(
                        text=variant_text,
                        variant_type="expanded",
                        priority=1,
                    )
                )

            simplified = self._simplify_query(original_query)
            if simplified != original_query:
                queries.append(
                    SearchQuery(
                        text=simplified,
                        variant_type="simplified",
                        priority=2,
                    )
                )

        return queries

    def rank_results(
        self,
        results: Sequence[KnowledgeSearchRecord],
        original_query: str,
        tax_domain: str,
        query_tax_year: int | None = None,
    ) -> list[RankedSearchResult]:
        """Rank search results by relevance and authority."""

        ranked: list[RankedSearchResult] = []

        for record in results:
            relevance = self._compute_relevance(
                record=record,
                query=original_query,
                tax_domain=tax_domain,
            )

            authority = self._compute_authority(record)
            currency = self._compute_currency(record, query_tax_year=query_tax_year)

            composite = (
                relevance * 0.5 + authority * 0.3 + currency * 0.2
            )

            ranked.append(
                RankedSearchResult(
                    record=record,
                    relevance_score=relevance,
                    authority_score=authority,
                    currency_score=currency,
                    composite_score=composite,
                )
            )

        return sorted(ranked, key=lambda x: x["composite_score"], reverse=True)

    def filter_results(
        self,
        ranked_results: Sequence[RankedSearchResult],
        min_confidence: float = 0.5,
        max_results: int = 10,
        query_tax_year: int | None = None,
    ) -> list[RankedSearchResult]:
        """Filter results by quality and limit count."""

        filtered: list[RankedSearchResult] = []

        for result in ranked_results:
            if result["composite_score"] < min_confidence:
                break

            if len(filtered) >= max_results:
                break

            record = result["record"]
            if not self._is_current_or_applicable(record, query_tax_year=query_tax_year):
                continue

            filtered.append(result)

        return filtered

    def _expand_query(self, query: str, tax_domain: str) -> list[str]:
        """Expand query with synonyms and related terms."""

        # Domain-specific expansions
        expansions: dict[str, list[str]] = {
            "income_tax": [
                "tax rate",
                "marginal rate",
                "effective rate",
                "tax bracket",
                "taxable income",
                "deductible",
                "allowable expense",
                "P9 form",
                "iTax",
                "annual return",
                "employment return",
                "file returns",
                "nil return",
            ],
            "business_income_generalized": [
                "business expenses",
                "allowable expenses",
                "deductible expenses",
                "home office",
                "home office deduction",
                "work from home",
                "rent",
                "electricity",
                "internet",
                "office at home",
                "sole proprietor",
                "self-employed",
            ],
            "paye_generalized": [
                "PAYE bands",
                "monthly pay bands",
                "tax bracket",
                "tax bands",
                "pay as you earn",
                "tax rate",
                "marginal rate",
            ],
            "health_contribution": [
                "NHIF",
                "SHIF",
                "SHA",
                "health levy",
                "insurance contribution",
            ],
        }

        expanded: list[str] = []

        # Add synonyms for common terms
        synonym_map: dict[str, list[str]] = {
            "deductible": ["allowable expense", "capital allowance", "tax relief"],
            "rate": ["percentage", "tariff"],
            "income": ["earnings", "revenue"],
            "employer": ["company", "organization"],
            "employee": ["worker", "staff"],
        }

        for term, synonyms in synonym_map.items():
            if term in query.lower():
                for syn in synonyms:
                    expanded_query = query.lower().replace(term, syn)
                    expanded.append(expanded_query)

        # Add domain-specific expansions
        for expansion_term in expansions.get(tax_domain, []):
            if expansion_term not in query.lower():
                expanded.append(f"{query} {expansion_term}")

        return expanded[:5]  # Limit to 5 expansions

    def _simplify_query(self, query: str) -> str:
        """Simplify query by removing qualifiers."""

        # Remove common qualifiers
        qualifiers = [
            "please",
            "can you",
            "tell me",
            "what is",
            "how do",
            "i need",
        ]

        simplified = query.lower()
        for qualifier in qualifiers:
            simplified = simplified.replace(qualifier, "").strip()

        return simplified

    def _compute_relevance(
        self,
        record: KnowledgeSearchRecord,
        query: str,
        tax_domain: str,
    ) -> float:
        """Compute relevance score (0.0-1.0) for a result."""

        title = record.title.lower()
        content = record.content.lower()
        query_lower = query.lower()

        # Exact match in title (highest)
        if query_lower in title:
            return 1.0

        # Keywords from query appear in content
        query_words = query_lower.split()
        matching_words = sum(1 for word in query_words if word in content)
        keyword_score = matching_words / len(query_words) if query_words else 0.0

        # Domain match
        domain_match = 1.0 if tax_domain in record.tax_domain.lower() else 0.5

        combined = keyword_score * 0.6 + domain_match * 0.4
        return min(1.0, combined)

    def _compute_authority(self, record: KnowledgeSearchRecord) -> float:
        """Compute authority score (0.0-1.0) for a result."""

        authority_weight = self._AUTHORITY_WEIGHTS.get(record.authority_level.lower(), 0.5)
        source_weight = self._SOURCE_TYPE_WEIGHTS.get(record.source_type.lower(), 0.5)

        combined = authority_weight * 0.6 + source_weight * 0.4
        return min(1.0, combined)

    def _compute_currency(
        self,
        record: KnowledgeSearchRecord,
        query_tax_year: int | None = None,
    ) -> float:
        """Compute currency score based on effective dates and query tax year."""

        # KnowledgeSearchRecord has no publication_state field — derive it from
        # the source version resolved at grounding time. At ranking time we use
        # the effective date window as the currency signal directly.

        if query_tax_year is not None:
            # Exact tax-year match is highest confidence.
            if record.tax_year is not None and record.tax_year == query_tax_year:
                return 1.0

            # Check effective date window overlap with the query year.
            effective_from_str = record.effective_from
            effective_to_str = record.effective_to
            if effective_from_str:
                try:
                    effective_from = date.fromisoformat(effective_from_str)
                    year_start = date(query_tax_year, 1, 1)
                    year_end = date(query_tax_year, 12, 31)

                    if effective_to_str:
                        effective_to = date.fromisoformat(effective_to_str)
                        # Window fully covers the query year → full score.
                        if effective_from <= year_start and effective_to >= year_end:
                            return 1.0
                        # Window partially overlaps the query year → partial score.
                        if effective_from <= year_end and effective_to >= year_start:
                            return 0.75
                        # Window does not overlap → penalise heavily.
                        return 0.2
                    else:
                        # No end date: valid from effective_from onwards.
                        if effective_from <= year_end:
                            return 1.0
                        return 0.2
                except ValueError:
                    pass

        # No query year context: recent records score higher than historical ones.
        effective_from_str = record.effective_from
        if effective_from_str:
            try:
                effective_from = date.fromisoformat(effective_from_str)
                age_years = (date.today() - effective_from).days / 365.25
                # Decay: full score for <2 years old, floor of 0.5 for very old.
                return max(0.5, 1.0 - age_years * 0.05)
            except ValueError:
                pass

        return 0.7

    def _is_current_or_applicable(
        self,
        record: KnowledgeSearchRecord,
        query_tax_year: int | None = None,
    ) -> bool:
        """Check if a result is applicable to the query context.

        When a specific tax year is requested, records are only included if
        their effective date window covers that year.
        """

        if query_tax_year is None:
            # Without year context, include everything with a known effective date.
            return bool(record.effective_from)

        # Exact tax-year match always qualifies.
        if record.tax_year is not None and record.tax_year == query_tax_year:
            return True

        effective_from_str = record.effective_from
        effective_to_str = record.effective_to

        if not effective_from_str:
            return False

        try:
            ef = date.fromisoformat(effective_from_str)
            year_start = date(query_tax_year, 1, 1)
            year_end = date(query_tax_year, 12, 31)
            if effective_to_str:
                et = date.fromisoformat(effective_to_str)
                return ef <= year_end and et >= year_start
            # No end date: valid from effective_from onwards.
            return ef <= year_end
        except ValueError:
            return False
