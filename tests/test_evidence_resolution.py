"""Milestone 22 evidence resolution behaviour."""

from __future__ import annotations

from services.document_ai.app.evidence_resolution import AssuranceState
from services.document_ai.app.evidence_resolution import DerivationScope
from services.document_ai.app.evidence_resolution import EvidenceResolver
from services.document_ai.app.evidence_resolution import EvidenceCandidate
from services.document_ai.app.evidence_resolution import TargetedDerivationResult
from services.document_ai.app.evidence_requirements import EvidenceRequirement


def test_assurance_states_are_explicit_and_complete() -> None:
    assert {state.value for state in AssuranceState} == {
        "directly_observed",
        "normalized",
        "aggregated",
        "model_derived",
        "confirmed",
        "corrected",
        "approximate",
        "conflicted",
        "incomplete",
        "missing",
        "unreadable",
        "unavailable",
        "unauthorized",
    }


def _requirement(**overrides: object) -> EvidenceRequirement:
    payload: dict[str, object] = {
        "requirement_id": "annual-paye",
        "schema_version": "1.0.0",
        "semantic_meaning": "Employee annual PAYE total",
        "entity_scope": {
            "selection": "specific_entities",
            "entities": [{"entity_id": "employee-1", "entity_type": "person", "role": "employee"}],
        },
        "time_scope": {"kind": "calendar_year", "year": 2025},
        "unit": {"dimension": "currency", "code": "KES", "normalization": "allowed"},
        "multiplicity": {"kind": "aggregate_plus_components"},
        "completeness": {"coverage": "all_periods", "partial_result": "mark_incomplete"},
        "materiality": {"kind": "all_values"},
        "permitted_derivations": ["direct_observation", "normalization", "summation"],
        "uncertainty_tolerance": {},
        "confirmation_policy": {"mode": "when_triggered", "triggers": ["missing_period"]},
    }
    payload.update(overrides)
    return EvidenceRequirement.model_validate(payload)


def _candidate(
    value: object, *, evidence_id: str = "e-1", period: str = "2025-01", **extra: object
) -> EvidenceCandidate:
    return EvidenceCandidate.model_validate(
        {
            "evidence_id": evidence_id,
            "document_id": f"d-{evidence_id}",
            "document_version_id": "v1",
            "canonical_representation_id": "r1",
            "canonical_element_id": f"ce-{evidence_id}",
            "value": value,
            "unit": "KES",
            "entity_id": "employee-1",
            "period": period,
            **extra,
        }
    )


def test_direct_amount_is_observed_without_provider() -> None:
    result = EvidenceResolver().resolve(
        requirement=_requirement(
            completeness={"coverage": "one_valid_item", "partial_result": "reject"}
        ),
        candidates=[_candidate(120000)],
    )
    assert result.assurance_method is AssuranceState.DIRECTLY_OBSERVED
    assert result.cost["provider_called"] is False


def test_currency_normalization_preserves_source_provenance() -> None:
    result = EvidenceResolver().resolve(
        requirement=_requirement(), candidates=[_candidate("KSh 120,000", unit="KSh")]
    )
    assert result.assurance_method is AssuranceState.NORMALIZED
    assert result.value == "120000"
    assert result.provenance[-1]["source_value"] == "KSh 120,000"


def test_ambiguous_date_requires_confirmation_not_model_guessing() -> None:
    requirement = _requirement(
        unit={"dimension": "date"},
        multiplicity={"kind": "exactly_one"},
        completeness={"coverage": "one_valid_item", "partial_result": "reject"},
        permitted_derivations=["direct_observation", "date_normalization"],
        confirmation_policy={"mode": "when_triggered", "triggers": ["prohibited_derivation"]},
    )
    result = EvidenceResolver().resolve(
        requirement=requirement, candidates=[_candidate("03/04/2025", unit=None)]
    )
    assert result.status == "confirmation_required"


def test_annual_aggregation_keeps_contributors_and_marks_missing_periods() -> None:
    candidates = [
        _candidate(10, evidence_id=f"e-{month}", period=f"2025-{month:02d}")
        for month in range(1, 12)
    ]
    result = EvidenceResolver().resolve(requirement=_requirement(), candidates=candidates)
    assert result.assurance_method is AssuranceState.AGGREGATED
    assert result.assurance_conditions == [AssuranceState.INCOMPLETE]
    assert len(result.contributors) == 11
    assert result.cost["provider_called"] is False


def test_duplicate_is_not_double_counted_and_employer_scope_is_not_collapsed() -> None:
    requirement = _requirement(
        entity_scope={
            "selection": "specific_entities",
            "entities": [
                {"entity_id": "employer-a", "entity_type": "organization", "role": "employer"}
            ],
        }
    )
    first = _candidate(20, evidence_id="one", entity_id="employer-a")
    duplicate = _candidate(20, evidence_id="two", entity_id="employer-a", duplicate_of="one")
    other = _candidate(20, evidence_id="three", entity_id="employer-b")
    result = EvidenceResolver().resolve(
        requirement=requirement, candidates=[first, duplicate, other]
    )
    assert result.value == "20"
    assert result.contributors == ["one"]


def test_search_miss_is_incomplete_not_proof_of_missing_and_unreadable_is_distinct() -> None:
    resolver = EvidenceResolver()
    missing = resolver.resolve(requirement=_requirement(), candidates=[])
    unreadable = resolver.resolve(
        requirement=_requirement(), candidates=[_candidate(None, readable=False)]
    )
    assert missing.assurance_method is AssuranceState.INCOMPLETE
    assert unreadable.assurance_method is AssuranceState.UNREADABLE


def test_conflict_requires_comparable_scope_and_unequal_employers_do_not_conflict() -> None:
    requirement = _requirement(
        completeness={"coverage": "one_valid_item", "partial_result": "reject"},
        confirmation_policy={"mode": "when_triggered", "triggers": ["conflict"]},
        uncertainty_tolerance={"conflicts": "require_confirmation"},
    )
    first = _candidate(20, evidence_id="one")
    second = _candidate(30, evidence_id="two")
    result = EvidenceResolver().resolve(requirement=requirement, candidates=[first, second])
    assert result.assurance_method is AssuranceState.CONFLICTED
    assert result.status == "confirmation_required"


class _RecordingDerivationClient:
    def __init__(self) -> None:
        self.scope_ids: list[str] = []

    def derive(
        self, *, requirement: EvidenceRequirement, scope: DerivationScope
    ) -> TargetedDerivationResult:
        self.scope_ids = list(scope.canonical_element_ids)
        return TargetedDerivationResult(
            value="interpreted",
            unit=None,
            source_element_ids=self.scope_ids,
            model="gpt-4.1-mini",
            usage={"input_tokens": 4, "output_tokens": 2},
        )


def test_targeted_derivation_uses_minimum_scope_and_records_usage() -> None:
    client = _RecordingDerivationClient()
    requirement = _requirement(
        unit={"dimension": "text"},
        multiplicity={"kind": "exactly_one"},
        completeness={"coverage": "one_valid_item", "partial_result": "reject"},
        permitted_derivations=["direct_observation"],
    )
    candidates = [
        _candidate(None, evidence_id="necessary"),
        _candidate(None, evidence_id="unrelated"),
    ]
    result = EvidenceResolver(derivation_client=client).resolve(
        requirement=requirement, candidates=candidates
    )
    assert result.assurance_method is AssuranceState.MODEL_DERIVED
    assert client.scope_ids == ["ce-necessary"]
    assert result.cost["usage"] == {"input_tokens": 4, "output_tokens": 2}
