"""Deterministic PDF renderer adapter for supported income-tax report artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportArtifactMetadataModel
from shared.determinism.input_hash import canonical_json_dumps

SUPPORTED_PDF_ARTIFACT_KINDS: frozenset[str] = frozenset({"tax_summary", "worksheet"})


class ReportPdfRenderingError(RuntimeError):
    """Represent deterministic PDF rendering failures."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def render_report_pdf(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> ReportArtifactMetadataModel:
    """Render deterministic pseudo-PDF content and return canonical metadata."""

    normalized_kind = artifact_kind.strip().lower()
    if normalized_kind not in SUPPORTED_PDF_ARTIFACT_KINDS:
        raise ReportPdfRenderingError(
            reason_code="report_generation_not_supported",
            message="Requested PDF artifact kind is not supported.",
        )
    try:
        pdf_bytes = _build_pdf_bytes(
            report_id=report_id,
            report_version_id=report_version_id,
            artifact_kind=normalized_kind,
            report_type=report_type,
            tax_year=tax_year,
            lineage=lineage,
        )
    except ReportPdfRenderingError:
        raise
    except Exception as error:  # pragma: no cover - defensive canonical mapping
        raise ReportPdfRenderingError(
            reason_code="report_rendering_failed",
            message="Failed to render report artifact as PDF.",
        ) from error

    return ReportArtifactMetadataModel(
        format="pdf",
        artifact_kind=normalized_kind,
        report_id=report_id,
        report_version_id=report_version_id,
        content_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )


def _build_pdf_bytes(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> bytes:
    _validate_lineage_for_rendering(lineage=lineage)
    lineage_payload = asdict(lineage)
    sorted_lineage = {key: lineage_payload[key] for key in sorted(lineage_payload)}
    content_payload = {
        "header": {
            "report_id": report_id,
            "report_version_id": report_version_id,
            "artifact_kind": artifact_kind,
            "report_type": report_type,
            "tax_year": tax_year,
        },
        "lineage": sorted_lineage,
        "sections": _sections_for_kind(artifact_kind=artifact_kind, tax_year=tax_year),
    }
    serialized = canonical_json_dumps(content_payload)
    return f"%PDF-1.4\n{serialized}\n%%EOF".encode()


def _validate_lineage_for_rendering(*, lineage: ReportLineageModel) -> None:
    for anchor in lineage.policy_anchor_ids:
        if not anchor.strip():
            raise ValueError("policy_anchor_ids must contain non-empty strings.")
    for anchor in lineage.source_anchor_ids:
        if not anchor.strip():
            raise ValueError("source_anchor_ids must contain non-empty strings.")


def _sections_for_kind(*, artifact_kind: str, tax_year: int) -> tuple[dict[str, object], ...]:
    if artifact_kind == "tax_summary":
        return (
            {"section": "summary_overview", "title": f"Tax Summary {tax_year}"},
            {"section": "lineage_trace", "title": "Lineage Traceability"},
        )
    return (
        {"section": "worksheet_inputs", "title": f"Worksheet Inputs {tax_year}"},
        {"section": "worksheet_outputs", "title": "Worksheet Outputs"},
        {"section": "lineage_trace", "title": "Lineage Traceability"},
    )
