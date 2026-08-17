"""Forms-owned conversion of governed evidence projections to field values."""

from __future__ import annotations

from shared.workflow_evidence_projection import WorkflowEvidenceProjection


def form_fields_from_projection(*, projection: WorkflowEvidenceProjection) -> dict[str, object]:
    """Map evidence to form fields while retaining evidence IDs and provenance."""

    if projection.workflow != "forms":
        raise ValueError("Forms workflow requires a forms evidence projection.")
    return {
        item.requirement_id: {
            "value": item.effective_value,
            "evidence_id": item.evidence_id,
            "source_references": [ref.model_dump() for ref in item.source_references],
        }
        for item in projection.evidence_items
    }
