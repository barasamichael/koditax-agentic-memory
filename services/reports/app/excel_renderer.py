"""Deterministic Excel renderer adapter for supported income-tax report artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportArtifactMetadataModel
from shared.determinism.input_hash import canonical_json_dumps

SUPPORTED_EXCEL_ARTIFACT_KINDS: frozenset[str] = frozenset({"tax_summary", "worksheet"})


class ReportExcelRenderingError(RuntimeError):
    """Represent deterministic Excel rendering failures."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def render_report_excel(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> ReportArtifactMetadataModel:
    """Render deterministic pseudo-XLSX content and return canonical metadata."""

    normalized_kind = artifact_kind.strip().lower()
    if normalized_kind not in SUPPORTED_EXCEL_ARTIFACT_KINDS:
        raise ReportExcelRenderingError(
            reason_code="report_generation_not_supported",
            message="Requested Excel artifact kind is not supported.",
        )
    try:
        workbook = build_excel_workbook_structure(
            report_id=report_id,
            report_version_id=report_version_id,
            artifact_kind=normalized_kind,
            report_type=report_type,
            tax_year=tax_year,
            lineage=lineage,
        )
        xlsx_bytes = _build_excel_bytes(
            report_id=report_id,
            report_version_id=report_version_id,
            artifact_kind=normalized_kind,
            workbook=workbook,
        )
    except ReportExcelRenderingError:
        raise
    except Exception as error:  # pragma: no cover - defensive canonical mapping
        raise ReportExcelRenderingError(
            reason_code="report_rendering_failed",
            message="Failed to render report artifact as Excel workbook.",
        ) from error

    return ReportArtifactMetadataModel(
        format="xlsx",
        artifact_kind=normalized_kind,
        report_id=report_id,
        report_version_id=report_version_id,
        content_sha256=hashlib.sha256(xlsx_bytes).hexdigest(),
    )


def build_excel_workbook_structure(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> tuple[dict[str, object], ...]:
    """Build deterministic worksheet structure for supported Excel outputs."""

    _validate_lineage_for_rendering(lineage=lineage)
    return (
        _summary_sheet(
            report_id=report_id,
            report_version_id=report_version_id,
            report_type=report_type,
            artifact_kind=artifact_kind,
            tax_year=tax_year,
        ),
        _worksheet_sheet(
            report_type=report_type,
            artifact_kind=artifact_kind,
            tax_year=tax_year,
        ),
        _lineage_sheet(lineage=lineage),
    )


def _build_excel_bytes(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    workbook: tuple[dict[str, object], ...],
) -> bytes:
    content_payload = {
        "workbook_format": "xlsx",
        "report_id": report_id,
        "report_version_id": report_version_id,
        "artifact_kind": artifact_kind,
        "worksheets": workbook,
    }
    serialized = canonical_json_dumps(content_payload)
    return f"XLSXv1\n{serialized}\nEOF".encode()


def _summary_sheet(
    *,
    report_id: str,
    report_version_id: str,
    report_type: str,
    artifact_kind: str,
    tax_year: int,
) -> dict[str, object]:
    return {
        "name": "Summary",
        "headers": ("field", "value"),
        "rows": (
            ("report_id", report_id),
            ("report_version_id", report_version_id),
            ("report_type", report_type),
            ("artifact_kind", artifact_kind),
            ("tax_year", str(tax_year)),
        ),
    }


def _worksheet_sheet(
    *,
    report_type: str,
    artifact_kind: str,
    tax_year: int,
) -> dict[str, object]:
    if artifact_kind == "tax_summary":
        rows = (
            ("section", "worksheet"),
            ("status", "not_applicable"),
            ("tax_year", str(tax_year)),
        )
    else:
        rows = (
            ("section", "inputs"),
            ("status", "computed"),
            ("report_type", report_type),
            ("tax_year", str(tax_year)),
        )
    return {
        "name": "Worksheet",
        "headers": ("field", "value"),
        "rows": rows,
    }


def _lineage_sheet(*, lineage: ReportLineageModel) -> dict[str, object]:
    lineage_payload = asdict(lineage)
    rows = tuple(
        (key, canonical_json_dumps(lineage_payload[key]))
        for key in (
            "computation_id",
            "form_id",
            "report_id",
            "report_version_id",
            "historical_version_id",
            "supported_lane_id",
            "tax_type",
            "tax_year",
            "policy_anchor_ids",
            "source_anchor_ids",
        )
    )
    return {
        "name": "Lineage",
        "headers": ("field", "value"),
        "rows": rows,
    }


def _validate_lineage_for_rendering(*, lineage: ReportLineageModel) -> None:
    for anchor in lineage.policy_anchor_ids:
        if not anchor.strip():
            raise ValueError("policy_anchor_ids must contain non-empty strings.")
    for anchor in lineage.source_anchor_ids:
        if not anchor.strip():
            raise ValueError("source_anchor_ids must contain non-empty strings.")
