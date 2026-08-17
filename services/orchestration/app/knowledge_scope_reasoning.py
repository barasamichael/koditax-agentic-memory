"""Deterministic, testable scope reasoning for grounded knowledge evidence."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
from __future__ import annotations

import re
from typing import TypedDict
from collections.abc import Mapping

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "month",
    "of",
    "on",
    "please",
    "someone",
    "tax",
    "the",
    "to",
    "what",
    "when",
    "who",
    "with",
    "you",
}

_DOMAIN_MARKERS: dict[str, tuple[str, ...]] = {
    "vat": ("vat", "value added tax"),
    "income_tax": ("income tax", "income tax act", "individual income tax"),
    "health_contribution": ("health contribution", "shif", "sha"),
    "paye_generalized": ("paye", "pay as you earn", "income tax"),
    "withholding_tax_generalized": ("withholding tax",),
    "business_income_generalized": (
        "business income tax",
        "turnover tax",
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
        "office at home",
        "rent",
        "electricity",
        "internet",
    ),
    "rental_income_generalized": ("rental income tax",),
}

_CONCEPT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "filing deadline": ("filing deadline", "return due date", "return deadline", "due date"),
    "paye bands": ("paye bands", "graduated tax rates", "tax bands", "tax bracket"),
    "tax band": ("tax band", "tax bands", "graduated tax rates", "tax bracket"),
    "deadline": ("deadline", "due date", "filing date"),
    "rate": ("rate", "rates", "percentage", "percentage band"),
    "income tax": ("income tax", "individual income tax", "employment income tax"),
    "paye": ("paye", "pay as you earn", "individual income tax"),
    "kra": ("kra", "kenya revenue authority"),
}


class ScopeAnalysis(TypedDict):
    """Represent deterministic scope analysis for one evidence item."""

    source_id: str | None
    source_version_id: str | None
    anchor_id: str | None
    title: str
    declared_tax_domain: str
    requested_tax_domain: str
    resolved_entity: str | None
    normalized_entity: str | None
    domain_markers: list[str]
    title_marker_matches: list[int]
    title_entity_matches: list[int]
    marker_passage_indices: list[int]
    entity_passage_indices: list[int]
    matching_passage_indices: list[int]
    canonical_claim_count: int
    decision: str
    diagnostic: str


def analyze_evidence_scope(
    item: Mapping[str, object],
    *,
    tax_domain_hint: str | None,
    resolved_entity: str | None = None,
    query_text: str | None = None,
) -> ScopeAnalysis:
    """Analyze one evidence item for domain and subject relevance."""

    declared_tax_domain = _string_value(item.get("tax_domain"))
    requested_tax_domain = tax_domain_hint or declared_tax_domain
    title = _string_value(item.get("title"))
    content = _evidence_body_text(item)
    passages = _split_passages("\n".join(part for part in (title, content) if part.strip()))
    domain_markers = list(_domain_markers_for(requested_tax_domain))
    normalized_entity = _normalize_scope_text(resolved_entity) if resolved_entity else None
    normalized_query = _normalize_scope_text(query_text) if query_text else None
    query_terms = _meaningful_terms(normalized_query)
    entity_terms = _subject_terms(normalized_entity)
    canonical_claims = item.get("canonical_claims")
    canonical_claim_count = len(canonical_claims) if isinstance(canonical_claims, list) else 0
    canonical_text = _canonical_claim_text(canonical_claims)

    title_marker_matches = _passage_matches(title, domain_markers)
    marker_passage_indices = [
        index
        for index, passage in enumerate(passages)
        if any(marker in passage.lower() for marker in domain_markers)
        or _normalize_scope_text(declared_tax_domain) in _normalize_scope_text(passage)
    ]
    title_entity_matches = _passage_matches(title, list(entity_terms))
    entity_passage_indices = [
        index
        for index, passage in enumerate(passages)
        if _concept_matches(passage, entity_terms, query_terms)
        or _concept_matches(passage, query_terms, entity_terms)
        or _concept_matches(passage, _claim_terms(canonical_text), ())
    ]

    declared_domain_match = (
        bool(tax_domain_hint)
        and declared_tax_domain == tax_domain_hint
        and bool(content.strip() or title.strip())
    )
    title_domain_match = bool(title_marker_matches)
    body_domain_match = any(
        any(marker in passage.lower() for marker in domain_markers) for passage in passages[1:]
    )
    canonical_domain_match = bool(canonical_text) and any(
        marker in canonical_text.lower() for marker in domain_markers
    )
    official_source_match = _is_official_source(item) and declared_domain_match
    domain_aligned = any(
        (
            declared_domain_match,
            title_domain_match,
            body_domain_match,
            canonical_domain_match,
            official_source_match,
        )
    )

    subject_relevant = False
    subject_matches_any_passage = False
    if entity_terms:
        subject_matches_any_passage = any(
            _concept_matches(passage, entity_terms, query_terms)
            or _concept_matches(passage, query_terms, entity_terms)
            or _concept_matches(passage, _claim_terms(canonical_text), ())
            for passage in passages
        )
        subject_relevant = subject_matches_any_passage or _concept_matches(
            canonical_text,
            entity_terms,
            query_terms,
        )
    elif query_terms:
        subject_matches_any_passage = any(
            _token_overlap(passage, query_terms) >= 1 for passage in passages
        )
        subject_relevant = subject_matches_any_passage or bool(
            canonical_text and _token_overlap(canonical_text, query_terms) >= 1
        )
    else:
        subject_relevant = bool(content.strip() or canonical_text)

    matching_passage_indices = sorted(
        set(marker_passage_indices).intersection(entity_passage_indices)
        or set(marker_passage_indices)
        or set(entity_passage_indices)
    )

    if not domain_aligned and not subject_relevant:
        decision = "rejected"
        if not title.strip() and not content.strip():
            diagnostic = "rejected_empty_content_and_wrong_declared_domain"
        elif declared_tax_domain and declared_tax_domain != requested_tax_domain:
            diagnostic = "rejected_no_domain_signal"
        else:
            diagnostic = "rejected_no_subject_relevance"
    elif domain_aligned and not subject_relevant:
        if canonical_claim_count > 0:
            decision = "retained"
            diagnostic = "retained_declared_domain_and_canonical_claim"
        else:
            decision = "rejected"
            diagnostic = "rejected_no_subject_relevance"
    elif subject_relevant and not domain_aligned:
        decision = "rejected"
        diagnostic = "rejected_no_domain_signal"
    else:
        decision = "retained"
        if title_domain_match and entity_passage_indices:
            diagnostic = "retained_title_domain_body_entity"
        elif marker_passage_indices and entity_passage_indices:
            diagnostic = "retained_adjacent_passage_match"
        elif canonical_claim_count > 0:
            diagnostic = "retained_declared_domain_and_canonical_claim"
        else:
            diagnostic = "retained_domain_marker_entity_not_required"

    return ScopeAnalysis(
        source_id=_string_value(item.get("source_id")) or None,
        source_version_id=_string_value(item.get("source_version_id")) or None,
        anchor_id=_string_value(item.get("anchor_id")) or None,
        title=title,
        declared_tax_domain=declared_tax_domain,
        requested_tax_domain=requested_tax_domain,
        resolved_entity=resolved_entity,
        normalized_entity=normalized_entity,
        domain_markers=list(domain_markers),
        title_marker_matches=title_marker_matches,
        title_entity_matches=title_entity_matches,
        marker_passage_indices=marker_passage_indices,
        entity_passage_indices=entity_passage_indices,
        matching_passage_indices=matching_passage_indices,
        canonical_claim_count=canonical_claim_count,
        decision=decision,
        diagnostic=diagnostic,
    )


def _domain_markers_for(tax_domain_hint: str | None) -> tuple[str, ...]:
    if not tax_domain_hint:
        return ()
    return _DOMAIN_MARKERS.get(tax_domain_hint, (tax_domain_hint.lower(),))


def _passage_matches(text: str, markers: list[str]) -> list[int]:
    if not text or not markers:
        return []
    return [0] if any(marker in text.lower() for marker in markers) else []


def _split_passages(text: str) -> list[str]:
    passages = [chunk.strip() for chunk in re.split(r"\n{1,}|(?<=[.!?])\s+", text) if chunk.strip()]
    return passages or ([text] if text else [])


def _normalize_scope_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = value.lower().replace("’", "'").replace("-", " ")
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _meaningful_terms(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(term for term in value.split() if term not in _STOPWORDS)


def _subject_terms(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    normalized = _normalize_scope_text(value)
    tokens = [token for token in normalized.split() if token not in _STOPWORDS]
    expanded: list[str] = []
    for token in tokens:
        expanded.extend(_CONCEPT_SYNONYMS.get(token, (token,)))
    expanded.extend(_CONCEPT_SYNONYMS.get(normalized, (normalized,)))
    return tuple(dict.fromkeys(term for term in expanded if term))


def _concept_matches(text: str, primary_terms: tuple[str, ...], secondary_terms: tuple[str, ...]) -> bool:
    normalized = _normalize_scope_text(text)
    if not normalized:
        return False
    for term in primary_terms:
        if term and term in normalized:
            return True
    for term in secondary_terms:
        if term and term in normalized:
            return True
    return False


def _token_overlap(text: str, terms: tuple[str, ...]) -> int:
    normalized = _normalize_scope_text(text)
    if not normalized:
        return 0
    tokens = set(normalized.split())
    return sum(1 for term in terms if term in tokens or term in normalized)


def _canonical_claim_text(canonical_claims: object) -> str:
    if not isinstance(canonical_claims, list):
        return ""
    text_parts: list[str] = []
    for claim in canonical_claims:
        if not isinstance(claim, Mapping):
            continue
        for key in ("claim_text", "canonical_text", "title", "content", "summary"):
            value = claim.get(key)
            if isinstance(value, str) and value.strip():
                text_parts.append(value.strip())
    return " ".join(text_parts)


def _claim_terms(text: str) -> tuple[str, ...]:
    return tuple(term for term in _meaningful_terms(_normalize_scope_text(text)) if term)


def _evidence_body_text(item: Mapping[str, object]) -> str:
    parts: list[str] = []
    for field in ("content", "content_excerpt"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _is_official_source(item: Mapping[str, object]) -> bool:
    source_type = _normalize_scope_text(_string_value(item.get("source_type")))
    authority = _normalize_scope_text(_string_value(item.get("authority_level")))
    url = _normalize_scope_text(_string_value(item.get("url")))
    return "official" in source_type or "official" in authority or "kra.go.ke" in url


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
