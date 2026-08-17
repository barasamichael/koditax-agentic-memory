"""Reports-owned evidence grouping boundary."""

from __future__ import annotations

from shared.workflow_evidence_projection import WorkflowEvidenceProjection


def report_evidence_from_projection(*, projection: WorkflowEvidenceProjection) -> dict[str, object]:
    """Expose report inputs with provenance; aggregation remains report-owned."""

    if projection.workflow != "reports":
        raise ValueError("Reports workflow requires a reports evidence projection.")
    return {
        "projection_version": projection.projection_version,
        "evidence": [item.model_dump(mode="json") for item in projection.evidence_items],
        "missing_requirement_ids": list(projection.missing_requirement_ids),
        "conflicts": list(projection.conflicts),
        "uncertainty": list(projection.uncertainty),
    }
