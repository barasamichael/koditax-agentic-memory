"""Tavily web search integration for knowledge corpus fallback."""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from typing import cast
from typing import TypedDict
from collections.abc import Mapping

import httpx

from services.orchestration.app.config import TavilyWebSearchConfig
from services.orchestration.app.config import load_tavily_web_search_config
from services.orchestration.app.debug_trace import bounded_preview
from services.orchestration.app.request_timer import timed_print

# Days of recency to request from Tavily per tax-domain. Domains with
# frequently-updated rates/thresholds get a tight window; procedural
# guidance is more stable and can tolerate older sources.
FRESHNESS_DAYS_BY_DOMAIN: dict[str, int] = {
    "rates_thresholds": 30,
    "amnesty_waiver": 30,
    "paye_bands": 30,
    "penalties": 90,
    "process_procedural": 180,
    "general_advisory": 365,
}
_DEFAULT_FRESHNESS_DAYS = 180

_DOMAIN_QUERY_TERMS: dict[str, tuple[str, ...]] = {
    "vat": ("VAT", "value added tax"),
    "income_tax": ("income tax", "P9", "iTax", "annual return", "employment return"),
    "health_contribution": ("health contribution", "SHIF"),
    "paye_generalized": (
        "PAYE",
        "pay as you earn",
        "tax band",
        "tax bands",
        "monthly pay bands",
        "tax bracket",
    ),
    "withholding_tax_generalized": ("withholding tax",),
    "business_income_generalized": (
        "business income tax",
        "income tax act",
        "deductions",
        "allowable deductions",
        "section 15",
        "business expenses",
        "allowable expenses",
        "deductible expenses",
        "home office",
        "home office deduction",
        "work from home",
    ),
    "rental_income_generalized": ("rental income tax",),
}


class TavilySearchError(RuntimeError):
    """Represent Tavily search API errors."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason_code: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason_code = reason_code
        self.context = context


class SearchResult(TypedDict):
    """Represent one search result with authority metadata."""

    answer_text: str
    source_url: str
    title: str
    authority_level: str
    publication_date: str | None
    domain: str


# KRA pages to fetch live for time-sensitive tax domains.
# All URLs verified live against kra.go.ke as of May 2026.
KRA_LIVE_SOURCES: dict[str, str] = {
    "paye_bands": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/individual-income-tax",
    "paye": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/paye",
    "vat": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/value-added-tax",
    "rental_income": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/residential-rental-income",
    "capital_gains": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/capital-gains-tax",
    "withholding_tax": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/individual-withholding-tax",
    "installment_tax": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/installment-tax",
    "turnover_tax": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/turnover-tax-tot",
    "penalties": "https://www.kra.go.ke/business/business-compliance-penalties/business-how-to-file/business-offences-penalties",
    "amnesty_waiver": "https://www.kra.go.ke/news-center",
    "tcc": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/tax-compliance",
}

# Domains that warrant a live KRA extract before the Tavily search.
EXTRACT_ELIGIBLE_DOMAINS = frozenset(
    {"rates_thresholds", "paye_bands", "amnesty_waiver", "penalties"}
)


class ExtractResult(TypedDict):
    """Represent content extracted from one URL via Tavily extract."""

    url: str
    raw_content: str


class TavilyWebSearchClient:
    """Use Tavily API for web search fallback when corpus is insufficient."""

    _TAVILY_API_URL = "https://api.tavily.com/search"
    _TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

    # Only these trusted Kenyan tax authority domains are allowed as sources.
    # Restricting at the API level prevents off-domain content from ever
    # entering the synthesis pipeline.
    _TRUSTED_DOMAINS: list[str] = [
        "kra.go.ke",
        "kenyalaw.org",
        "kesra.ac.ke",
        "pwc.com",
    ]
    _MAX_QUERY_LENGTH = 380

    def __init__(self, *, config: TavilyWebSearchConfig | None = None) -> None:
        self._config = config or load_tavily_web_search_config()

    def search_tax_topic(
        self,
        query: str,
        tax_year: int | None = None,
        jurisdiction: str | None = None,
        max_results: int | None = None,
        tax_domain_hint: str | None = None,
        resolved_entity: str | None = None,
    ) -> list[SearchResult]:
        """Search for tax-related content with authority scoring."""

        if not self._config.configured:
            raise TavilySearchError(
                error_code="web_search_unavailable",
                message="Tavily web search is not configured.",
                reason_code="missing_tavily_configuration",
            )

        max_results = max_results or self._config.max_results
        search_query = self._build_search_query(
            query=query,
            tax_year=tax_year,
            jurisdiction=jurisdiction,
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
        )
        days = self._resolve_search_recency_days(
            tax_year=tax_year,
            tax_domain_hint=tax_domain_hint,
        )
        start_date = self._resolve_search_start_date(days=days)

        try:
            results = self._execute_search(
                search_query=search_query,
                start_date=start_date,
                max_results=max_results,
            )
            fallback_domain: str | None = None
            if not results and tax_domain_hint == "general_tax":
                fallback_domain = "income_tax"
            elif not results and tax_domain_hint == "income_tax":
                fallback_domain = "general_tax"
            elif not results and tax_domain_hint == "business_income_generalized":
                fallback_domain = "general_tax"
            elif not results and tax_domain_hint == "rental_income_generalized":
                fallback_domain = "general_tax"
            if fallback_domain is not None:
                fallback_query = self._build_search_query(
                    query=query,
                    tax_year=tax_year,
                    jurisdiction=jurisdiction,
                    tax_domain_hint=fallback_domain,
                    resolved_entity=resolved_entity,
                )
                fallback_days = self._resolve_search_recency_days(
                    tax_year=tax_year,
                    tax_domain_hint=fallback_domain,
                )
                fallback_start_date = self._resolve_search_start_date(
                    days=fallback_days
                )
                timed_print(
                    "[TAVILY] Retrying Tavily search with fallback domain "
                    f"{fallback_domain!r} after empty {tax_domain_hint!r} result set"
                )
                results = self._execute_search(
                    search_query=fallback_query,
                    start_date=fallback_start_date,
                    max_results=max_results,
                )
            return results

        except httpx.RequestError as error:
            timed_print("[TAVILY] Tavily search request failed")
            raise TavilySearchError(
                error_code="tavily_api_error",
                message=f"Tavily API request failed: {str(error)}",
                reason_code="api_request_failed",
                context={"query": search_query[:100]},
            ) from error
        except httpx.HTTPStatusError as error:
            response = error.response
            response_text = response.text
            timed_print(
                "[TAVILY] Tavily search request rejected "
                f"status_code={response.status_code}"
            )
            raise TavilySearchError(
                error_code="tavily_api_error",
                message=(
                    "Tavily API rejected the search request: "
                    f"{bounded_preview(response_text, max_length=300)}"
                ),
                reason_code="api_request_rejected",
                context={
                    "query": search_query[:100],
                    "status_code": response.status_code,
                },
            ) from error
        except (KeyError, ValueError) as error:
            raise TavilySearchError(
                error_code="tavily_response_parse_error",
                message=f"Failed to parse Tavily response: {str(error)}",
                reason_code="invalid_response_format",
            ) from error

    def _execute_search(
        self,
        *,
        search_query: str,
        start_date: str | None,
        max_results: int,
    ) -> list[SearchResult]:
        timed_print("[TAVILY] About to execute Tavily search request")
        with httpx.Client(timeout=self._config.timeout_seconds) as client:
            request_payload: dict[str, object] = {
                "query": search_query,
                "include_answer": True,
                "max_results": max_results,
                "search_depth": "advanced",
                "topic": "general",
                "include_domains": self._TRUSTED_DOMAINS,
            }
            if start_date is not None:
                request_payload["start_date"] = start_date
            response = client.post(
                self._TAVILY_API_URL,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json=request_payload,
            )
            response.raise_for_status()
            data = response.json()
            print(data)
            print(response.status_code)
            print(response.json())
        timed_print("[TAVILY] Executed Tavily search request")

        results: list[SearchResult] = []
        if "results" in data:
            for item in data["results"]:
                url = item.get("url", "")
                # Defence-in-depth: drop any result whose URL doesn't
                # originate from a trusted domain, even if include_domains
                # was sent (Tavily may still return redirected URLs).
                if not self._is_trusted_domain(url):
                    continue
                result = SearchResult(
                    answer_text=item.get("content", ""),
                    source_url=url,
                    title=item.get("title", ""),
                    authority_level=self._infer_authority_level(url),
                    publication_date=item.get("published_date"),
                    domain=self._extract_domain(url),
                )
                results.append(result)
        timed_print(
            "[TAVILY] Parsed Tavily search results "
            f"result_count={len(results)}"
        )

        return results

    def _resolve_search_start_date(self, *, days: int | None) -> str | None:
        """Return an API-compatible Tavily start date for one recency window."""

        if days is None:
            return None
        start_date = date.today() - timedelta(days=max(days - 1, 0))
        return start_date.isoformat()

    def extract_url(self, url: str) -> ExtractResult | None:
        """Fetch and extract content from one URL via Tavily extract.

        Returns None (rather than raising) when the API is unconfigured or the
        extract call returns no usable content — callers must treat None as a
        soft miss and fall through to the regular search.
        """
        if not self._config.configured:
            return None

        try:
            timed_print("[TAVILY_EXTRACT] About to execute Tavily extract request")
            with httpx.Client(timeout=self._config.timeout_seconds) as client:
                response = client.post(
                    self._TAVILY_EXTRACT_URL,
                    headers={"Authorization": f"Bearer {self._config.api_key}"},
                    json={
                        "urls": [url],
                    },
                )
                response.raise_for_status()
                data: object = response.json()
            timed_print("[TAVILY_EXTRACT] Executed Tavily extract request")

            if not isinstance(data, Mapping):
                return None
            results = cast(Mapping[object, object], data).get("results")
            if not isinstance(results, list) or not results:
                return None
            results = cast(list[object], results)
            first = results[0]
            if not isinstance(first, Mapping):
                return None
            first_result = cast(Mapping[object, object], first)
            raw_content = first_result.get("raw_content")
            if not isinstance(raw_content, str):
                return None
            if not raw_content.strip():
                return None
            extracted_url = first_result.get("url")
            timed_print(
                "[TAVILY_EXTRACT] Parsed Tavily extract response "
                f"content_length={len(raw_content)}"
            )
            return ExtractResult(
                url=extracted_url if isinstance(extracted_url, str) else url,
                raw_content=raw_content,
            )

        except (httpx.RequestError, KeyError, ValueError):
            timed_print("[TAVILY_EXTRACT] Tavily extract request failed")
            return None

    def _build_search_query(
        self,
        query: str,
        tax_year: int | None = None,
        jurisdiction: str | None = None,
        tax_domain_hint: str | None = None,
        resolved_entity: str | None = None,
    ) -> str:
        """Build refined search query with context."""
        query_lower = query.lower()
        if tax_domain_hint == "business_income_generalized":
            jurisdiction_term = jurisdiction.strip() if jurisdiction else "Kenya"
            if any(
                marker in query_lower
                for marker in (
                    "home office",
                    "work from home",
                    "rent",
                    "electricity",
                    "internet",
                    "deduction",
                    "deductions",
                    "allowable expenses",
                    "allowable deductions",
                )
            ):
                return self._join_query_parts(
                    [
                        "home office deductions",
                        jurisdiction_term,
                        "Income Tax Act",
                        "section 15",
                    ]
                )
            return self._join_query_parts(
                [
                    "business expenses",
                    jurisdiction_term,
                    "Income Tax Act",
                    "deductions",
                ]
            )

        parts = [query.strip()]

        if tax_year:
            parts.append(f"{tax_year}")

        if jurisdiction:
            parts.append(jurisdiction.strip())

        # Domain/entity are query terms, never metadata-only annotations.  A
        # continuation such as "and the rate?" must therefore be unable to
        # retrieve a generic or neighbouring-regime result merely because its
        # caller logged a VAT hint separately.
        if tax_domain_hint:
            parts.extend(_DOMAIN_QUERY_TERMS.get(tax_domain_hint, (tax_domain_hint,)))
        if resolved_entity and resolved_entity.lower() not in query.lower():
            parts.append(resolved_entity.strip())

        # Append domain-specific terms so the search targets statutory content.
        # If the query is about exemptions or reliefs, make that explicit so
        # Tavily surfaces the relevant statutory sections rather than summaries.
        if any(
            w in query_lower
            for w in ("exempt", "relief", "deduction", "allowance")
        ):
            parts.extend(
                ["Kenya", "Income Tax Act", "exemption", "relief", "statute"]
            )
        else:
            parts.extend(["Kenya", "tax", "law", "statute"])

        return self._join_query_parts(parts)

    def _resolve_search_recency_days(
        self,
        *,
        tax_year: int | None,
        tax_domain_hint: str | None,
    ) -> int:
        """Return the Tavily recency window in days for one search request."""

        domain_days = FRESHNESS_DAYS_BY_DOMAIN.get(
            tax_domain_hint or "", _DEFAULT_FRESHNESS_DAYS
        )
        if tax_year is not None:
            return domain_days

        today = date.today()
        three_calendar_year_start = date(today.year - 2, 1, 1)
        return (today - three_calendar_year_start).days + 1

    def _join_query_parts(self, parts: list[str]) -> str:
        """Join query parts without exceeding Tavily's query-length limit."""

        bounded: list[str] = []
        total_length = 0
        for part in parts:
            normalized = part.strip()
            if not normalized:
                continue
            remaining = self._MAX_QUERY_LENGTH - total_length
            if remaining <= 0:
                break
            if not bounded:
                if len(normalized) > remaining:
                    bounded.append(normalized[:remaining].rstrip())
                    break
                bounded.append(normalized)
                total_length += len(normalized)
                continue
            if len(normalized) + 1 > remaining:
                break
            bounded.append(normalized)
            total_length += len(normalized) + 1
        return " ".join(bounded)

    def _infer_authority_level(self, url: str) -> str:
        """Infer authority level from URL domain.

        Returns values from the shared authority vocabulary:
        statute > regulation > guidance > commentary.
        """
        url_lower = url.lower()

        # KRA and Kenya Law are primary legal authorities → statute
        if "kra.go.ke" in url_lower or "kenyalaw.org" in url_lower:
            return "statute"

        # KESRA (Kenya School of Revenue Administration) → regulation
        if "kesra.ac.ke" in url_lower:
            return "regulation"

        # PwC Kenya tax publications → guidance
        if "pwc.com" in url_lower:
            return "guidance"

        # Fallback for any domain that slips through (should not occur with
        # include_domains restriction active)
        return "commentary"

    def _is_trusted_domain(self, url: str) -> bool:
        """Return True only when the URL originates from a trusted domain."""
        url_lower = url.lower()
        return any(domain in url_lower for domain in self._TRUSTED_DOMAINS)

    def _extract_domain(self, url: str) -> str:
        """Extract domain name from URL."""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            return domain or "unknown"
        except Exception:
            return "unknown"
