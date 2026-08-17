"""Deterministic format regression suite for reports output adapters."""

from __future__ import annotations

from io import BytesIO
import json
from typing import Any
from typing import cast
from pathlib import Path
from zipfile import ZipFile
from dataclasses import asdict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.models import ReportLineageModel
from services.reports.app.repository import ReportsRepository
from services.reports.app.csv_renderer import render_report_csv
from services.reports.app.pdf_renderer import render_report_pdf
from services.reports.app.audit_package import read_manifest_from_zip
from services.reports.app.audit_package import render_audit_package_zip
from services.reports.app.audit_package import build_audit_package_zip_bytes
from services.reports.app.excel_renderer import render_report_excel

FIXTURE_PATH = Path("tests/fixtures/reports/format_regression_baseline.json")


def test_reports_format_regression_supported_formats_match_baseline() -> None:
    fixture = _load_fixture()
    lineage = _lineage_from_fixture(fixture=fixture)
    baseline = _as_object(fixture["regression_baseline"])

    actual_pdf = asdict(
        render_report_pdf(
            report_id=lineage.report_id,
            report_version_id=lineage.report_version_id,
            artifact_kind="tax_summary",
            report_type="income_tax_summary",
            tax_year=lineage.tax_year,
            lineage=lineage,
        )
    )
    actual_xlsx = asdict(
        render_report_excel(
            report_id=lineage.report_id,
            report_version_id=lineage.report_version_id,
            artifact_kind="worksheet",
            report_type="income_tax_worksheet",
            tax_year=lineage.tax_year,
            lineage=lineage,
        )
    )
    actual_csv = asdict(
        render_report_csv(
            report_id=lineage.report_id,
            report_version_id=lineage.report_version_id,
            artifact_kind="tax_summary",
            report_type="income_tax_summary",
            tax_year=lineage.tax_year,
            lineage=lineage,
        )
    )
    actual_zip = asdict(
        render_audit_package_zip(
            report_id=lineage.report_id,
            report_version_id=lineage.report_version_id,
            artifact_kind="audit_package",
            report_type="income_tax_audit_package_manifest",
            tax_year=lineage.tax_year,
            lineage=lineage,
        )
    )

    _assert_baseline_match(actual=actual_pdf, expected=_as_object(baseline["pdf"]), label="pdf")
    _assert_baseline_match(actual=actual_xlsx, expected=_as_object(baseline["xlsx"]), label="xlsx")
    _assert_baseline_match(actual=actual_csv, expected=_as_object(baseline["csv"]), label="csv")
    _assert_baseline_match(
        actual={k: v for k, v in actual_zip.items() if k != "content_sha256"},
        expected=_as_object(baseline["zip"]),
        label="zip_metadata",
    )

    zip_bytes = build_audit_package_zip_bytes(
        report_id=lineage.report_id,
        report_version_id=lineage.report_version_id,
        lineage=lineage,
        source_artifacts=_source_artifacts(),
    )
    with ZipFile(BytesIO(zip_bytes), mode="r") as archive:
        actual_entries = list(archive.namelist())
    actual_manifest = read_manifest_from_zip(zip_bytes=zip_bytes)
    assert actual_entries == baseline["zip_entry_list"]
    _assert_baseline_match(
        actual=actual_manifest,
        expected=_as_object(baseline["zip_manifest"]),
        label="zip_manifest",
    )


def test_reports_format_regression_repeated_generation_is_deterministic() -> None:
    fixture = _load_fixture()
    lineage = _lineage_from_fixture(fixture=fixture)

    first = asdict(
        render_report_csv(
            report_id=lineage.report_id,
            report_version_id=lineage.report_version_id,
            artifact_kind="tax_summary",
            report_type="income_tax_summary",
            tax_year=lineage.tax_year,
            lineage=lineage,
        )
    )
    second = asdict(
        render_report_csv(
            report_id=lineage.report_id,
            report_version_id=lineage.report_version_id,
            artifact_kind="tax_summary",
            report_type="income_tax_summary",
            tax_year=lineage.tax_year,
            lineage=lineage,
        )
    )
    assert first == second


def test_reports_format_regression_unsupported_format_rejected_canonically() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["format"] = "yaml"

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-format-regression-unsupported-format"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 409
    assert detail["error_code"] == "report_generation_not_supported"
    assert detail["reason"] == "report_generation_not_supported"
    assert detail["reason_code"] == "report_generation_not_supported"


def test_reports_format_regression_unsupported_scope_rejected_canonically() -> None:
    app = _fresh_app()
    with TestClient(app) as client:
        response = client.get(
            "/v1/reports/vat/artifacts",
            headers={"X-Correlation-ID": "reports-format-regression-unsupported-scope"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "unsupported_report_scope"
    assert detail["reason"] == "unsupported_report_scope"
    assert detail["reason_code"] == "unsupported_report_scope"


def test_reports_format_regression_drift_guard_emits_clear_failure() -> None:
    with pytest.raises(AssertionError, match="baseline drift for `pdf`"):
        _assert_baseline_match(
            actual={"format": "pdf", "artifact_kind": "tax_summary"},
            expected={"format": "pdf", "artifact_kind": "worksheet"},
            label="pdf",
        )


def _load_fixture() -> dict[str, object]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _lineage_from_fixture(*, fixture: dict[str, object]) -> ReportLineageModel:
    lineage_input = _as_object(fixture["lineage_input"])
    return ReportLineageModel(
        computation_id=str(lineage_input["computation_id"]),
        form_id=str(lineage_input["form_id"]),
        report_id=str(lineage_input["report_id"]),
        report_version_id=str(lineage_input["report_version_id"]),
        historical_version_id=str(lineage_input["historical_version_id"]),
        supported_lane_id=str(lineage_input["supported_lane_id"]),
        tax_type=str(lineage_input["tax_type"]),
        tax_year=_as_int(lineage_input["tax_year"]),
        policy_anchor_ids=tuple(_as_list(lineage_input["policy_anchor_ids"])),
        source_anchor_ids=tuple(_as_list(lineage_input["source_anchor_ids"])),
    )


def _assert_baseline_match(
    *,
    actual: dict[str, object],
    expected: dict[str, object],
    label: str,
) -> None:
    if actual != expected:
        raise AssertionError(
            f"baseline drift for `{label}`: expected={expected!r}, actual={actual!r}"
        )


def _source_artifacts() -> dict[str, dict[str, object]]:
    return {
        "summary": {"status": "generated"},
        "worksheet": {"status": "generated"},
        "exports": {"available_formats": ["pdf", "xlsx", "csv", "zip"]},
    }


def _fresh_app() -> FastAPI:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    repository.reset()
    return app


def _valid_generation_payload() -> dict[str, object]:
    return {
        "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
        "form_id": "f3f640ca-a99f-5126-84e1-c2fd59ea8ce8",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "resident_employment_income_2023_07_01",
    }


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _error_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_object = cast(dict[str, object], detail)
    assert {"error_code", "message", "reason", "reason_code"}.issubset(detail_object.keys())
    return detail_object


def _as_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _as_list(value: object) -> list[str]:
    assert isinstance(value, list)
    value_list = cast(list[object], value)
    normalized = [str(item) for item in value_list]
    return normalized


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise AssertionError("Expected integer-compatible value.")
