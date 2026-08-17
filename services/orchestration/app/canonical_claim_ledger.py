"""Canonical claim ledger for governed evidence contradiction analysis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any
from typing import Literal
from typing import Protocol
from typing import cast

from openai import APIConnectionError
from openai import APIError
from openai import APIStatusError
from openai import APITimeoutError
from openai import OpenAI
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from services.knowledge.app.repository import KnowledgeSearchRecord
from services.orchestration.app.config import load_orchestration_openai_response_synthesis_config
from services.orchestration.app.value_normalization import NormalizedValue
from services.orchestration.app.value_normalization import canonical_text
from services.orchestration.app.value_normalization import convert_frequency_value
from services.orchestration.app.value_normalization import parse_tax_year_text

CLAIM_SCHEMA_VERSION = "2026-07-26"
CLAIM_EXTRACTION_METHOD_VERSION = "2026-07-26"
CLAIM_RELATIONSHIP_JUDGE_VERSION = "2026-07-26"
_OPENAI_MAX_TOKENS = 1200

ClaimPredicate = Literal[
    "tax_rate",
    "contribution_rate",
    "threshold",
    "exemption",
    "deduction",
    "filing_deadline",
    "effective_date",
    "liability",
    "tax_base",
    "withholding_obligation",
    "employer_obligation",
    "employee_obligation",
    "eligibility",
    "prohibition",
    "reporting_requirement",
    "documentation_requirement",
    "status",
    "definition",
    "exception",
    "other",
]

ClaimPolarity = Literal[
    "affirms",
    "denies",
    "prohibits",
    "exempts",
    "conditionally_permits",
    "conditionally_requires",
    "neutral",
]

ClaimEntityType = Literal[
    "taxpayer",
    "individual",
    "resident_individual",
    "non_resident_individual",
    "employer",
    "employee",
    "business",
    "spouse",
    "regime",
    "document_subject",
    "unknown",
]

ClaimSourceTrustStatus = Literal[
    "verified_official_source",
    "verified_professional_source",
    "unverified_web_source",
    "unknown",
]

JurisdictionStatus = Literal["verified", "inferred_unverified", "absent", "conflicting"]

ClaimRelationship = Literal[
    "equivalent",
    "complementary",
    "narrower_than",
    "broader_than",
    "supersedes",
    "superseded_by",
    "conditionally_different",
    "contradictory",
    "insufficiently_comparable",
    "duplicate",
    "unresolved",
]

ClaimComparisonStatus = Literal[
    "exact_match",
    "normalized_match",
    "compatible_difference",
    "normalized_conflict",
    "incompatible",
    "unknown",
]


class ClaimLedgerError(RuntimeError):
    """Represent canonical claim ledger failures."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.context = context


class ClaimProvenance(BaseModel):
    """Represent trusted provenance for one grounded claim."""

    model_config = ConfigDict(extra="forbid")

    record_id: str | None = None
    source_id: str
    source_version_id: str | None = None
    anchor_id: str | None = None
    url: str | None = None
    title: str | None = None
    source_type: str | None = None
    retrieval_provider: str | None = None
    tavily_result_id: str | None = None
    excerpt_ref: str | None = None
    evidence_sequence: int = 0
    correlation_id: str | None = None
    trace_id: str | None = None
    authority_level: str | None = None
    jurisdiction: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    tax_year: int | None = None
    publication_state: str | None = None
    source_trust_status: ClaimSourceTrustStatus = "unknown"


class CanonicalClaimDraft(BaseModel):
    """Represent one model-extracted claim before deterministic identifiers are assigned."""

    model_config = ConfigDict(extra="forbid")

    entity_type: ClaimEntityType = "unknown"
    entity_label: str | None = None
    predicate: ClaimPredicate = "other"
    polarity: ClaimPolarity = "neutral"
    raw_value_text: str | None = None
    normalized_value: NormalizedValue = Field(default_factory=lambda: NormalizedValue(kind="unknown"))
    taxpayer_category: str | None = None
    tax_domain: str = "unknown"
    jurisdiction: str | None = None
    jurisdiction_status: JurisdictionStatus = "absent"
    effective_from: str | None = None
    effective_to: str | None = None
    tax_year: int | None = None
    period_type: str | None = None
    current_effective: bool = False
    historical_effective: bool = False
    authority_level: str = "unknown"
    source_type: str = "unknown"
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    claim_excerpt: str | None = None
    claim_topic: str | None = None
    extraction_confidence: float = 0.0
    source_trust_status: ClaimSourceTrustStatus = "unknown"
    provenance: ClaimProvenance | None = None


class CanonicalClaim(BaseModel):
    """Represent one deterministic grounded claim with provenance."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    claim_schema_version: str = CLAIM_SCHEMA_VERSION
    extraction_method_version: str = CLAIM_EXTRACTION_METHOD_VERSION
    source_claim_sequence: int = 0
    content_fingerprint: str = ""
    entity_type: ClaimEntityType = "unknown"
    entity_label: str | None = None
    predicate: ClaimPredicate = "other"
    polarity: ClaimPolarity = "neutral"
    raw_value_text: str | None = None
    normalized_value: NormalizedValue = Field(default_factory=lambda: NormalizedValue(kind="unknown"))
    taxpayer_category: str | None = None
    tax_domain: str = "unknown"
    jurisdiction: str | None = None
    jurisdiction_status: JurisdictionStatus = "absent"
    effective_from: str | None = None
    effective_to: str | None = None
    tax_year: int | None = None
    period_type: str | None = None
    current_effective: bool = False
    historical_effective: bool = False
    authority_level: str = "unknown"
    source_type: str = "unknown"
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    claim_excerpt: str | None = None
    claim_topic: str | None = None
    extraction_confidence: float = 0.0
    source_trust_status: ClaimSourceTrustStatus = "unknown"
    provenance: ClaimProvenance


class ClaimPairCandidate(BaseModel):
    """Represent one bounded candidate pair for relationship adjudication."""

    model_config = ConfigDict(extra="forbid")

    claim_a: CanonicalClaim
    claim_b: CanonicalClaim
    pair_reason: str
    deterministic_status: ClaimComparisonStatus
    overlapping_entity: bool
    overlapping_jurisdiction: bool
    overlapping_scope: bool
    overlapping_temporal_scope: bool


class ClaimRelationshipDecision(BaseModel):
    """Represent one model-adjudicated relationship between two claims."""

    model_config = ConfigDict(extra="forbid")

    claim_a_id: str
    claim_b_id: str
    relationship: ClaimRelationship
    materiality: Literal["high", "medium", "low"]
    confidence_class: Literal["high", "medium", "low", "abstain"]
    differing_dimensions: list[str] = Field(default_factory=list)
    compatible_dimensions: list[str] = Field(default_factory=list)
    normalized_value_comparison_status: ClaimComparisonStatus = "unknown"
    deterministic_validation_required: bool = True
    audit_safe_explanation: str
    clarification_or_evidence_requirement: str | None = None
    model_identifier: str | None = None


class CanonicalClaimExtractionResponse(BaseModel):
    """Represent a strict extraction response from one evidence record."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    coverage_status: Literal["complete", "partial", "abstained", "failed"] = "partial"
    claims: list[CanonicalClaimDraft] = Field(
        default_factory=lambda: cast(list[CanonicalClaimDraft], [])
    )
    unresolved_metadata_gaps: list[str] = Field(default_factory=list)
    extraction_confidence: float = 0.0
    model_identifier: str | None = None


class ClaimExtractionClient(Protocol):
    """Describe the minimum supported extraction client."""

    def extract(self, evidence: Mapping[str, object]) -> CanonicalClaimExtractionResponse: ...


def build_canonical_claims_from_evidence(
    evidence_records: Sequence[Mapping[str, object] | KnowledgeSearchRecord],
    *,
    client: OpenAI | None = None,
) -> list[CanonicalClaim]:
    """Return canonical claims for one set of evidence records."""

    normalized_records: list[dict[str, object]] = [
        _normalize_evidence_record(record, index) for index, record in enumerate(evidence_records)
    ]
    claims: list[CanonicalClaim] = []
    for record in normalized_records:
        canonical_claims = record.get("canonical_claims")
        if isinstance(canonical_claims, list):
            for claim_index, raw_claim in enumerate(cast(list[object], canonical_claims)):
                if not isinstance(raw_claim, Mapping):
                    continue
                mapped_raw_claim = cast(Mapping[str, object], raw_claim)
                claims.append(
                    _project_claim_from_mapping(
                        mapped_raw_claim,
                        record=record,
                        source_claim_sequence=claim_index,
                    )
                )
            continue
        extraction = extract_claims_from_evidence_record(record, client=client)
        claims.extend(extraction)
    return claims


def extract_claims_from_evidence_record(
    record: Mapping[str, object],
    *,
    client: OpenAI | None = None,
) -> list[CanonicalClaim]:
    """Extract canonical grounded claims from one evidence record."""

    if client is None:
        config = load_orchestration_openai_response_synthesis_config()
        if not config.configured:
            raise ClaimLedgerError(
                reason_code="claim_extraction_unavailable",
                message="Canonical claim extraction is not configured.",
                context={
                    "source_id": record.get("source_id"),
                    "source_type": record.get("source_type"),
                },
            )
        client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    response = _extract_claims_with_client(record=record, client=client)
    claims: list[CanonicalClaim] = []
    for index, draft in enumerate(response.claims):
        claims.append(
            _project_claim_from_draft(
                draft,
                record=record,
                source_claim_sequence=index,
                extraction_confidence=response.extraction_confidence or draft.extraction_confidence,
            )
        )
    return claims


def generate_candidate_pairs(
    claims: Sequence[CanonicalClaim],
    *,
    max_pairs: int = 24,
) -> list[ClaimPairCandidate]:
    """Return a bounded set of plausible claim pairs for relationship judgment."""

    candidates: list[ClaimPairCandidate] = []
    for index, claim_a in enumerate(claims):
        for claim_b in claims[index + 1 :]:
            if len(candidates) >= max_pairs:
                return candidates
            pair_reason = _pair_reason(claim_a, claim_b)
            if pair_reason is None:
                continue
            deterministic_status = _deterministic_claim_comparison_status(claim_a, claim_b)
            candidates.append(
                ClaimPairCandidate(
                    claim_a=claim_a,
                    claim_b=claim_b,
                    pair_reason=pair_reason,
                    deterministic_status=deterministic_status,
                    overlapping_entity=_claims_overlap_on_entity(claim_a, claim_b),
                    overlapping_jurisdiction=_claims_overlap_on_jurisdiction(claim_a, claim_b),
                    overlapping_scope=_claims_overlap_on_predicate(claim_a, claim_b),
                    overlapping_temporal_scope=_claims_overlap_on_time(claim_a, claim_b),
                )
            )
    return candidates


def judge_candidate_pair(
    candidate: ClaimPairCandidate,
    *,
    client: OpenAI | None = None,
) -> ClaimRelationshipDecision:
    """Return one bounded relationship decision for a candidate pair."""

    deterministic = _validate_deterministic_relationship(candidate)
    if deterministic is not None:
        return deterministic
    if client is None:
        config = load_orchestration_openai_response_synthesis_config()
        if not config.configured:
            return ClaimRelationshipDecision(
                claim_a_id=candidate.claim_a.claim_id,
                claim_b_id=candidate.claim_b.claim_id,
                relationship="unresolved",
                materiality="low",
                confidence_class="abstain",
                differing_dimensions=["model_unavailable"],
                compatible_dimensions=[],
                normalized_value_comparison_status=candidate.deterministic_status,
                deterministic_validation_required=True,
                audit_safe_explanation=(
                    "The claims are comparable, but no relationship model is configured and "
                    "deterministic validation was not sufficient to classify them."
                ),
                clarification_or_evidence_requirement="relationship_judge_unavailable",
                model_identifier=None,
            )
        client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )
    response = _judge_with_client(candidate=candidate, client=client)
    validated = _validate_model_relationship(candidate, response)
    if validated is not None:
        return validated
    return ClaimRelationshipDecision(
        claim_a_id=candidate.claim_a.claim_id,
        claim_b_id=candidate.claim_b.claim_id,
        relationship="unresolved",
        materiality="low",
        confidence_class="abstain",
        differing_dimensions=["invalid_model_result"],
        compatible_dimensions=[],
        normalized_value_comparison_status=candidate.deterministic_status,
        deterministic_validation_required=True,
        audit_safe_explanation="The relationship model returned an unsupported or incompatible result.",
        clarification_or_evidence_requirement="relationship_result_invalid",
        model_identifier=getattr(response, "model_identifier", None),
    )


def claim_pair_to_finding(candidate: ClaimPairCandidate, decision: ClaimRelationshipDecision) -> dict[str, str] | None:
    """Project a validated pair decision into the legacy contradiction finding shape."""

    if decision.relationship != "contradictory":
        return None
    return {
        "claim_topic": candidate.claim_a.claim_topic or candidate.claim_a.predicate,
        "source_a_id": candidate.claim_a.provenance.source_id,
        "source_a_value": _claim_value_label(candidate.claim_a),
        "source_b_id": candidate.claim_b.provenance.source_id,
        "source_b_value": _claim_value_label(candidate.claim_b),
    }


def _normalize_evidence_record(
    record: Mapping[str, object] | KnowledgeSearchRecord,
    index: int,
) -> dict[str, object]:
    if isinstance(record, KnowledgeSearchRecord):
        payload = record.to_public_payload()
        payload["content"] = record.content
        payload["record_id"] = None
        payload["retrieval_provider"] = "governed_repository"
        payload["canonical_claims"] = list(record.canonical_claims) if record.canonical_claims is not None else None
        payload["evidence_sequence"] = index
        return payload
    normalized = dict(record)
    normalized.setdefault("evidence_sequence", index)
    normalized.setdefault("retrieval_provider", normalized.get("retrieval_provider", "unknown"))
    normalized.setdefault("canonical_claims", normalized.get("canonical_claims"))
    return normalized


def _project_claim_from_mapping(
    raw_claim: Mapping[str, object],
    *,
    record: Mapping[str, object],
    source_claim_sequence: int,
) -> CanonicalClaim:
    draft = CanonicalClaimDraft.model_validate(raw_claim)
    return _project_claim_from_draft(
        draft,
        record=record,
        source_claim_sequence=source_claim_sequence,
        extraction_confidence=draft.extraction_confidence,
    )


def _project_claim_from_draft(
    draft: CanonicalClaimDraft,
    *,
    record: Mapping[str, object],
    source_claim_sequence: int,
    extraction_confidence: float,
) -> CanonicalClaim:
    provenance = draft.provenance or _build_claim_provenance(record=record, extraction_sequence=source_claim_sequence)
    claim_without_id = draft.model_dump(mode="python")
    claim_without_id.pop("provenance", None)
    content_fingerprint = _claim_content_fingerprint(
        record=record,
        draft=draft,
        source_claim_sequence=source_claim_sequence,
    )
    claim_id = _claim_id(
        provenance=provenance,
        claim_topic=draft.claim_topic or draft.predicate,
        source_claim_sequence=source_claim_sequence,
        content_fingerprint=content_fingerprint,
        normalized_value=draft.normalized_value,
        predicate=draft.predicate,
        entity_type=draft.entity_type,
        tax_domain=draft.tax_domain,
        tax_year=draft.tax_year,
    )
    return CanonicalClaim(
        claim_id=claim_id,
        source_claim_sequence=source_claim_sequence,
        content_fingerprint=content_fingerprint,
        entity_type=draft.entity_type,
        entity_label=draft.entity_label,
        predicate=draft.predicate,
        polarity=draft.polarity,
        raw_value_text=draft.raw_value_text,
        normalized_value=draft.normalized_value,
        taxpayer_category=draft.taxpayer_category,
        tax_domain=draft.tax_domain,
        jurisdiction=draft.jurisdiction,
        jurisdiction_status=draft.jurisdiction_status,
        effective_from=draft.effective_from or provenance.effective_from,
        effective_to=draft.effective_to or provenance.effective_to,
        tax_year=draft.tax_year or provenance.tax_year,
        period_type=draft.period_type,
        current_effective=draft.current_effective,
        historical_effective=draft.historical_effective,
        authority_level=draft.authority_level or provenance.authority_level or "unknown",
        source_type=draft.source_type or provenance.source_type or "unknown",
        conditions=list(draft.conditions),
        exceptions=list(draft.exceptions),
        claim_excerpt=draft.claim_excerpt,
        claim_topic=draft.claim_topic,
        extraction_confidence=extraction_confidence,
        source_trust_status=draft.source_trust_status or provenance.source_trust_status,
        provenance=provenance,
    )


def _build_claim_provenance(
    *,
    record: Mapping[str, object],
    extraction_sequence: int,
) -> ClaimProvenance:
    source_id = str(record.get("source_id") or record.get("record_id") or f"source-{extraction_sequence}")
    source_version_id = record.get("source_version_id")
    anchor_id = record.get("anchor_id")
    url = record.get("url")
    title = record.get("title")
    source_type = record.get("source_type")
    authority_level = record.get("authority_level")
    effective_from = record.get("effective_from")
    effective_to = record.get("effective_to")
    tax_year = record.get("tax_year")
    source_trust_status = _source_trust_status(record)
    tax_year_value: int | None = None
    if isinstance(tax_year, int):
        tax_year_value = tax_year
    elif tax_year is not None:
        tax_year_value = parse_tax_year_text(str(tax_year))
    evidence_sequence = record.get("evidence_sequence")
    if isinstance(evidence_sequence, int):
        evidence_sequence_value = evidence_sequence
    else:
        evidence_sequence_value = extraction_sequence
    return ClaimProvenance(
        record_id=str(record.get("record_id")) if record.get("record_id") is not None else None,
        source_id=source_id,
        source_version_id=str(source_version_id) if isinstance(source_version_id, str) else None,
        anchor_id=str(anchor_id) if isinstance(anchor_id, str) else None,
        url=str(url) if isinstance(url, str) else None,
        title=str(title) if isinstance(title, str) else None,
        source_type=str(source_type) if isinstance(source_type, str) else None,
        retrieval_provider=str(record.get("retrieval_provider")) if isinstance(record.get("retrieval_provider"), str) else None,
        tavily_result_id=str(record.get("tavily_result_id")) if isinstance(record.get("tavily_result_id"), str) else None,
        excerpt_ref=str(record.get("excerpt_ref")) if isinstance(record.get("excerpt_ref"), str) else None,
        evidence_sequence=evidence_sequence_value,
        correlation_id=str(record.get("correlation_id")) if isinstance(record.get("correlation_id"), str) else None,
        trace_id=str(record.get("trace_id")) if isinstance(record.get("trace_id"), str) else None,
        authority_level=str(authority_level) if isinstance(authority_level, str) else None,
        jurisdiction=str(record.get("jurisdiction")) if isinstance(record.get("jurisdiction"), str) else None,
        effective_from=str(effective_from) if isinstance(effective_from, str) else None,
        effective_to=str(effective_to) if isinstance(effective_to, str) else None,
        tax_year=tax_year_value,
        publication_state=str(record.get("publication_state")) if isinstance(record.get("publication_state"), str) else None,
        source_trust_status=source_trust_status,
    )


def _source_trust_status(record: Mapping[str, object]) -> ClaimSourceTrustStatus:
    source_type = canonical_text(record.get("source_type"))
    url = canonical_text(record.get("url"))
    authority_level = canonical_text(record.get("authority_level"))
    if source_type in {"tax_law", "regulation"} or authority_level in {"statute", "regulation"}:
        return "verified_official_source"
    if "tavily" in canonical_text(record.get("retrieval_provider")) or url.startswith("http"):
        return "unverified_web_source"
    return "unknown"


def _claim_content_fingerprint(
    *,
    record: Mapping[str, object],
    draft: CanonicalClaimDraft,
    source_claim_sequence: int,
) -> str:
    payload = {
        "source_id": record.get("source_id"),
        "source_version_id": record.get("source_version_id"),
        "anchor_id": record.get("anchor_id"),
        "claim_topic": draft.claim_topic,
        "entity_type": draft.entity_type,
        "entity_label": draft.entity_label,
        "predicate": draft.predicate,
        "polarity": draft.polarity,
        "raw_value_text": draft.raw_value_text,
        "normalized_value": asdict(draft.normalized_value),
        "tax_domain": draft.tax_domain,
        "tax_year": draft.tax_year,
        "effective_from": draft.effective_from,
        "effective_to": draft.effective_to,
        "source_claim_sequence": source_claim_sequence,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _claim_id(
    *,
    provenance: ClaimProvenance,
    claim_topic: str,
    source_claim_sequence: int,
    content_fingerprint: str,
    normalized_value: NormalizedValue,
    predicate: ClaimPredicate,
    entity_type: ClaimEntityType,
    tax_domain: str,
    tax_year: int | None,
) -> str:
    payload = {
        "source_id": provenance.source_id,
        "source_version_id": provenance.source_version_id,
        "anchor_id": provenance.anchor_id,
        "claim_topic": claim_topic,
        "source_claim_sequence": source_claim_sequence,
        "content_fingerprint": content_fingerprint,
        "normalized_value": normalized_value.comparison_key(),
        "predicate": predicate,
        "entity_type": entity_type,
        "tax_domain": tax_domain,
        "tax_year": tax_year,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"claim-{digest[:24]}"


def _extract_claims_with_client(
    *,
    record: Mapping[str, object],
    client: OpenAI,
) -> CanonicalClaimExtractionResponse:
    system_prompt = (
        "You are a governed claim extractor for Kenyan tax evidence. "
        "Extract only claims explicitly supported by the provided evidence. "
        "Do not invent source metadata or trusted authority. "
        "Return only the structured schema."
    )
    user_prompt = _build_extraction_prompt(record)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error: Exception | None = None
    for _ in range(1):
        try:
            parsed = client.chat.completions.parse(
                model=cast(str, load_orchestration_openai_response_synthesis_config().model),
                messages=cast(Any, messages),
                temperature=0.0,
                max_completion_tokens=_OPENAI_MAX_TOKENS,
                response_format=CanonicalClaimExtractionResponse,
            )
            choice = parsed.choices[0] if parsed.choices else None
            message = getattr(choice, "message", None) if choice is not None else None
            response = getattr(message, "parsed", None) if message is not None else None
            if isinstance(response, CanonicalClaimExtractionResponse):
                return response
            raise ClaimLedgerError(
                reason_code="claim_extraction_failed",
                message="Structured claim extraction returned an empty response.",
                context={"source_id": record.get("source_id")},
            )
        except (APITimeoutError, APIConnectionError, APIStatusError, APIError) as error:
            last_error = error
            break
    raise ClaimLedgerError(
        reason_code="claim_extraction_failed",
        message="Structured claim extraction failed.",
        context={"source_id": record.get("source_id"), "error_type": type(last_error).__name__ if last_error else None},
    ) from last_error


def _build_extraction_prompt(record: Mapping[str, object]) -> str:
    evidence_payload = {
        key: record.get(key)
        for key in (
            "source_id",
            "source_version_id",
            "anchor_id",
            "title",
            "url",
            "source_type",
            "authority_level",
            "effective_from",
            "effective_to",
            "tax_year",
            "retrieval_provider",
            "tavily_result_id",
            "jurisdiction",
            "publication_state",
            "content",
        )
    }
    return (
        "Extract canonical claims from this evidence record.\n"
        "Evidence JSON:\n"
        f"{json.dumps(evidence_payload, sort_keys=True, separators=(',', ':'))}\n\n"
        "Return only claims explicitly supported by the evidence.\n"
        "Do not invent source identifiers, dates, authority, or jurisdiction.\n"
        "If the evidence is insufficient, return an empty claims array.\n"
    )


def _judge_with_client(
    *,
    candidate: ClaimPairCandidate,
    client: OpenAI,
) -> ClaimRelationshipDecision:
    system_prompt = (
        "You are a governed claim-relationship judge for Kenyan tax evidence. "
        "Classify only the supplied pair. Do not invent claims or metadata. "
        "Return only the structured schema."
    )
    user_prompt = _build_judgment_prompt(candidate)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    parsed = client.chat.completions.parse(
        model=cast(str, load_orchestration_openai_response_synthesis_config().model),
        messages=cast(Any, messages),
        temperature=0.0,
        max_completion_tokens=_OPENAI_MAX_TOKENS,
        response_format=ClaimRelationshipDecision,
    )
    choice = parsed.choices[0] if parsed.choices else None
    message = getattr(choice, "message", None) if choice is not None else None
    response = getattr(message, "parsed", None) if message is not None else None
    if isinstance(response, ClaimRelationshipDecision):
        return response
    raise ClaimLedgerError(
        reason_code="claim_relationship_failed",
        message="Structured claim relationship judgment returned an empty response.",
        context={"claim_a_id": candidate.claim_a.claim_id, "claim_b_id": candidate.claim_b.claim_id},
    )


def _build_judgment_prompt(candidate: ClaimPairCandidate) -> str:
    payload = {
        "claim_a": candidate.claim_a.model_dump(mode="python"),
        "claim_b": candidate.claim_b.model_dump(mode="python"),
        "deterministic_status": candidate.deterministic_status,
        "overlap": {
            "entity": candidate.overlapping_entity,
            "jurisdiction": candidate.overlapping_jurisdiction,
            "scope": candidate.overlapping_scope,
            "temporal": candidate.overlapping_temporal_scope,
        },
        "pair_reason": candidate.pair_reason,
    }
    return (
        "Classify the semantic relationship between the two claims.\n"
        "Use only the supplied claims, overlap summary, and normalized values.\n"
        f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
    )


def _project_claim_relationship(
    *,
    candidate: ClaimPairCandidate,
    relationship: ClaimRelationship,
    confidence_class: Literal["high", "medium", "low", "abstain"],
    materiality: Literal["high", "medium", "low"],
    differing_dimensions: list[str],
    compatible_dimensions: list[str],
    normalized_value_comparison_status: ClaimComparisonStatus,
    audit_safe_explanation: str,
    clarification_or_evidence_requirement: str | None = None,
    model_identifier: str | None = None,
) -> ClaimRelationshipDecision:
    return ClaimRelationshipDecision(
        claim_a_id=candidate.claim_a.claim_id,
        claim_b_id=candidate.claim_b.claim_id,
        relationship=relationship,
        materiality=materiality,
        confidence_class=confidence_class,
        differing_dimensions=differing_dimensions,
        compatible_dimensions=compatible_dimensions,
        normalized_value_comparison_status=normalized_value_comparison_status,
        deterministic_validation_required=True,
        audit_safe_explanation=audit_safe_explanation,
        clarification_or_evidence_requirement=clarification_or_evidence_requirement,
        model_identifier=model_identifier,
    )


def _validate_deterministic_relationship(
    candidate: ClaimPairCandidate,
) -> ClaimRelationshipDecision | None:
    a = candidate.claim_a
    b = candidate.claim_b
    if a.claim_id == b.claim_id:
        return _project_claim_relationship(
            candidate=candidate,
            relationship="duplicate",
            confidence_class="high",
            materiality="low",
            differing_dimensions=[],
            compatible_dimensions=["same_claim_id"],
            normalized_value_comparison_status="exact_match",
            audit_safe_explanation="The claims are identical after canonical projection.",
        )
    normalized_status = _deterministic_claim_comparison_status(a, b)
    if normalized_status in {"exact_match", "normalized_match"}:
        return _project_claim_relationship(
            candidate=candidate,
            relationship="equivalent",
            confidence_class="high",
            materiality="low",
            differing_dimensions=[],
            compatible_dimensions=["normalized_value", "scope"],
            normalized_value_comparison_status=normalized_status,
            audit_safe_explanation="The claims normalize to the same value under compatible scope.",
        )
    if not candidate.overlapping_temporal_scope:
        return _project_claim_relationship(
            candidate=candidate,
            relationship="conditionally_different",
            confidence_class="high",
            materiality="medium",
            differing_dimensions=["temporal_scope"],
            compatible_dimensions=["entity", "predicate"],
            normalized_value_comparison_status=normalized_status,
            audit_safe_explanation="The claims occur in different time windows and are not directly comparable.",
        )
    if not candidate.overlapping_entity:
        return _project_claim_relationship(
            candidate=candidate,
            relationship="conditionally_different",
            confidence_class="high",
            materiality="medium",
            differing_dimensions=["entity"],
            compatible_dimensions=["predicate", "temporal_scope"],
            normalized_value_comparison_status=normalized_status,
            audit_safe_explanation="The claims refer to different entities and are not directly comparable.",
        )
    if not candidate.overlapping_jurisdiction:
        return _project_claim_relationship(
            candidate=candidate,
            relationship="conditionally_different",
            confidence_class="high",
            materiality="medium",
            differing_dimensions=["jurisdiction"],
            compatible_dimensions=["entity", "predicate", "temporal_scope"],
            normalized_value_comparison_status=normalized_status,
            audit_safe_explanation="The claims apply in different jurisdictions and are not directly comparable.",
        )
    if normalized_status == "normalized_conflict":
        return _project_claim_relationship(
            candidate=candidate,
            relationship="contradictory",
            confidence_class="high",
            materiality="high",
            differing_dimensions=["normalized_value"],
            compatible_dimensions=["entity", "predicate", "jurisdiction", "temporal_scope"],
            normalized_value_comparison_status=normalized_status,
            audit_safe_explanation="The claims concern the same scope but normalize to incompatible values.",
        )
    if a.predicate == "exception" or b.predicate == "exception":
        return _project_claim_relationship(
            candidate=candidate,
            relationship="complementary",
            confidence_class="medium",
            materiality="medium",
            differing_dimensions=["exception_scope"],
            compatible_dimensions=["predicate"],
            normalized_value_comparison_status=normalized_status,
            audit_safe_explanation="One claim describes an exception that may coexist with the broader rule.",
        )
    return None


def _validate_model_relationship(
    candidate: ClaimPairCandidate,
    decision: ClaimRelationshipDecision,
) -> ClaimRelationshipDecision | None:
    if decision.claim_a_id != candidate.claim_a.claim_id or decision.claim_b_id != candidate.claim_b.claim_id:
        return None
    if decision.relationship not in cast(tuple[ClaimRelationship, ...], (
        "equivalent",
        "complementary",
        "narrower_than",
        "broader_than",
        "supersedes",
        "superseded_by",
        "conditionally_different",
        "contradictory",
        "insufficiently_comparable",
        "duplicate",
        "unresolved",
    )):
        return None
    if decision.relationship == "contradictory" and candidate.deterministic_status == "exact_match":
        return _project_claim_relationship(
            candidate=candidate,
            relationship="equivalent",
            confidence_class="high",
            materiality="low",
            differing_dimensions=[],
            compatible_dimensions=["normalized_value"],
            normalized_value_comparison_status="exact_match",
            audit_safe_explanation="Deterministic normalization shows the claims are equivalent, so they cannot contradict.",
            model_identifier=decision.model_identifier,
        )
    if decision.relationship == "contradictory" and not candidate.overlapping_temporal_scope:
        return _project_claim_relationship(
            candidate=candidate,
            relationship="conditionally_different",
            confidence_class="high",
            materiality="medium",
            differing_dimensions=["temporal_scope"],
            compatible_dimensions=["entity", "predicate"],
            normalized_value_comparison_status=decision.normalized_value_comparison_status,
            audit_safe_explanation="Deterministic validation found non-overlapping time windows, so contradiction is not allowed.",
            model_identifier=decision.model_identifier,
        )
    return decision


def _deterministic_claim_comparison_status(
    claim_a: CanonicalClaim,
    claim_b: CanonicalClaim,
) -> ClaimComparisonStatus:
    if claim_a.normalized_value.comparison_key() == claim_b.normalized_value.comparison_key():
        return "exact_match"
    if _normalized_value_equivalent(claim_a, claim_b):
        return "normalized_match"
    if _normalized_value_conflicts(claim_a, claim_b):
        return "normalized_conflict"
    if _normalized_value_compatible(claim_a, claim_b):
        return "compatible_difference"
    return "incompatible"


def _normalized_value_equivalent(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    a = claim_a.normalized_value
    b = claim_b.normalized_value
    if a.kind != b.kind:
        return False
    if a.kind == "amount":
        if a.currency_code and b.currency_code and a.currency_code != b.currency_code:
            return False
        if a.number_value is None or b.number_value is None:
            return False
        if a.frequency or b.frequency:
            target_frequency = a.frequency or b.frequency
            converted_a = convert_frequency_value(a.number_value, from_frequency=a.frequency, to_frequency=target_frequency)
            converted_b = convert_frequency_value(b.number_value, from_frequency=b.frequency, to_frequency=target_frequency)
            if converted_a is None or converted_b is None:
                return False
            return _close_enough(converted_a, converted_b)
        return _close_enough(a.number_value, b.number_value)
    if a.kind == "rate":
        if a.basis and b.basis and a.basis != b.basis and not _rate_basis_equivalent(a.basis, b.basis):
            return False
        return a.number_value is not None and b.number_value is not None and _close_enough(a.number_value, b.number_value)
    if a.kind == "date":
        return a.date_value is not None and a.date_value == b.date_value
    if a.kind == "date_range":
        return (
            a.date_start is not None
            and a.date_start == b.date_start
            and a.date_end == b.date_end
            and a.inclusive_start == b.inclusive_start
            and a.inclusive_end == b.inclusive_end
        )
    if a.kind == "boolean":
        return a.bool_value is not None and a.bool_value == b.bool_value
    if a.kind in {"categorical", "text"}:
        return canonical_text(a.enum_value or a.text_value) == canonical_text(b.enum_value or b.text_value)
    return False


def _normalized_value_conflicts(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    a = claim_a.normalized_value
    b = claim_b.normalized_value
    if a.kind != b.kind:
        return False
    if a.kind in {"amount", "rate"}:
        if a.currency_code and b.currency_code and a.currency_code != b.currency_code:
            return False
        if a.kind == "rate" and a.basis and b.basis and a.basis != b.basis and not _rate_basis_equivalent(a.basis, b.basis):
            return False
        if a.number_value is None or b.number_value is None:
            return False
        return not _normalized_value_equivalent(claim_a, claim_b)
    if a.kind == "boolean" and a.bool_value is not None and b.bool_value is not None:
        return a.bool_value != b.bool_value
    if a.kind == "date" and a.date_value and b.date_value:
        return a.date_value != b.date_value
    if a.kind == "date_range" and a.date_start and b.date_start:
        return (a.date_start, a.date_end, a.inclusive_start, a.inclusive_end) != (
            b.date_start,
            b.date_end,
            b.inclusive_start,
            b.inclusive_end,
        )
    return False


def _normalized_value_compatible(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    a = claim_a.normalized_value
    b = claim_b.normalized_value
    if a.kind == b.kind:
        return True
    if {a.kind, b.kind} <= {"amount", "rate", "date", "date_range", "boolean", "categorical", "text"}:
        return True
    return False


def _claims_overlap_on_entity(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    if claim_a.entity_type == "unknown" or claim_b.entity_type == "unknown":
        return True
    if claim_a.entity_type == claim_b.entity_type:
        return True
    return bool(
        claim_a.entity_label
        and claim_b.entity_label
        and canonical_text(claim_a.entity_label) == canonical_text(claim_b.entity_label)
    )


def _claims_overlap_on_jurisdiction(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    if not claim_a.jurisdiction or not claim_b.jurisdiction:
        return True
    return canonical_text(claim_a.jurisdiction) == canonical_text(claim_b.jurisdiction)


def _claims_overlap_on_predicate(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    if claim_a.predicate == claim_b.predicate:
        return True
    related = {
        frozenset({"tax_rate", "contribution_rate"}),
        frozenset({"threshold", "liability"}),
        frozenset({"employer_obligation", "employee_obligation"}),
        frozenset({"exemption", "exception"}),
        frozenset({"eligibility", "status"}),
        frozenset({"filing_deadline", "reporting_requirement"}),
        frozenset({"prohibition", "exemption"}),
    }
    return frozenset({claim_a.predicate, claim_b.predicate}) in related


def _claims_overlap_on_time(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> bool:
    if claim_a.tax_year is not None and claim_b.tax_year is not None and claim_a.tax_year != claim_b.tax_year:
        return False
    if claim_a.effective_from and claim_b.effective_to and claim_a.effective_from > claim_b.effective_to:
        return False
    if claim_b.effective_from and claim_a.effective_to and claim_b.effective_from > claim_a.effective_to:
        return False
    return True


def _pair_reason(claim_a: CanonicalClaim, claim_b: CanonicalClaim) -> str | None:
    if claim_a.tax_domain != claim_b.tax_domain and "unknown" not in {claim_a.tax_domain, claim_b.tax_domain}:
        return None
    if not _claims_overlap_on_entity(claim_a, claim_b):
        return None
    if not _claims_overlap_on_jurisdiction(claim_a, claim_b):
        return None
    if not _claims_overlap_on_predicate(claim_a, claim_b):
        return None
    if not _claims_overlap_on_time(claim_a, claim_b):
        return None
    return "compatible_claim_pair"


def _claim_value_label(claim: CanonicalClaim) -> str:
    if claim.raw_value_text:
        return claim.raw_value_text
    value = claim.normalized_value
    if value.kind == "amount" and value.number_value is not None:
        currency = f" {value.currency_code}" if value.currency_code else ""
        frequency = f"/{value.frequency}" if value.frequency else ""
        return f"{value.number_value}{currency}{frequency}".strip()
    if value.kind == "rate" and value.number_value is not None:
        return f"{value.number_value:.6g}"
    if value.kind == "date" and value.date_value:
        return value.date_value
    if value.kind == "date_range" and value.date_start and value.date_end:
        return f"{value.date_start}..{value.date_end}"
    if value.enum_value:
        return value.enum_value
    if value.text_value:
        return value.text_value
    return claim.raw_value_text or claim.predicate


def _close_enough(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b), 1.0))


def _rate_basis_equivalent(basis_a: str, basis_b: str) -> bool:
    pair = {basis_a.lower(), basis_b.lower()}
    return pair <= {"percentage", "decimal"}
