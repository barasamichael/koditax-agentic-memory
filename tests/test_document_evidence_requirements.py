"""Semantic evidence-requirement contract coverage for Milestone 21."""

from __future__ import annotations

import pytest

from services.document_ai.app.evidence_requirements import EvidenceRequirement


def _requirement(**overrides: object) -> EvidenceRequirement:
    payload: dict[str, object] = {
        "requirement_id": "req-annual-income-2025-a",
        "schema_version": "1.0.0",
        "semantic_meaning": "total employment income attributable to the employee",
        "entity_scope": {
            "selection": "specific_entities",
            "entities": [{"entity_id": "employee-a", "entity_type": "person", "role": "employee"}],
        },
        "time_scope": {"kind": "calendar_year", "year": 2025},
        "unit": {"dimension": "currency", "code": "KES", "normalization": "required"},
        "multiplicity": {"kind": "exactly_one"},
        "completeness": {"coverage": "all_periods", "partial_result": "reject"},
        "materiality": {"kind": "all_values"},
        "permitted_derivations": ["direct_observation", "summation"],
        "uncertainty_tolerance": {
            "allowed": ["minor_formatting_ambiguity"],
            "estimated_values": "prohibited",
            "conflicts": "require_confirmation",
        },
        "confirmation_policy": {
            "mode": "when_triggered",
            "triggers": ["missing_period", "conflict"],
        },
    }
    payload.update(overrides)
    return EvidenceRequirement.model_validate(payload)


def test_current_annual_employment_request_is_semantic_and_template_neutral() -> None:
    requirement = _requirement()
    assert requirement.time_scope.year == 2025
    assert requirement.entity_scope.entities[0].role == "employee"
    assert requirement.unit.code == "KES"
    assert "gross_pay" not in requirement.semantic_meaning
    assert "p9" not in requirement.model_dump_json().lower()


def test_current_monthly_paye_request_requires_one_value_per_month_and_full_coverage() -> None:
    requirement = _requirement(
        requirement_id="req-monthly-paye-2025-a",
        semantic_meaning="PAYE tax deducted from the employee remuneration",
        time_scope={"kind": "month_range", "start": "2025-01", "end": "2025-12"},
        multiplicity={"kind": "one_per_month"},
    )
    assert requirement.multiplicity.kind == "one_per_month"
    assert requirement.completeness.coverage == "all_periods"


def test_current_multi_document_supplier_customer_request_preserves_each_document() -> None:
    requirement = _requirement(
        requirement_id="req-supplier-total-2025-a",
        semantic_meaning="total amount payable for transactions between the supplier and customer",
        entity_scope={
            "selection": "specific_entities",
            "entities": [
                {"entity_id": "supplier-a", "entity_type": "organization", "role": "supplier"},
                {"entity_id": "customer-a", "entity_type": "organization", "role": "customer"},
            ],
        },
        time_scope={"kind": "date_range", "start": "2025-01-01", "end": "2025-12-31"},
        multiplicity={"kind": "one_per_document"},
        completeness={"coverage": "all_authorized_documents", "partial_result": "mark_incomplete"},
    )
    assert requirement.multiplicity.kind == "one_per_document"
    assert "invoice" not in requirement.model_dump_json().lower()


def test_derived_annual_requirement_requires_summation_and_direct_only_rejects_it() -> None:
    assert "summation" in _requirement().permitted_derivations
    with pytest.raises(ValueError, match="direct_observation_only"):
        _requirement(permitted_derivations=["direct_observation_only", "summation"])


def test_relationship_validation_protects_monthly_time_scope_and_confirmation_policy() -> None:
    with pytest.raises(ValueError, match="one_per_month_requires_month_resolvable_time_scope"):
        _requirement(
            multiplicity={"kind": "one_per_month"},
            time_scope={"kind": "as_of", "date": "2025-12-31"},
        )
    with pytest.raises(ValueError, match="confirmation_trigger_requires_compatible_uncertainty"):
        _requirement(
            uncertainty_tolerance={
                "allowed": [],
                "estimated_values": "prohibited",
                "conflicts": "prohibited",
            },
            confirmation_policy={"mode": "when_triggered", "triggers": ["conflict"]},
        )
