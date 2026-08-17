"""Tax-owned evidence interpretation and P9 reconciliation."""

from __future__ import annotations

from typing import cast

from shared.workflow_evidence_projection import WorkflowEvidenceProjection


class TaxEvidenceProjectionError(ValueError):
    """Raised when a tax workflow cannot safely consume its projection."""


def reconcile_p9_totals(*, projection: WorkflowEvidenceProjection) -> dict[str, object]:
    """Reconcile P9 figures from tax projection evidence, not a document record."""

    if projection.workflow != "tax":
        raise TaxEvidenceProjectionError("P9 reconciliation requires a tax projection.")
    values = {item.requirement_id: item.effective_value for item in projection.evidence_items}
    gross_monthly = _numbers(values.get("monthly_gross_pay"), "monthly_gross_pay")
    paye_monthly = _numbers(values.get("monthly_paye"), "monthly_paye")
    total_gross = _number(values.get("total_gross_pay"), "total_gross_pay")
    total_paye = _number(values.get("total_paye"), "total_paye")
    calculated_gross, calculated_paye = sum(gross_monthly), sum(paye_monthly)
    gross_matches = abs(total_gross - calculated_gross) <= 0.01
    paye_matches = abs(total_paye - calculated_paye) <= 0.01
    return {
        "calculation_kind": "p9_total_reconciliation",
        "projection_version": projection.projection_version,
        "evidence_ids": [item.evidence_id for item in projection.evidence_items],
        "gross_pay": {
            "reported": total_gross,
            "calculated_from_monthly": calculated_gross,
            "matches": gross_matches,
        },
        "paye": {
            "reported": total_paye,
            "calculated_from_monthly": calculated_paye,
            "matches": paye_matches,
        },
        "reconciliation_status": "matched"
        if gross_matches and paye_matches
        else "mismatch",
    }


def _numbers(value: object, name: str) -> list[float]:
    if not isinstance(value, list | tuple):
        raise TaxEvidenceProjectionError(f"{name} must be a numeric list.")
    values = cast(list[object] | tuple[object, ...], value)
    return [_number(item, name) for item in values]


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TaxEvidenceProjectionError(f"{name} must be numeric.")
    return float(value)
