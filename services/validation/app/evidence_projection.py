"""Compliance-owned evaluation input boundary."""

from __future__ import annotations

from shared.workflow_evidence_projection import WorkflowEvidenceProjection


def compliance_inputs_from_projection(
    *, projection: WorkflowEvidenceProjection
) -> dict[str, object]:
    """Prepare compliance inputs without interpreting raw document payloads."""

    if projection.workflow != "compliance":
        raise ValueError("Compliance workflow requires a compliance evidence projection.")
    return {item.requirement_id: item.effective_value for item in projection.evidence_items}
