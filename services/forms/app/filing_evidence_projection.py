"""Filing-owned evidence interpretation boundary for the forms service."""

from __future__ import annotations

from shared.workflow_evidence_projection import WorkflowEvidenceProjection


def filing_inputs_from_projection(*, projection: WorkflowEvidenceProjection) -> dict[str, object]:
    """Return filing evidence and governance state for filing rules to interpret."""

    if projection.workflow != "filing":
        raise ValueError("Filing workflow requires a filing evidence projection.")
    values = {
        item.requirement_id: item.effective_value for item in projection.evidence_items
    }
    return {
        "projection_version": projection.projection_version,
        "values": values,
        "missing_requirement_ids": list(projection.missing_requirement_ids),
        "conflicts": list(projection.conflicts),
        "correction_ids": list(projection.correction_ids),
    }
