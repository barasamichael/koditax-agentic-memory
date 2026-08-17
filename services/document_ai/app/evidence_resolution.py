"""Ordered, source-grounded semantic evidence resolution (Milestone 22).

This module intentionally consumes *authorized canonical candidates*, rather
than retrieval scores or extraction-template fields.  It is a pure service
boundary: repositories own authorization/canonical lifecycle checks and pass
only current candidates here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from typing import Protocol
from decimal import Decimal
from decimal import InvalidOperation
from dataclasses import dataclass
from collections.abc import Mapping

from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict

from services.document_ai.app.evidence_requirements import EvidenceRequirement

EVIDENCE_ASSURANCE_SCHEMA_VERSION = "1.0.0"
EVIDENCE_RESOLUTION_POLICY_VERSION = "v1"


def _empty_assurance_states() -> list[AssuranceState]:
    return []


def _empty_provenance() -> list[dict[str, object]]:
    return []


class AssuranceState(StrEnum):
    DIRECTLY_OBSERVED = "directly_observed"
    NORMALIZED = "normalized"
    AGGREGATED = "aggregated"
    MODEL_DERIVED = "model_derived"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    APPROXIMATE = "approximate"
    CONFLICTED = "conflicted"
    INCOMPLETE = "incomplete"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"


class EvidenceCandidate(BaseModel):
    """A current, authorized canonical observation supplied by Milestone 20."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version_id: str = Field(min_length=1)
    canonical_representation_id: str = Field(min_length=1)
    canonical_element_id: str = Field(min_length=1)
    value: object | None = None
    observed_text: str | None = None
    unit: str | None = None
    entity_id: str | None = None
    entity_role: str | None = None
    period: str | None = None
    source_region: dict[str, object] = Field(default_factory=dict)
    semantic_keys: list[str] = Field(default_factory=list)
    readable: bool = True
    authorized: bool = True
    current: bool = True
    duplicate_of: str | None = None
    correction_of: str | None = None
    uncertainty: dict[str, object] = Field(default_factory=dict)


class ExistingEvidence(BaseModel):
    """A previously resolved result considered only with explicit compatibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    value: object
    unit: str | None = None
    assurance_method: AssuranceState
    assurance_conditions: list[AssuranceState] = Field(default_factory=_empty_assurance_states)
    provenance: list[dict[str, object]] = Field(default_factory=_empty_provenance)
    compatible: bool = False
    current: bool = True
    authorized: bool = True


class DerivationScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    candidates: list[EvidenceCandidate]
    excluded_candidate_count: int = Field(ge=0)
    scope_selection_policy_version: str = EVIDENCE_RESOLUTION_POLICY_VERSION

    @property
    def canonical_element_ids(self) -> list[str]:
        return [candidate.canonical_element_id for candidate in self.candidates]


class TargetedDerivationResult(BaseModel):
    """Strict provider result accepted only when all references stay in scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: object
    unit: str | None = None
    source_element_ids: list[str] = Field(min_length=1)
    uncertainty: dict[str, object] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    confirmation_required: bool = False
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1)
    usage: dict[str, int | None] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)


class TargetedDerivationClient(Protocol):
    def derive(
        self, *, requirement: EvidenceRequirement, scope: DerivationScope
    ) -> TargetedDerivationResult: ...


class EvidenceResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    status: Literal["resolved", "confirmation_required", "unresolved"]
    method: str
    value: object | None = None
    unit: str | None = None
    assurance_method: AssuranceState
    assurance_conditions: list[AssuranceState] = Field(default_factory=_empty_assurance_states)
    contributors: list[str] = Field(default_factory=list)
    provenance: list[dict[str, object]] = Field(default_factory=_empty_provenance)
    uncertainty: dict[str, object] = Field(default_factory=dict)
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    cost: dict[str, object] = Field(default_factory=dict)
    confirmation: dict[str, object] | None = None
    diagnostics: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class EvidenceResolver:
    """Try resolution methods in the mandatory, cheapest-valid order."""

    derivation_client: TargetedDerivationClient | None = None

    def resolve(
        self,
        *,
        requirement: EvidenceRequirement,
        candidates: list[EvidenceCandidate],
        existing_evidence: list[ExistingEvidence] | None = None,
    ) -> EvidenceResolutionResult:
        diagnostics: list[str] = []
        reused = self._reuse(requirement, existing_evidence or [])
        if reused is not None:
            return reused
        usable = [item for item in candidates if item.current and item.authorized]
        if any(not item.authorized for item in candidates):
            diagnostics.append("unauthorized_candidates_excluded")
        unreadable = [item for item in usable if not item.readable]
        readable = [item for item in usable if item.readable]
        direct = self._direct(requirement, readable)
        if direct is not None:
            return direct
        normalized = self._normalize(requirement, readable)
        if normalized is not None:
            return normalized
        relationship = self._relationship(requirement, readable)
        if relationship is not None:
            return relationship
        conflict = self._conflict(requirement, readable)
        if conflict is not None:
            return conflict
        aggregate = self._aggregate(requirement, readable)
        if aggregate is not None:
            return aggregate
        if unreadable:
            return self._unresolved(
                requirement, AssuranceState.UNREADABLE, "source_unreadable", unreadable
            )
        derived = self._derive(requirement, readable)
        if derived is not None:
            return derived
        if not usable and candidates and all(not item.authorized for item in candidates):
            return self._unresolved(
                requirement, AssuranceState.UNAUTHORIZED, "scope_unauthorized", []
            )
        reason = "no_candidate_discovered" if not candidates else "requirement_not_satisfied"
        state = (
            AssuranceState.INCOMPLETE
            if reason == "no_candidate_discovered"
            else AssuranceState.MISSING
        )
        # A search miss is only unknown coverage, never proof of absence.
        return self._unresolved(requirement, state, reason, readable, diagnostics=diagnostics)

    def _reuse(
        self, requirement: EvidenceRequirement, records: list[ExistingEvidence]
    ) -> EvidenceResolutionResult | None:
        for record in records:
            if (
                record.requirement_id == requirement.requirement_id
                and record.compatible
                and record.current
                and record.authorized
            ):
                return EvidenceResolutionResult(
                    requirement_id=requirement.requirement_id,
                    status="resolved",
                    method="existing_valid_evidence",
                    value=record.value,
                    unit=record.unit,
                    assurance_method=record.assurance_method,
                    assurance_conditions=record.assurance_conditions,
                    provenance=record.provenance
                    + [
                        {
                            "reused_evidence_id": record.evidence_id,
                            "policy_version": EVIDENCE_RESOLUTION_POLICY_VERSION,
                        }
                    ],
                    completeness="complete",
                    cost={"provider_called": False},
                )
        return None

    def _direct(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> EvidenceResolutionResult | None:
        matching = self._matching(requirement, candidates)
        if len(matching) != 1 or matching[0].value is None:
            return None
        candidate = matching[0]
        if requirement.unit.dimension == "date" and _ambiguous_date(candidate.value):
            return None
        if not _unit_compatible(requirement, candidate.unit):
            return None
        if requirement.unit.dimension == "currency" and _as_decimal(candidate.value) is None:
            return None
        return self._result(
            requirement,
            "direct_canonical_mapping",
            candidate.value,
            candidate.unit,
            AssuranceState.DIRECTLY_OBSERVED,
            [candidate],
        )

    def _normalize(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> EvidenceResolutionResult | None:
        if (
            "normalization" not in requirement.permitted_derivations
            and "date_normalization" not in requirement.permitted_derivations
        ):
            return None
        for candidate in self._matching(requirement, candidates):
            normalized = _normalise_value(candidate, requirement)
            if normalized is not None:
                value, unit, rule = normalized
                return self._result(
                    requirement,
                    "deterministic_normalization",
                    value,
                    unit,
                    AssuranceState.NORMALIZED,
                    [candidate],
                    extra_provenance={
                        "normalization_rule": rule,
                        "source_value": candidate.value,
                        "source_unit": candidate.unit,
                    },
                )
        return None

    def _relationship(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> EvidenceResolutionResult | None:
        matching = self._matching(requirement, candidates)
        # Canonical entity/period attribution is a relationship, never proximity inference.
        if (
            len(matching) == 1
            and matching[0].value is not None
            and (matching[0].entity_id or matching[0].period)
        ):
            candidate = matching[0]
            if _unit_compatible(requirement, candidate.unit) and not (
                requirement.unit.dimension == "date" and _ambiguous_date(candidate.value)
            ):
                return self._result(
                    requirement,
                    "deterministic_relationship_mapping",
                    candidate.value,
                    candidate.unit,
                    AssuranceState.DIRECTLY_OBSERVED,
                    [candidate],
                    extra_provenance={
                        "relationship_policy_version": EVIDENCE_RESOLUTION_POLICY_VERSION
                    },
                )
        return None

    def _aggregate(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> EvidenceResolutionResult | None:
        if "summation" not in requirement.permitted_derivations:
            return None
        matching = [
            item
            for item in self._matching(requirement, candidates)
            if _as_decimal(item.value) is not None and _unit_compatible(requirement, item.unit)
        ]
        deduped = [item for item in matching if item.duplicate_of is None]
        if not deduped:
            return None
        periods = {item.period for item in deduped if item.period}
        expected = _expected_periods(requirement)
        complete = not expected or expected <= periods
        if not complete and requirement.completeness.partial_result == "reject":
            return None
        total = Decimal("0")
        for item in deduped:
            amount = _as_decimal(item.value)
            if amount is not None:
                total += amount
        conditions = [] if complete else [AssuranceState.INCOMPLETE]
        result = self._result(
            requirement,
            "deterministic_aggregation",
            str(total),
            requirement.unit.code or deduped[0].unit,
            AssuranceState.AGGREGATED,
            deduped,
            conditions=conditions,
            extra_provenance={
                "operation": "summation",
                "excluded_evidence_ids": [
                    item.evidence_id for item in matching if item.duplicate_of is not None
                ],
                "aggregation_policy_version": EVIDENCE_RESOLUTION_POLICY_VERSION,
            },
        )
        result.completeness = "complete" if complete else "partial"
        return result

    def _conflict(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> EvidenceResolutionResult | None:
        """Mark only incompatible claims about the same scoped proposition."""

        comparable: dict[tuple[str | None, str | None, str | None], list[EvidenceCandidate]] = {}
        for candidate in self._matching(requirement, candidates):
            key = (candidate.entity_id, candidate.entity_role, candidate.period)
            comparable.setdefault(key, []).append(candidate)
        for items in comparable.values():
            values = {str(item.value) for item in items if item.value is not None}
            if len(values) > 1:
                return self._unresolved(
                    requirement,
                    AssuranceState.CONFLICTED,
                    "conflicting_comparable_sources",
                    items,
                )
        return None

    def _derive(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> EvidenceResolutionResult | None:
        if (
            self.derivation_client is None
            or set(requirement.permitted_derivations) == {"direct_observation_only"}
            or not candidates
        ):
            return None
        scope_candidates = self._minimum_scope(requirement, candidates)
        scope = DerivationScope(
            requirement_id=requirement.requirement_id,
            candidates=scope_candidates,
            excluded_candidate_count=len(candidates) - len(scope_candidates),
        )
        response = self.derivation_client.derive(requirement=requirement, scope=scope)
        if not set(response.source_element_ids) <= set(scope.canonical_element_ids):
            return None
        if response.confirmation_required:
            return self._unresolved(
                requirement,
                AssuranceState.INCOMPLETE,
                "model_requires_confirmation",
                scope_candidates,
            )
        return self._result(
            requirement,
            "targeted_openai_derivation",
            response.value,
            response.unit,
            AssuranceState.MODEL_DERIVED,
            scope_candidates,
            uncertainty=response.uncertainty,
            extra_provenance={
                "derivation_scope": {
                    "canonical_element_ids": scope.canonical_element_ids,
                    "excluded_candidate_count": scope.excluded_candidate_count,
                    "policy_version": scope.scope_selection_policy_version,
                },
                "provider_source_element_ids": response.source_element_ids,
            },
            cost={
                "provider": response.provider,
                "model": response.model,
                "usage": response.usage,
                "latency_ms": response.latency_ms,
                "source_scope_size": len(scope_candidates),
            },
        )

    def _matching(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> list[EvidenceCandidate]:
        required_entities = {
            entity.entity_id for entity in requirement.entity_scope.entities if entity.entity_id
        }
        result: list[EvidenceCandidate] = []
        for candidate in candidates:
            if required_entities and candidate.entity_id not in required_entities:
                continue
            if (
                candidate.semantic_keys
                and requirement.requirement_id not in candidate.semantic_keys
                and requirement.label not in candidate.semantic_keys
            ):
                continue
            if not _period_matches(requirement, candidate.period):
                continue
            result.append(candidate)
        return result

    def _minimum_scope(
        self, requirement: EvidenceRequirement, candidates: list[EvidenceCandidate]
    ) -> list[EvidenceCandidate]:
        matching = self._matching(requirement, candidates)
        return matching[:1] if matching else candidates[:1]

    def _result(
        self,
        requirement: EvidenceRequirement,
        method: str,
        value: object,
        unit: str | None,
        assurance: AssuranceState,
        contributors: list[EvidenceCandidate],
        *,
        conditions: list[AssuranceState] | None = None,
        extra_provenance: Mapping[str, object] | None = None,
        uncertainty: Mapping[str, object] | None = None,
        cost: Mapping[str, object] | None = None,
    ) -> EvidenceResolutionResult:
        provenance: list[dict[str, object]] = [
            {
                "evidence_id": item.evidence_id,
                "document_id": item.document_id,
                "document_version_id": item.document_version_id,
                "canonical_representation_id": item.canonical_representation_id,
                "canonical_element_id": item.canonical_element_id,
                "source_region": item.source_region,
                "entity_id": item.entity_id,
                "period": item.period,
            }
            for item in contributors
        ]
        if extra_provenance:
            provenance.append(dict(extra_provenance))
        return EvidenceResolutionResult(
            requirement_id=requirement.requirement_id,
            status="resolved",
            method=method,
            value=value,
            unit=unit,
            assurance_method=assurance,
            assurance_conditions=conditions or [],
            contributors=[item.evidence_id for item in contributors],
            provenance=provenance,
            uncertainty=dict(uncertainty or {}),
            completeness="complete",
            cost=dict(cost or {"provider_called": False}),
        )

    def _unresolved(
        self,
        requirement: EvidenceRequirement,
        state: AssuranceState,
        reason: str,
        candidates: list[EvidenceCandidate],
        *,
        diagnostics: list[str] | None = None,
    ) -> EvidenceResolutionResult:
        requires_confirmation = requirement.confirmation_policy.mode == "always" or (
            requirement.confirmation_policy.mode == "when_triggered"
            and _reason_trigger(reason) in requirement.confirmation_policy.triggers
        )
        return EvidenceResolutionResult(
            requirement_id=requirement.requirement_id,
            status="confirmation_required" if requires_confirmation else "unresolved",
            method="confirmation" if requires_confirmation else "unresolved",
            assurance_method=state,
            contributors=[item.evidence_id for item in candidates],
            provenance=[
                {
                    "canonical_element_id": item.canonical_element_id,
                    "document_id": item.document_id,
                    "source_region": item.source_region,
                }
                for item in candidates
            ],
            completeness="partial" if candidates else "unknown",
            cost={"provider_called": False},
            confirmation={"required": True, "reason": reason} if requires_confirmation else None,
            diagnostics=(diagnostics or []) + [reason],
        )


def _normalise_value(
    candidate: EvidenceCandidate, requirement: EvidenceRequirement
) -> tuple[object, str | None, str] | None:
    if requirement.unit.dimension == "currency" and requirement.unit.code:
        currency = (candidate.unit or "").upper().replace("KSH", "KES")
        source = str(
            candidate.value if candidate.value is not None else candidate.observed_text or ""
        )
        if (
            currency in {"KES", "KENYAN SHILLINGS"}
            or source.upper().startswith(("KSH", "KES"))
            or "KENYAN SHILLINGS" in source.upper()
        ):
            amount = _currency_amount(source)
            if amount is not None and (
                candidate.unit != requirement.unit.code or _as_decimal(candidate.value) is None
            ):
                return (
                    str(amount),
                    requirement.unit.code,
                    "currency_label_and_decimal_normalization_v1",
                )
    if (
        requirement.unit.dimension == "date"
        and isinstance(candidate.value, str)
        and not _ambiguous_date(candidate.value)
    ):
        return candidate.value, candidate.unit, "iso_date_normalization_v1"
    return None


def _currency_amount(value: str) -> Decimal | None:
    compact = (
        value.upper()
        .replace("KENYAN SHILLINGS", "")
        .replace("KSH", "")
        .replace("KES", "")
        .replace(",", "")
        .strip()
    )
    return _as_decimal(compact)


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _ambiguous_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return (
        len(parts) == 3
        and len(parts[0]) <= 2
        and len(parts[1]) <= 2
        and int(parts[0]) <= 12
        and int(parts[1]) <= 12
    )


def _unit_compatible(requirement: EvidenceRequirement, unit: str | None) -> bool:
    return (
        requirement.unit.code is None
        or unit is None
        or unit.upper().replace("KSH", "KES") == requirement.unit.code.upper()
    )


def _period_matches(requirement: EvidenceRequirement, period: str | None) -> bool:
    if period is None or requirement.time_scope.kind in {"unspecified", "recurring"}:
        return True
    if requirement.time_scope.year is not None and not period.startswith(
        str(requirement.time_scope.year)
    ):
        return False
    if requirement.time_scope.kind == "month" and requirement.time_scope.month is not None:
        return period.endswith(f"-{requirement.time_scope.month:02d}")
    return True


def _expected_periods(requirement: EvidenceRequirement) -> set[str]:
    if (
        requirement.completeness.coverage == "all_periods"
        and requirement.time_scope.kind in {"calendar_year", "fiscal_year"}
        and requirement.time_scope.year
    ):
        return {f"{requirement.time_scope.year}-{month:02d}" for month in range(1, 13)}
    return set()


def _reason_trigger(reason: str) -> str:
    if "conflict" in reason:
        return "conflict"
    if "unreadable" in reason:
        return "missing_material"
    if "period" in reason or "candidate" in reason:
        return "missing_period"
    return "prohibited_derivation"
