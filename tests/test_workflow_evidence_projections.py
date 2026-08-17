import json
from typing import cast
from pathlib import Path

from jsonschema.validators import Draft202012Validator

from shared.workflow_evidence_projection import SourceReference
from shared.workflow_evidence_projection import ProjectedEvidenceItem
from shared.workflow_evidence_projection import WorkflowEvidenceProjection
from services.forms.app.evidence_projection import form_fields_from_projection
from services.reports.app.evidence_projection import report_evidence_from_projection
from services.tax_core.app.evidence_projection import reconcile_p9_totals
from services.validation.app.evidence_projection import compliance_inputs_from_projection
from services.forms.app.filing_evidence_projection import filing_inputs_from_projection


def _projection(workflow: str) -> WorkflowEvidenceProjection:
    return WorkflowEvidenceProjection.from_resolved_evidence(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_version="v1",
        evidence_items=[
            ProjectedEvidenceItem(
                evidence_id="ev-gross",
                requirement_id="total_gross_pay",
                effective_value=1200.0,
                source_references=(
                    SourceReference(document_id="doc-1", source_location="page:1"),
                ),
                correction_ids=("correction-1",),
            ),
            ProjectedEvidenceItem(
                evidence_id="ev-monthly-gross",
                requirement_id="monthly_gross_pay",
                effective_value=[100.0] * 12,
            ),
            ProjectedEvidenceItem(
                evidence_id="ev-paye", requirement_id="total_paye", effective_value=120.0
            ),
            ProjectedEvidenceItem(
                evidence_id="ev-monthly-paye",
                requirement_id="monthly_paye",
                effective_value=[10.0] * 12,
            ),
        ],
        missing_requirement_ids=["employer_pin"],
        conflicts=[{"requirement_id": "income", "status": "open"}],
        uncertainty=["low-confidence-address"],
        correction_ids=["correction-1"],
    )


def test_tax_projection_preserves_evidence_governance_and_reconciles_p9() -> None:
    projection = _projection("tax")
    result = reconcile_p9_totals(projection=projection)
    assert result["reconciliation_status"] == "matched"
    assert result["projection_version"] == "1.0.0"
    assert "ev-gross" in cast(list[str], result["evidence_ids"])
    assert projection.evidence_items[0].source_references[0].document_id == "doc-1"
    assert projection.missing_requirement_ids == ("employer_pin",)
    assert projection.correction_ids == ("correction-1",)


def test_non_tax_workflows_consume_their_own_versioned_projections() -> None:
    filing_inputs = filing_inputs_from_projection(projection=_projection("filing"))
    form_fields = form_fields_from_projection(projection=_projection("forms"))
    report_inputs = report_evidence_from_projection(projection=_projection("reports"))
    compliance_inputs = compliance_inputs_from_projection(projection=_projection("compliance"))
    assert (
        filing_inputs["projection_version"] == "1.0.0"
    )
    assert cast(dict[str, object], form_fields["total_gross_pay"])["evidence_id"] == "ev-gross"
    assert report_inputs["conflicts"]
    assert compliance_inputs["total_paye"] == 120.0


def test_workflow_projection_contract_is_valid_and_document_ai_has_no_tax_calculator() -> None:
    schema = json.loads(
        Path("contracts/tools/schemas/workflow_evidence_projection.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    document_ai_main = Path("services/document_ai/app/main.py").read_text()
    assert "reconciliations/p9-totals" not in document_ai_main
