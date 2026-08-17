"""Versioned, immutable workflow evidence projection boundary.

This module deliberately accepts resolved evidence, never uploaded documents,
provider responses, or ``extracted_fields`` dictionaries.  Workflow services
own their field interpretation after this boundary.
"""

from __future__ import annotations

from typing import Literal
from collections.abc import Mapping

from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict

WORKFLOW_PROJECTION_VERSION = "1.0.0"
WorkflowName = Literal["tax", "filing", "forms", "reports", "compliance"]


class SourceReference(BaseModel):
    """Stable provenance pointer retained with one resolved evidence value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1)
    document_version_id: str | None = None
    source_location: str | None = None


class ProjectedEvidenceItem(BaseModel):
    """An effective evidence value and its governance state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    effective_value: object
    source_references: tuple[SourceReference, ...] = ()
    uncertainty: tuple[str, ...] = ()
    correction_ids: tuple[str, ...] = ()


class WorkflowEvidenceProjection(BaseModel):
    """The only document-derived input accepted by workflow projection adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_version: Literal["1.0.0"] = WORKFLOW_PROJECTION_VERSION
    workflow: WorkflowName
    workflow_version: str = Field(min_length=1)
    evidence_items: tuple[ProjectedEvidenceItem, ...]
    missing_requirement_ids: tuple[str, ...] = ()
    conflicts: tuple[Mapping[str, object], ...] = ()
    uncertainty: tuple[str, ...] = ()
    correction_ids: tuple[str, ...] = ()
    validity_state: Literal["valid", "stale", "invalid"] = "valid"

    @classmethod
    def from_resolved_evidence(
        cls,
        *,
        workflow: WorkflowName,
        workflow_version: str,
        evidence_items: list[ProjectedEvidenceItem],
        missing_requirement_ids: list[str] | None = None,
        conflicts: list[Mapping[str, object]] | None = None,
        uncertainty: list[str] | None = None,
        correction_ids: list[str] | None = None,
    ) -> WorkflowEvidenceProjection:
        """Create a deterministic projection from already-resolved evidence."""

        return cls(
            workflow=workflow,
            workflow_version=workflow_version,
            evidence_items=tuple(sorted(evidence_items, key=lambda item: item.evidence_id)),
            missing_requirement_ids=tuple(sorted(set(missing_requirement_ids or []))),
            conflicts=tuple(conflicts or []),
            uncertainty=tuple(sorted(set(uncertainty or []))),
            correction_ids=tuple(sorted(set(correction_ids or []))),
        )
