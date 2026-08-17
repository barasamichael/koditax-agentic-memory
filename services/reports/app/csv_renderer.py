"""Deterministic CSV renderer adapter for supported income-tax report artifacts."""

from __future__ import annotations

from io import StringIO
import csv
import hashlib

from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportArtifactMetadataModel

CSV_HEADERS: tuple[str, ...] = (
    "record_type",
    "report_id",
    "report_version_id",
    "artifact_kind",
    "report_type",
    "tax_type",
    "tax_year",
    "historical_version_id",
    "supported_lane_id",
    "key",
    "value",
)
SUPPORTED_CSV_ARTIFACT_KINDS: frozenset[str] = frozenset({"tax_summary", "worksheet"})


class ReportCsvRenderingError(RuntimeError):
    """Represent deterministic CSV rendering failures."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def render_report_csv(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> ReportArtifactMetadataModel:
    """Render deterministic CSV payload and return canonical metadata."""

    normalized_kind = artifact_kind.strip().lower()
    if normalized_kind not in SUPPORTED_CSV_ARTIFACT_KINDS:
        raise ReportCsvRenderingError(
            reason_code="report_generation_not_supported",
            message="Requested CSV artifact kind is not supported.",
        )
    try:
        csv_bytes = build_report_csv_bytes(
            report_id=report_id,
            report_version_id=report_version_id,
            artifact_kind=normalized_kind,
            report_type=report_type,
            tax_year=tax_year,
            lineage=lineage,
        )
    except ReportCsvRenderingError:
        raise
    except Exception as error:  # pragma: no cover - defensive canonical mapping
        raise ReportCsvRenderingError(
            reason_code="report_rendering_failed",
            message="Failed to render report artifact as CSV export.",
        ) from error

    return ReportArtifactMetadataModel(
        format="csv",
        artifact_kind=normalized_kind,
        report_id=report_id,
        report_version_id=report_version_id,
        content_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )


def build_report_csv_rows(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> tuple[tuple[str, ...], ...]:
    """Build deterministic CSV rows with canonical ordering and formatting."""

    _validate_lineage_for_rendering(lineage=lineage)
    common_prefix = (
        report_id,
        report_version_id,
        artifact_kind,
        report_type,
        lineage.tax_type,
        _format_tax_year(tax_year),
        lineage.historical_version_id,
        lineage.supported_lane_id,
    )
    rows: list[tuple[str, ...]] = [
        ("report", *common_prefix, "status", "generated"),
        ("report", *common_prefix, "computation_id", lineage.computation_id),
        ("report", *common_prefix, "form_id", lineage.form_id),
    ]

    for anchor in sorted(lineage.policy_anchor_ids):
        rows.append(("policy_anchor", *common_prefix, "policy_anchor_id", anchor))
    for anchor in sorted(lineage.source_anchor_ids):
        rows.append(("source_anchor", *common_prefix, "source_anchor_id", anchor))

    return tuple(
        sorted(
            rows,
            key=lambda row: (row[0], row[9], row[10]),
        )
    )


def build_report_csv_bytes(
    *,
    report_id: str,
    report_version_id: str,
    artifact_kind: str,
    report_type: str,
    tax_year: int,
    lineage: ReportLineageModel,
) -> bytes:
    """Serialize deterministic CSV bytes with canonical headers and newline rules."""

    rows = build_report_csv_rows(
        report_id=report_id,
        report_version_id=report_version_id,
        artifact_kind=artifact_kind,
        report_type=report_type,
        tax_year=tax_year,
        lineage=lineage,
    )
    output = StringIO(newline="")
    writer = csv.writer(
        output,
        delimiter=",",
        quotechar='"',
        lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerow(CSV_HEADERS)
    writer.writerows(rows)
    return output.getvalue().encode()


def _format_tax_year(value: int) -> str:
    return f"{value:d}"


def _validate_lineage_for_rendering(*, lineage: ReportLineageModel) -> None:
    for anchor in lineage.policy_anchor_ids:
        if not anchor.strip():
            raise ValueError("policy_anchor_ids must contain non-empty strings.")
    for anchor in lineage.source_anchor_ids:
        if not anchor.strip():
            raise ValueError("source_anchor_ids must contain non-empty strings.")
