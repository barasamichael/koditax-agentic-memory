"""Deterministic tests for reports CSV renderer adapter."""

from __future__ import annotations

from io import StringIO
import csv
from typing import cast

import pytest

from services.reports.app.models import ReportLineageModel
from services.reports.app.csv_renderer import CSV_HEADERS
from services.reports.app.csv_renderer import render_report_csv
from services.reports.app.csv_renderer import build_report_csv_rows
from services.reports.app.csv_renderer import build_report_csv_bytes
from services.reports.app.csv_renderer import ReportCsvRenderingError


def test_csv_renderer_returns_canonical_header_order() -> None:
    csv_bytes = build_report_csv_bytes(
        report_id="fb7849c9-5a65-48cb-bd17-2850f10a4e92",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="tax_summary",
        report_type="income_tax_summary",
        tax_year=2023,
        lineage=_lineage(report_id="fb7849c9-5a65-48cb-bd17-2850f10a4e92"),
    )
    parsed_rows = list(csv.reader(StringIO(csv_bytes.decode("utf-8"))))
    assert tuple(parsed_rows[0]) == CSV_HEADERS


def test_csv_renderer_row_ordering_is_deterministic() -> None:
    lineage = _lineage(report_id="b2d79599-1088-4fe8-ba8b-aa64f26a1ebb")
    first = build_report_csv_rows(
        report_id="b2d79599-1088-4fe8-ba8b-aa64f26a1ebb",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="worksheet",
        report_type="income_tax_worksheet",
        tax_year=2023,
        lineage=lineage,
    )
    second = build_report_csv_rows(
        report_id="b2d79599-1088-4fe8-ba8b-aa64f26a1ebb",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="worksheet",
        report_type="income_tax_worksheet",
        tax_year=2023,
        lineage=lineage,
    )
    assert first == second


def test_csv_renderer_numeric_and_date_style_fields_are_stable() -> None:
    rows = build_report_csv_rows(
        report_id="fcf9f971-c7ff-4460-a96c-b3665d6e02e8",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        artifact_kind="tax_summary",
        report_type="income_tax_summary",
        tax_year=2023,
        lineage=_lineage(report_id="fcf9f971-c7ff-4460-a96c-b3665d6e02e8"),
    )
    report_rows = [row for row in rows if row[0] == "report"]
    first_report_row = report_rows[0]
    assert first_report_row[6] == "2023"
    assert first_report_row[7] == "KIT-VER-20230701-A"


def test_csv_renderer_unsupported_artifact_kind_rejected_canonically() -> None:
    with pytest.raises(ReportCsvRenderingError) as error_info:
        render_report_csv(
            report_id="021d09e0-8478-478f-b111-3990d8f5377d",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="audit_package",
            report_type="income_tax_summary",
            tax_year=2023,
            lineage=_lineage(report_id="021d09e0-8478-478f-b111-3990d8f5377d"),
        )
    assert error_info.value.reason_code == "report_generation_not_supported"


def test_csv_renderer_internal_failure_maps_to_canonical_reason() -> None:
    invalid_policy_anchor_ids = cast(tuple[str, ...], ("POL-001", 7))
    invalid_lineage = ReportLineageModel(
        computation_id="c63cd26d-6d34-545a-833f-ca7888856670",
        form_id="f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        report_id="2f5db503-6518-4fdb-91cb-3bcbcc0d4772",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        historical_version_id="KIT-VER-20230701-A",
        supported_lane_id="resident_employment_income_2023_07_01",
        tax_type="income_tax",
        tax_year=2023,
        policy_anchor_ids=invalid_policy_anchor_ids,
        source_anchor_ids=("SRC-001",),
    )
    with pytest.raises(ReportCsvRenderingError) as error_info:
        render_report_csv(
            report_id="2f5db503-6518-4fdb-91cb-3bcbcc0d4772",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="tax_summary",
            report_type="income_tax_summary",
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
        policy_anchor_ids=("POL-001", "POL-002"),
        source_anchor_ids=("SRC-002", "SRC-001"),
    )
