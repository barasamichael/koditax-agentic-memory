"""Deterministic retention metadata persistence tests for reports repository."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import cast

from services.reports.app.authz import ReportAccessContext
from services.reports.app.models import ReportLineageModel
from services.reports.app.models import ReportGenerationResponseModel
from services.reports.app.repository import ReportsRepository


def test_reports_generated_record_persists_retention_metadata() -> None:
    repository = ReportsRepository(database_url="")
    report_response = _report_response(report_type="income_tax_summary")
    stored_record = repository.create_report_record(
        report=report_response,
        access_context=ReportAccessContext(
            owner_user_id="owner-a",
            tenant_id="tenant-a",
        ),
    )
    retention = _retention_metadata(stored_record.report_payload)
    assert retention["retention_class"] == "tax_summary"
    assert retention["cleanup_status"] == "active"
    created_at = datetime.fromisoformat(stored_record.created_at)
    retention_expires_at = datetime.fromisoformat(str(retention["retention_expires_at"]))
    assert retention_expires_at == created_at + timedelta(days=2555)


def test_reports_retention_metadata_is_deterministic_across_identical_requests() -> None:
    repository = ReportsRepository(database_url="")
    first = repository.create_report_record(
        report=_report_response(report_type="income_tax_worksheet"),
        access_context=ReportAccessContext(owner_user_id="owner-a", tenant_id="tenant-a"),
    )
    second = repository.create_report_record(
        report=_report_response(report_type="income_tax_worksheet"),
        access_context=ReportAccessContext(owner_user_id="owner-a", tenant_id="tenant-a"),
    )
    assert _retention_metadata(first.report_payload) == _retention_metadata(second.report_payload)


def _report_response(*, report_type: str) -> ReportGenerationResponseModel:
    report_id = "d55890da-bf1d-5fdb-a1f5-c89f5b6ed4f2"
    return ReportGenerationResponseModel(
        status="generated",
        report_id=report_id,
        report_type=report_type,
        tax_year=2023,
        report_version_id="ITX-RPT-20230701-RES-EMP-V1",
        lineage_reference=ReportLineageModel(
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
        ),
    )


def _retention_metadata(payload: dict[str, object]) -> dict[str, object]:
    retention = payload.get("retention_metadata")
    assert isinstance(retention, dict)
    return cast(dict[str, object], retention)
