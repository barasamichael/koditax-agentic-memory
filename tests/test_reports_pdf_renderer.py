"""Deterministic tests for reports PDF renderer adapter."""

from __future__ import annotations

from typing import cast

import pytest

from services.reports.app.models import ReportLineageModel
from services.reports.app.pdf_renderer import render_report_pdf
from services.reports.app.pdf_renderer import ReportPdfRenderingError


def test_pdf_renderer_tax_summary_metadata_is_deterministic() -> None:
    lineage = _lineage(report_id="9b8947f6-5f59-4893-85f7-f6ac3f7aa3d2")
    first = render_report_pdf(
        report_id="9b8947f6-5f59-4893-85f7-f6ac3f7aa3d2",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="tax_summary",
        report_type="income_tax_summary",
        tax_year=2023,
        lineage=lineage,
    )
    second = render_report_pdf(
        report_id="9b8947f6-5f59-4893-85f7-f6ac3f7aa3d2",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="tax_summary",
        report_type="income_tax_summary",
        tax_year=2023,
        lineage=lineage,
    )
    assert first.format == "pdf"
    assert first.artifact_kind == "tax_summary"
    assert first.report_id == "9b8947f6-5f59-4893-85f7-f6ac3f7aa3d2"
    assert first.report_version_id == "ITX-RPT-20230701-RES-EMP-V1"
    assert first.content_sha256 == second.content_sha256


def test_pdf_renderer_worksheet_metadata_is_generated() -> None:
    artifact = render_report_pdf(
        report_id="f806bf4a-45c4-455b-bb2f-f32f6a11b0da",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="worksheet",
        report_type="income_tax_worksheet",
        tax_year=2023,
        lineage=_lineage(report_id="f806bf4a-45c4-455b-bb2f-f32f6a11b0da"),
    )
    assert artifact.format == "pdf"
    assert artifact.artifact_kind == "worksheet"
    assert len(artifact.content_sha256) == 64


def test_pdf_renderer_rejects_unsupported_artifact_kind() -> None:
    with pytest.raises(ReportPdfRenderingError) as error_info:
        render_report_pdf(
            report_id="3cf44f89-6e2b-4af7-a00a-30545f7889ca",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="audit_package",
            report_type="income_tax_summary",
            tax_year=2023,
            lineage=_lineage(report_id="3cf44f89-6e2b-4af7-a00a-30545f7889ca"),
        )
    assert error_info.value.reason_code == "report_generation_not_supported"


def test_pdf_renderer_internal_failure_maps_to_canonical_reason() -> None:
    with pytest.raises(ReportPdfRenderingError) as error_info:
        render_report_pdf(
            report_id="6d473f5a-53d9-44f8-b7ec-d126a6899f8f",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="tax_summary",
            report_type="income_tax_summary",
            tax_year=2023,
            lineage=_invalid_lineage_for_failure(report_id="6d473f5a-53d9-44f8-b7ec-d126a6899f8f"),
        )
    assert error_info.value.reason_code == "report_rendering_failed"


def _lineage(*, report_id: str) -> ReportLineageModel:
    return ReportLineageModel(
        computation_id="c63cd26d-6d34-545a-833f-ca7888856670",
        form_id="f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        report_id=report_id,
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        historical_version_id="KIT-VER-20230701-A",
        supported_lane_id="resident_employment_income_2023_07_01",
        tax_type="income_tax",
        tax_year=2023,
        policy_anchor_ids=("POL-001",),
        source_anchor_ids=("SRC-001",),
    )


def _invalid_lineage_for_failure(*, report_id: str) -> ReportLineageModel:
    invalid_source_anchor_ids = cast(tuple[str, ...], ("SRC-001", 7))
    return ReportLineageModel(
        computation_id="c63cd26d-6d34-545a-833f-ca7888856670",
        form_id="f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        report_id=report_id,
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        historical_version_id="KIT-VER-20230701-A",
        supported_lane_id="resident_employment_income_2023_07_01",
        tax_type="income_tax",
        tax_year=2023,
        policy_anchor_ids=("POL-001",),
        source_anchor_ids=invalid_source_anchor_ids,
    )
