"""Versioned, template-neutral semantic evidence-requirement contract.

This module describes a caller's information need only.  It deliberately does
not resolve evidence, translate extraction keys, or select document templates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import model_validator

EVIDENCE_REQUIREMENT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_EVIDENCE_REQUIREMENT_SCHEMA_VERSIONS = frozenset({EVIDENCE_REQUIREMENT_SCHEMA_VERSION})


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScopedEntity(_ContractModel):
    entity_id: str | None = Field(default=None, min_length=1, max_length=200)
    entity_type: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=80)
    relationship: str | None = Field(default=None, min_length=1, max_length=160)


def _empty_entities() -> list[ScopedEntity]:
    return []


class EntityScope(_ContractModel):
    selection: Literal["specific_entities", "role", "authorized_set", "unresolved"]
    entities: list[ScopedEntity] = Field(default_factory=_empty_entities, max_length=100)
    unresolved_identity_requires_confirmation: bool = False

    @model_validator(mode="after")
    def _validate_selection(self) -> EntityScope:
        if self.selection == "specific_entities" and not self.entities:
            raise ValueError("specific_entities_requires_entities")
        if self.selection == "unresolved" and not self.unresolved_identity_requires_confirmation:
            raise ValueError("unresolved_entity_requires_confirmation")
        return self


class TimeScope(_ContractModel):
    kind: Literal[
        "calendar_year",
        "fiscal_year",
        "month",
        "month_range",
        "date_range",
        "as_of",
        "recurring",
        "unspecified",
    ]
    year: int | None = Field(default=None, ge=1900, le=2200)
    month: int | None = Field(default=None, ge=1, le=12)
    start: str | None = Field(default=None, min_length=4, max_length=32)
    end: str | None = Field(default=None, min_length=4, max_length=32)
    date: str | None = Field(default=None, min_length=8, max_length=32)
    basis: (
        Literal["calendar", "fiscal", "evidence_effective", "document_effective", "event"] | None
    ) = None
    unresolved_requires_confirmation: bool = False

    @model_validator(mode="after")
    def _validate_shape(self) -> TimeScope:
        if self.kind in {"calendar_year", "fiscal_year"} and self.year is None:
            raise ValueError("year_scope_requires_year")
        if self.kind == "month" and (self.year is None or self.month is None):
            raise ValueError("month_scope_requires_year_and_month")
        if self.kind in {"month_range", "date_range"}:
            if self.start is None or self.end is None:
                raise ValueError("range_scope_requires_start_and_end")
            if self.start > self.end:
                raise ValueError("time_range_must_be_ordered")
        if self.kind == "as_of" and self.date is None:
            raise ValueError("as_of_scope_requires_date")
        if self.kind == "unspecified" and not self.unresolved_requires_confirmation:
            raise ValueError("unspecified_time_requires_confirmation")
        return self

    def resolves_months(self) -> bool:
        return self.kind in {"calendar_year", "fiscal_year", "month", "month_range", "recurring"}


class Unit(_ContractModel):
    dimension: Literal[
        "currency",
        "percentage",
        "count",
        "duration",
        "date",
        "text",
        "identifier",
        "quantity",
        "mass",
        "volume",
        "rate",
        "unitless",
        "source_native",
    ]
    code: str | None = Field(default=None, min_length=1, max_length=32)
    normalization: Literal["source_native", "required", "allowed", "unresolved"] = "source_native"
    source_unit_may_differ: bool = False

    @model_validator(mode="after")
    def _validate_unit(self) -> Unit:
        if self.dimension == "currency" and self.code is None:
            raise ValueError("currency_unit_requires_code")
        return self


class Multiplicity(_ContractModel):
    kind: Literal[
        "exactly_one",
        "zero_or_one",
        "one_or_more",
        "zero_or_more",
        "one_per_document",
        "one_per_entity",
        "one_per_month",
        "one_per_period",
        "one_per_transaction",
        "grouped",
        "aggregate_plus_components",
    ]
    min_items: int | None = Field(default=None, ge=0, le=10000)
    max_items: int | None = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def _validate_bounds(self) -> Multiplicity:
        if (
            self.min_items is not None
            and self.max_items is not None
            and self.min_items > self.max_items
        ):
            raise ValueError("multiplicity_minimum_exceeds_maximum")
        return self


class Completeness(_ContractModel):
    coverage: Literal[
        "one_valid_item",
        "all_matching_items",
        "all_periods",
        "all_authorized_documents",
        "all_named_entities",
        "best_available",
    ]
    partial_result: Literal["reject", "mark_incomplete", "allow"]


class Materiality(_ContractModel):
    kind: Literal["all_values", "none", "threshold", "qualitative", "unresolved"]
    threshold: float | None = None
    unit: Unit | None = None
    level: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def _validate_materiality(self) -> Materiality:
        if self.kind == "threshold" and (self.threshold is None or self.unit is None):
            raise ValueError("materiality_threshold_requires_value_and_unit")
        if self.kind == "qualitative" and self.level is None:
            raise ValueError("qualitative_materiality_requires_level")
        return self


def _empty_uncertainty_categories() -> list[
    Literal[
        "minor_formatting_ambiguity",
        "bounded_uncertainty",
        "incomplete_temporal_attribution",
        "unresolved_entity_identity",
    ]
]:
    return []


class UncertaintyTolerance(_ContractModel):
    allowed: list[
        Literal[
            "minor_formatting_ambiguity",
            "bounded_uncertainty",
            "incomplete_temporal_attribution",
            "unresolved_entity_identity",
        ]
    ] = Field(default_factory=_empty_uncertainty_categories)
    estimated_values: Literal["prohibited", "allowed_labeled"] = "prohibited"
    conflicts: Literal["prohibited", "require_confirmation", "allowed_labeled"] = (
        "require_confirmation"
    )


def _empty_confirmation_triggers() -> list[
    Literal[
        "unresolved_entity",
        "unresolved_time",
        "missing_material",
        "missing_period",
        "conflict",
        "prohibited_derivation",
        "estimate",
    ]
]:
    return []


class ConfirmationPolicy(_ContractModel):
    mode: Literal["never", "when_triggered", "always", "allow_partial"]
    triggers: list[
        Literal[
            "unresolved_entity",
            "unresolved_time",
            "missing_material",
            "missing_period",
            "conflict",
            "prohibited_derivation",
            "estimate",
        ]
    ] = Field(default_factory=_empty_confirmation_triggers, max_length=20)

    @model_validator(mode="after")
    def _validate_policy(self) -> ConfirmationPolicy:
        if self.mode == "when_triggered" and not self.triggers:
            raise ValueError("triggered_confirmation_requires_trigger")
        if self.mode == "never" and self.triggers:
            raise ValueError("never_confirmation_cannot_have_triggers")
        return self


class EvidenceRequirement(_ContractModel):
    """One versioned semantic information need for downstream resolution."""

    requirement_id: str = Field(
        min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    schema_version: str = Field(min_length=1, max_length=32)
    semantic_meaning: str = Field(min_length=8, max_length=1000)
    entity_scope: EntityScope
    time_scope: TimeScope
    unit: Unit
    multiplicity: Multiplicity
    completeness: Completeness
    materiality: Materiality
    permitted_derivations: list[
        Literal[
            "direct_observation",
            "direct_observation_only",
            "normalization",
            "currency_conversion",
            "unit_conversion",
            "summation",
            "subtraction",
            "multiplication",
            "division",
            "aggregation_by_entity",
            "aggregation_by_period",
            "grouping",
            "date_normalization",
            "provenance_deduplication",
            "identified_component_calculation",
        ]
    ] = Field(min_length=1, max_length=20)
    uncertainty_tolerance: UncertaintyTolerance
    confirmation_policy: ConfirmationPolicy
    label: str | None = Field(default=None, min_length=1, max_length=120)
    caller_correlation_reference: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate_requirement(self) -> EvidenceRequirement:
        if self.schema_version not in SUPPORTED_EVIDENCE_REQUIREMENT_SCHEMA_VERSIONS:
            raise ValueError("unsupported_evidence_requirement_schema_version")
        if " " not in self.semantic_meaning.strip():
            raise ValueError("semantic_meaning_must_be_natural_language")
        derivations = set(self.permitted_derivations)
        if "direct_observation_only" in derivations and len(derivations) != 1:
            raise ValueError("direct_observation_only_cannot_permit_other_derivations")
        if self.multiplicity.kind == "one_per_month" and not self.time_scope.resolves_months():
            raise ValueError("one_per_month_requires_month_resolvable_time_scope")
        if self.multiplicity.kind == "aggregate_plus_components" and not (
            {"summation", "aggregation_by_entity", "aggregation_by_period"} & derivations
        ):
            raise ValueError("aggregate_plus_components_requires_aggregation_derivation")
        if (
            self.unit.normalization == "required"
            and self.unit.source_unit_may_differ
            and self.unit.dimension == "currency"
            and "currency_conversion" not in derivations
        ):
            raise ValueError("normalized_currency_requires_conversion_permission")
        triggers = set(self.confirmation_policy.triggers)
        if (
            "conflict" in triggers
            and self.uncertainty_tolerance.conflicts != "require_confirmation"
        ):
            raise ValueError("confirmation_trigger_requires_compatible_uncertainty")
        if (
            "estimate" in triggers
            and self.uncertainty_tolerance.estimated_values != "allowed_labeled"
        ):
            raise ValueError("estimate_confirmation_requires_allowed_labeled_estimates")
        if (
            "unresolved_entity" in triggers
            and not self.entity_scope.unresolved_identity_requires_confirmation
        ):
            raise ValueError("unresolved_entity_confirmation_requires_entity_scope")
        if "unresolved_time" in triggers and not self.time_scope.unresolved_requires_confirmation:
            raise ValueError("unresolved_time_confirmation_requires_time_scope")
        return self
