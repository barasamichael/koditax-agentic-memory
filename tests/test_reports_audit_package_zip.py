"""Deterministic tests for reports audit-package ZIP builder."""

from __future__ import annotations

from io import BytesIO
from typing import cast
from zipfile import ZipFile

import pytest

from services.reports.app.models import ReportLineageModel
from services.reports.app.audit_package import read_manifest_from_zip
from services.reports.app.audit_package import ReportAuditPackageError
from services.reports.app.audit_package import AUDIT_PACKAGE_FILE_ORDER
from services.reports.app.audit_package import render_audit_package_zip
from services.reports.app.audit_package import build_audit_package_zip_bytes


def test_audit_package_zip_has_expected_folder_and_file_structure() -> None:
    zip_bytes = build_audit_package_zip_bytes(
        report_id="8c8d635b-f012-40ad-a747-7899bbf015a1",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        lineage=_lineage(report_id="8c8d635b-f012-40ad-a747-7899bbf015a1"),
        source_artifacts=_source_artifacts(),
    )
    with ZipFile(BytesIO(zip_bytes), mode="r") as archive:
        assert tuple(archive.namelist()) == AUDIT_PACKAGE_FILE_ORDER


def test_audit_package_manifest_contains_required_lineage_fields() -> None:
    zip_bytes = build_audit_package_zip_bytes(
        report_id="0ccf7276-daee-4d93-89d6-b1db5b41de92",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        lineage=_lineage(report_id="0ccf7276-daee-4d93-89d6-b1db5b41de92"),
        source_artifacts=_source_artifacts(),
    )
    manifest = read_manifest_from_zip(zip_bytes=zip_bytes)
    assert manifest["report_id"] == "0ccf7276-daee-4d93-89d6-b1db5b41de92"
    assert manifest["report_version_id"] == "ITX-RPT-20230701-RES-EMP-V1"
    assert manifest["computation_id"] == "c63cd26d-6d34-545a-833f-ca7888856670"
    assert manifest["form_id"] == "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8"
    assert manifest["included_files"] == list(AUDIT_PACKAGE_FILE_ORDER)


def test_audit_package_zip_is_deterministic_for_identical_input() -> None:
    lineage = _lineage(report_id="d7a94755-c14f-46a9-aad3-20f576c79b6e")
    first = build_audit_package_zip_bytes(
        report_id="d7a94755-c14f-46a9-aad3-20f576c79b6e",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        lineage=lineage,
        source_artifacts=_source_artifacts(),
    )
    second = build_audit_package_zip_bytes(
        report_id="d7a94755-c14f-46a9-aad3-20f576c79b6e",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        lineage=lineage,
        source_artifacts=_source_artifacts(),
    )
    with ZipFile(BytesIO(first), mode="r") as archive_first:
        with ZipFile(BytesIO(second), mode="r") as archive_second:
            assert tuple(archive_first.namelist()) == tuple(archive_second.namelist())
            assert archive_first.read("lineage/manifest.json") == archive_second.read(
                "lineage/manifest.json"
            )


def test_audit_package_missing_required_source_artifact_rejected_canonically() -> None:
    source_artifacts = _source_artifacts()
    source_artifacts.pop("worksheet")
    with pytest.raises(ReportAuditPackageError) as error_info:
        build_audit_package_zip_bytes(
            report_id="8f4a3689-a5dd-47dc-b001-5808f8036e67",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            lineage=_lineage(report_id="8f4a3689-a5dd-47dc-b001-5808f8036e67"),
            source_artifacts=source_artifacts,
        )
    assert error_info.value.reason_code == "report_packaging_failed"


def test_audit_package_renderer_unsupported_artifact_kind_rejected_canonically() -> None:
    with pytest.raises(ReportAuditPackageError) as error_info:
        render_audit_package_zip(
            report_id="74b8a206-0053-436f-b74d-681d506f0f0c",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="worksheet",
            report_type="income_tax_audit_package_manifest",
            tax_year=2023,
            lineage=_lineage(report_id="74b8a206-0053-436f-b74d-681d506f0f0c"),
        )
    assert error_info.value.reason_code == "report_generation_not_supported"


def test_audit_package_renderer_internal_failure_maps_to_packaging_reason() -> None:
    invalid_source_anchor_ids = cast(tuple[str, ...], ("SRC-001", 9))
    invalid_lineage = ReportLineageModel(
        computation_id="c63cd26d-6d34-545a-833f-ca7888856670",
        form_id="f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        report_id="67f15e8f-1f22-4954-b49d-0ba9f947fff5",
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        historical_version_id="KIT-VER-20230701-A",
        supported_lane_id="resident_employment_income_2023_07_01",
        tax_type="income_tax",
        tax_year=2023,
        policy_anchor_ids=("POL-001",),
        source_anchor_ids=invalid_source_anchor_ids,
    )
    with pytest.raises(ReportAuditPackageError) as error_info:
        render_audit_package_zip(
            report_id="67f15e8f-1f22-4954-b49d-0ba9f947fff5",
            report_version_id="ITX-RPT-20230701-RES-EMP-V1",
            artifact_kind="audit_package",
            report_type="income_tax_audit_package_manifest",
            tax_year=2023,
            lineage=invalid_lineage,
        )
    assert error_info.value.reason_code == "report_packaging_failed"


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


def _source_artifacts() -> dict[str, dict[str, object]]:
    return {
        "summary": {"status": "generated"},
        "worksheet": {"status": "generated"},
        "exports": {"available_formats": ["pdf", "xlsx", "csv", "zip"]},
    }
