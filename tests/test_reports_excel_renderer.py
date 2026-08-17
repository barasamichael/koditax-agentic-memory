"""Deterministic tests for reports Excel renderer adapter."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest

from services.reports.app.models import ReportLineageModel
from shared.determinism.input_hash import canonical_json_dumps
from services.reports.app.excel_renderer import render_report_excel
from services.reports.app.excel_renderer import ReportExcelRenderingError
from services.reports.app.excel_renderer import build_excel_workbook_structure


def test_excel_renderer_tax_summary_has_fixed_worksheet_mapping() -> None:
    lineage = _lineage(report_id="d6ec49b0-f73b-4cc4-b211-083a26986a31")
    workbook = build_excel_workbook_structure(
        report_id="d6ec49b0-f73b-4cc4-b211-083a26986a31",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="tax_summary",
        report_type="income_tax_summary",
        tax_year=2023,
        lineage=lineage,
    )
    sheet_names = tuple(sheet["name"] for sheet in workbook)
    assert sheet_names == ("Summary", "Worksheet", "Lineage")
    summary_sheet = _as_sheet(workbook[0])
    assert summary_sheet["headers"] == ("field", "value")
    assert summary_sheet["rows"][0] == ("report_id", "d6ec49b0-f73b-4cc4-b211-083a26986a31")


def test_excel_renderer_worksheet_has_stable_headers_and_ordering() -> None:
    workbook = build_excel_workbook_structure(
        report_id="7f8f701f-33cf-4ec8-a7ea-13ff9ba7eb48",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="worksheet",
        report_type="income_tax_worksheet",
        tax_year=2023,
        lineage=_lineage(report_id="7f8f701f-33cf-4ec8-a7ea-13ff9ba7eb48"),
    )
    worksheet_sheet = _as_sheet(workbook[1])
    assert worksheet_sheet["name"] == "Worksheet"
    assert worksheet_sheet["headers"] == ("field", "value")
    assert worksheet_sheet["rows"] == (
        ("section", "inputs"),
        ("status", "computed"),
        ("report_type", "income_tax_worksheet"),
        ("tax_year", "2023"),
    )


def test_excel_renderer_output_hash_is_deterministic() -> None:
    lineage = _lineage(report_id="128f4949-ba4a-4bcc-bc15-795df4ec7ee0")
    first = render_report_excel(
        report_id="128f4949-ba4a-4bcc-bc15-795df4ec7ee0",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="worksheet",
        report_type="income_tax_worksheet",
        tax_year=2023,
        lineage=lineage,
    )
    second = render_report_excel(
        report_id="128f4949-ba4a-4bcc-bc15-795df4ec7ee0",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="worksheet",
        report_type="income_tax_worksheet",
        tax_year=2023,
        lineage=lineage,
    )
    assert first.format == "xlsx"
    assert first.content_sha256 == second.content_sha256


def test_excel_renderer_rejects_unsupported_artifact_kind() -> None:
    with pytest.raises(ReportExcelRenderingError) as error_info:
        render_report_excel(
            report_id="c3324ed3-d53f-4ab7-9b0f-a533bf86716e",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="audit_package",
            report_type="income_tax_summary",
            tax_year=2023,
            lineage=_lineage(report_id="c3324ed3-d53f-4ab7-9b0f-a533bf86716e"),
        )
    assert error_info.value.reason_code == "report_generation_not_supported"


def test_excel_renderer_internal_failure_maps_to_canonical_reason() -> None:
    invalid_source_anchor_ids = cast(tuple[str, ...], ("SRC-001", 9))
    invalid_lineage = ReportLineageModel(
        computation_id="c63cd26d-6d34-545a-833f-ca7888856670",
        form_id="f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        report_id="eadfd47d-6f75-4e2c-a05a-37e852cb1f6c",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        historical_version_id="KIT-VER-20230701-A",
        supported_lane_id="resident_employment_income_2023_07_01",
        tax_type="income_tax",
        tax_year=2023,
        policy_anchor_ids=("POL-001",),
        source_anchor_ids=invalid_source_anchor_ids,
    )
    with pytest.raises(ReportExcelRenderingError) as error_info:
        render_report_excel(
            report_id="eadfd47d-6f75-4e2c-a05a-37e852cb1f6c",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="worksheet",
            report_type="income_tax_worksheet",
            tax_year=2023,
            lineage=invalid_lineage,
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


def _as_sheet(value: dict[str, object]) -> dict[str, Any]:
    assert isinstance(value, dict)
    assert {"name", "headers", "rows"}.issubset(value)
    assert canonical_json_dumps(value)
    return value
