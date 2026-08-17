"""Render deterministic explanation blocks from governed grounded knowledge evidence."""

from __future__ import annotations

from typing import TypedDict
from collections.abc import Mapping
from collections.abc import Sequence
from services.orchestration.app.request_timer import timed_print

AUTHORITY_RANK = {
    "statute": 0,
    "regulation": 1,
    "guidance": 2,
    "commentary": 3,
}

TEMPORAL_RANK = {
    "timeline-multi-period": 0,
    "current-effective": 0,
    "tax-year-scoped": 1,
    "historical-effective": 2,
}


class GroundedExplanationItem(TypedDict):
    """Represent one deterministic explanation item tied to evidence identifiers."""

    explanation_text: str
    source_id: str
    source_version_id: str
    anchor_id: str
    authority_level: str
    source_type: str
    temporal_applicability: str


class GroundedExplanationCitation(TypedDict):
    """Represent one deterministic citation block for grounded explanation rendering."""

    citation_index: int
    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    source_type: str
    authority_level: str
    tax_domain: str
    temporal_applicability: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None


class GroundedAuthoritySummary(TypedDict):
    """Represent deterministic authority summary for one grounded explanation block."""

    highest_authority_level: str
    source_types: list[str]
    citation_count: int


class GroundedTemporalApplicability(TypedDict):
    """Represent deterministic temporal disclosure for one grounded explanation block."""

    scope: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    disclosure_text: str


class GroundedExplanationPayload(TypedDict):
    """Represent the deterministic explanation payload attached to orchestration output."""

    explanation_status: str
    explanation_items: list[GroundedExplanationItem]
    citations: list[GroundedExplanationCitation]
    authority_summary: GroundedAuthoritySummary
    temporal_applicability: GroundedTemporalApplicability


class GroundedExplanationError(ValueError):
    """Represent deterministic explanation-rendering failures."""

    def __init__(self, *, error_code: str, message: str, reason: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason


def render_grounded_explanation(
    *,
    grounded_evidence: Sequence[Mapping[str, object]],
) -> GroundedExplanationPayload:
    """Render a deterministic explanation block from governed grounded evidence."""

    if not grounded_evidence:
        raise GroundedExplanationError(
            error_code="unsupported_prompt_scope",
            message="Grounded evidence is insufficient for deterministic explanation rendering.",
            reason="insufficient_grounded_evidence",
        )

    timed_print(
        "[GROUNDING] About to render grounded explanation "
        f"evidence_count={len(grounded_evidence)}"
    )
    normalized = [_normalize_evidence_item(item) for item in grounded_evidence]
    _assert_explanation_safe(normalized)

    citations = sorted(normalized, key=_citation_sort_key)
    citation_items: list[GroundedExplanationCitation] = []
    explanation_items: list[GroundedExplanationItem] = []
    for index, citation in enumerate(citations, start=1):
        citation_items.append(
            {
                "citation_index": index,
                "source_id": citation["source_id"],
                "source_version_id": citation["source_version_id"],
                "anchor_id": citation["anchor_id"],
                "title": citation["title"],
                "url": citation["url"],
                "source_type": citation["source_type"],
                "authority_level": citation["authority_level"],
                "tax_domain": citation["tax_domain"],
                "temporal_applicability": citation["temporal_applicability"],
                "effective_from": citation["effective_from"],
                "effective_to": citation["effective_to"],
                "tax_year": citation["tax_year"],
            }
        )
        explanation_items.append(
            {
                "explanation_text": _build_explanation_text(citation),
                "source_id": citation["source_id"],
                "source_version_id": citation["source_version_id"],
                "anchor_id": citation["anchor_id"],
                "authority_level": citation["authority_level"],
                "source_type": citation["source_type"],
                "temporal_applicability": citation["temporal_applicability"],
            }
        )

    first = citations[0]
    source_types = sorted({item["source_type"] for item in citations})
    temporal_scope = _overall_temporal_scope(citations)
    temporal_disclosure = _temporal_disclosure_text(first, temporal_scope)
    # Authority summary reflects the highest authority across the full evidence set.
    highest_authority_rank = min(
        AUTHORITY_RANK.get(item["authority_level"], 99) for item in citations
    )
    highest_authority_level = next(
        item["authority_level"]
        for item in citations
        if AUTHORITY_RANK.get(item["authority_level"], 99) == highest_authority_rank
    )
    timed_print(
        "[GROUNDING] Rendered grounded explanation "
        f"citation_count={len(citations)}"
    )
    return {
        "explanation_status": "grounded",
        "explanation_items": explanation_items,
        "citations": citation_items,
        "authority_summary": {
            "highest_authority_level": highest_authority_level,
            "source_types": source_types,
            "citation_count": len(citations),
        },
        "temporal_applicability": {
            "scope": temporal_scope,
            "effective_from": first["effective_from"],
            "effective_to": first["effective_to"],
            "tax_year": first["tax_year"],
            "disclosure_text": temporal_disclosure,
        },
    }


class _NormalizedEvidence(TypedDict):
    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    source_type: str
    authority_level: str
    tax_domain: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    publication_state: str
    temporal_applicability: str
    content: str | None
    knowledge_route_mode: str
    timeline_position: int | None


def _normalize_evidence_item(item: Mapping[str, object]) -> _NormalizedEvidence:
    source_id = _require_non_empty_string(item, "source_id")
    # Web search results have no source_version_id — fall back to source_id so
    # downstream citation rendering always has a non-empty reference string.
    source_version_id = _optional_string(item.get("source_version_id")) or source_id
    anchor_id = _require_non_empty_string(item, "anchor_id")
    title = _require_non_empty_string(item, "title")
    url = _require_non_empty_string(item, "url")
    source_type = _require_non_empty_string(item, "source_type")
    authority_level = _require_non_empty_string(item, "authority_level")
    tax_domain = _require_non_empty_string(item, "tax_domain")
    # Web search results carry no effective_from date — default to "unknown".
    effective_from = _optional_string(item.get("effective_from")) or "unknown"
    effective_to = _optional_string(item.get("effective_to"))
    tax_year = _optional_int(item.get("tax_year"))
    # Web search results have no publication_state — treat as "published" since
    # live pages are currently accessible and not superseded.
    publication_state = _optional_string(item.get("publication_state")) or "published"
    # Accept either "content" (corpus) or "content_excerpt" (web search fallback).
    raw_content = item.get("content") or item.get("content_excerpt")
    content = raw_content.strip() if isinstance(raw_content, str) and raw_content.strip() else None
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
        "temporal_applicability": _temporal_applicability(
            publication_state=publication_state,
            effective_to=effective_to,
            tax_year=tax_year,
            knowledge_route_mode=_optional_string(item.get("knowledge_route_mode")) or "search",
        ),
        "content": content,
        "knowledge_route_mode": _optional_string(item.get("knowledge_route_mode")) or "search",
        "timeline_position": _optional_int(item.get("timeline_position")),
    }


def _assert_explanation_safe(citations: Sequence[_NormalizedEvidence]) -> None:
    tax_domains = {item["tax_domain"] for item in citations}
    if len(tax_domains) != 1:
        raise GroundedExplanationError(
            error_code="unsupported_prompt_scope",
            message="Grounded evidence spans unsupported mixed tax-domain scope.",
            reason="conflicting_grounded_evidence",
        )
    publication_states = {item["publication_state"] for item in citations}
    if not publication_states.issubset({"published", "superseded"}):
        raise GroundedExplanationError(
            error_code="unsupported_prompt_scope",
            message="Grounded explanation requires published governed evidence.",
            reason="insufficient_grounded_evidence",
        )
    # Mixed temporal scope is now handled by priority selection in _overall_temporal_scope
    # rather than rejected outright — valid tax queries can span multiple regimes.


def _build_explanation_text(citation: _NormalizedEvidence) -> str:
    """Build a meaningful explanation text using the evidence content where available."""
    base = (
        f"{citation['title']} ({citation['authority_level']}-level {citation['source_type']}, "
        f"{citation['temporal_applicability']})"
    )
    timeline_position = citation.get("timeline_position")
    if timeline_position is not None:
        base = f"Timeline item {timeline_position}: {base}"
    content = citation.get("content")
    if content:
        excerpt = content[:5000].rstrip()
        if len(content) > 5000:
            excerpt += "…"
        return f"{base}: {excerpt}"
    return f"{base}."


def _temporal_applicability(
    *,
    publication_state: str,
    effective_to: str | None,
    tax_year: int | None,
    knowledge_route_mode: str,
) -> str:
    if knowledge_route_mode == "timeline_search":
        return "timeline-multi-period"
    if tax_year is not None:
        return "tax-year-scoped"
    if publication_state == "superseded" or effective_to is not None:
        return "historical-effective"
    return "current-effective"


def _overall_temporal_scope(citations: Sequence[_NormalizedEvidence]) -> str:
    """Resolve the dominant temporal scope by priority: tax-year-scoped > historical > current."""
    if any(item["knowledge_route_mode"] == "timeline_search" for item in citations):
        return "timeline-multi-period"
    scopes = {item["temporal_applicability"] for item in citations}
    if len(scopes) == 1:
        return next(iter(scopes))
    # Prefer the most specific scope present.
    for preferred in (
        "tax-year-scoped",
        "historical-effective",
        "current-effective",
    ):
        if preferred in scopes:
            return preferred
    return "current-effective"


def _temporal_disclosure_text(citation: _NormalizedEvidence, temporal_scope: str) -> str:
    if temporal_scope == "timeline-multi-period":
        return "Grounded explanation preserves chronology across multiple governed legal windows."
    if temporal_scope == "tax-year-scoped":
        assert citation["tax_year"] is not None
        return f"Grounded explanation is tax-year-scoped for tax year {citation['tax_year']}."
    if temporal_scope == "historical-effective":
        effective_to = citation["effective_to"]
        if effective_to is None:
            return "Grounded explanation is historical-effective."
        return (
            "Grounded explanation is historical-effective from "
            f"{citation['effective_from']} to {effective_to}."
        )
    return f"Grounded explanation is current-effective from {citation['effective_from']}."


def _require_non_empty_string(item: Mapping[str, object], field_name: str) -> str:
    value = item.get(field_name)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    raise GroundedExplanationError(
        error_code="unsupported_prompt_scope",
        message="Grounded evidence is insufficient for deterministic explanation rendering.",
        reason="insufficient_grounded_evidence",
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    raise GroundedExplanationError(
        error_code="unsupported_prompt_scope",
        message="Grounded evidence is insufficient for deterministic explanation rendering.",
        reason="insufficient_grounded_evidence",
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise GroundedExplanationError(
        error_code="unsupported_prompt_scope",
        message="Grounded evidence is insufficient for deterministic explanation rendering.",
        reason="insufficient_grounded_evidence",
    )


def _citation_sort_key(item: _NormalizedEvidence) -> tuple[object, ...]:
    if item["knowledge_route_mode"] == "timeline_search":
        return (
            item["timeline_position"] if item["timeline_position"] is not None else 10_000,
            AUTHORITY_RANK.get(item["authority_level"], 99),
            item["source_id"],
            item["source_version_id"],
            item["anchor_id"],
        )
    return (
        AUTHORITY_RANK.get(item["authority_level"], 99),
        TEMPORAL_RANK.get(item["temporal_applicability"], 99),
        item["source_id"],
        item["source_version_id"],
        item["anchor_id"],
    )
