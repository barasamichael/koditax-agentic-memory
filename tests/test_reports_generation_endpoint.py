"""Deterministic tests for reports generation endpoint (Phase 9.2.2)."""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from services.reports.app.audit import ReportsAuditEmitter
from shared.determinism.input_hash import canonical_json_dumps
import services.reports.app.generation as generation_module
from services.reports.app.repository import ReportsRepository
from services.reports.app.repository import FinalizedLineageReference


def test_reports_generation_success_from_finalized_lineage_is_deterministic() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()

    with TestClient(app) as client:
        first = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-generate-corr"},
        )
        second = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-generate-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first_payload["status"] == "generated"
    assert first_payload["report_type"] == "income_tax_summary"
    assert first_payload["report_id"] == second_payload["report_id"]
    assert first_payload["report_version_id"] == second_payload["report_version_id"]
    artifact_metadata_value = first_payload["artifact_metadata"]
    assert isinstance(artifact_metadata_value, dict)
    artifact_metadata = _as_object(cast(dict[str, object], artifact_metadata_value))
    assert artifact_metadata["format"] == "pdf"
    assert artifact_metadata["artifact_kind"] == "tax_summary"
    assert artifact_metadata["report_id"] == first_payload["report_id"]
    assert artifact_metadata["report_version_id"] == first_payload["report_version_id"]
    lineage_reference_value = first_payload["lineage_reference"]
    second_lineage_reference_value = second_payload["lineage_reference"]
    assert isinstance(lineage_reference_value, dict)
    assert isinstance(second_lineage_reference_value, dict)
    typed_lineage_reference = cast(dict[str, object], lineage_reference_value)
    typed_second_lineage_reference = cast(dict[str, object], second_lineage_reference_value)
    assert canonical_json_dumps(typed_lineage_reference) == canonical_json_dumps(
        typed_second_lineage_reference
    )
    second_artifact_metadata_value = second_payload["artifact_metadata"]
    assert isinstance(second_artifact_metadata_value, dict)
    typed_second_artifact_metadata = cast(dict[str, object], second_artifact_metadata_value)
    assert canonical_json_dumps(artifact_metadata) == canonical_json_dumps(
        typed_second_artifact_metadata
    )

    lineage = _as_object(typed_lineage_reference)
    required_lineage_fields = {
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
    }
    assert required_lineage_fields.issubset(lineage)
    events = _audit_events(app)
    assert len(events) == 2
    assert str(events[0]["event_type"]) == "report_generated"
    assert str(events[1]["event_type"]) == "report_generated"
    repository = _repository(app)
    snapshot = repository.snapshot_persisted_reports()
    assert len(snapshot) == 1


def test_reports_generation_missing_lineage_fields_rejected_canonically() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload.pop("supported_lane_id")

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-missing-lineage-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 400
    assert detail["error_code"] == "invalid_lineage_reference"
    assert detail["reason"] == "invalid_lineage_reference"
    assert detail["reason_code"] == "invalid_lineage_reference"
    events = _audit_events(app)
    assert len(events) == 1
    assert str(events[0]["event_type"]) == "report_generation_failed"
    assert str(events[0]["error_code"]) == "invalid_lineage_reference"


def test_reports_generation_unsupported_lane_rejected_deterministically() -> None:
    app = _fresh_app()
    repository = _repository(app)
    repository.register_finalized_lineage_reference(
        reference=FinalizedLineageReference(
            computation_id="3f5ae7cd-6254-5cd4-9f2e-38f6714f3bd5",
            form_id="9cfd12c5-96c0-5f3a-80af-8e6b44b88fe7",
            historical_version_id="KIT-VER-20230701-A",
            supported_lane_id="unsupported_lane_stub",
            tax_year=2023,
            tax_type="income_tax",
            policy_anchor_ids=("POL-001",),
            source_anchor_ids=("SRC-001",),
        )
    )

    payload = {
        "computation_id": "3f5ae7cd-6254-5cd4-9f2e-38f6714f3bd5",
        "form_id": "9cfd12c5-96c0-5f3a-80af-8e6b44b88fe7",
        "report_type": "income_tax_summary",
        "tax_year": 2023,
        "historical_version_id": "KIT-VER-20230701-A",
        "supported_lane_id": "unsupported_lane_stub",
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-unsupported-lane-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 409
    assert detail["error_code"] == "report_generation_not_supported"
    assert detail["reason"] == "report_generation_not_supported"
    assert detail["reason_code"] == "report_generation_not_supported"


def test_reports_generation_unknown_lineage_reference_rejected_deterministically() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["form_id"] = "11111111-1111-1111-1111-111111111111"

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-unknown-lineage-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 404
    assert detail["error_code"] == "invalid_lineage_reference"
    assert detail["reason"] == "invalid_lineage_reference"
    assert detail["reason_code"] == "invalid_lineage_reference"


def test_reports_generation_persists_required_lineage_fields() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-persist-corr"},
        )

    assert response.status_code == 201
    repository = _repository(app)
    snapshot = repository.snapshot_persisted_reports()
    assert len(snapshot) == 1
    persisted = _as_object(snapshot[0])
    lineage = _as_object(persisted["lineage_reference"])
    assert persisted["report_id"] == lineage["report_id"]
    assert lineage["computation_id"] == payload["computation_id"]
    assert lineage["form_id"] == payload["form_id"]
    assert lineage["historical_version_id"] == payload["historical_version_id"]
    assert lineage["supported_lane_id"] == payload["supported_lane_id"]
    assert lineage["tax_year"] == payload["tax_year"]


def test_reports_generation_supports_governed_health_nhif_summary() -> None:
    app = _fresh_app()
    repository = _repository(app)
    repository.register_finalized_lineage_reference(
        reference=FinalizedLineageReference(
            computation_id="3b1d40a8-c1c5-566d-8d4a-a17d14551697",
            form_id="0a7cb837-55dd-58cb-9d00-8bd33cf0532c",
            historical_version_id="HCH-VER-20100716-A",
            supported_lane_id="health_contribution_nhif_legacy_v1_2010_07_16",
            tax_year=2012,
            tax_type="health_contribution",
            policy_anchor_ids=("HCP-POL-106",),
            source_anchor_ids=("HC-NHIF-CONTRIB-REG-2010-07-16",),
        )
    )
    payload = {
        "computation_id": "3b1d40a8-c1c5-566d-8d4a-a17d14551697",
        "form_id": "0a7cb837-55dd-58cb-9d00-8bd33cf0532c",
        "report_type": "health_contribution_summary",
        "tax_year": 2012,
        "historical_version_id": "HCH-VER-20100716-A",
        "supported_lane_id": "health_contribution_nhif_legacy_v1_2010_07_16",
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/health-contribution/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-health-nhif-corr"},
        )

    response_payload = _response_json(response)
    assert response.status_code == 201
    assert response_payload["report_type"] == "health_contribution_summary"
    lineage_reference = _as_object(response_payload["lineage_reference"])
    assert lineage_reference["tax_type"] == "health_contribution"
    assert lineage_reference["historical_version_id"] == "HCH-VER-20100716-A"


def test_reports_generation_supports_governed_health_transition_resolved_summary() -> None:
    app = _fresh_app()
    repository = _repository(app)
    repository.register_finalized_lineage_reference(
        reference=FinalizedLineageReference(
            computation_id="6f2efe3b-7b4b-5f0a-9b6e-d77d4ac4132e",
            form_id="aa230576-57e5-5ab0-a93c-f860db3fd067",
            historical_version_id="HCH-VER-20241001-A",
            supported_lane_id="health_contribution_sha_shif_v1_2024_10_01",
            tax_year=2024,
            tax_type="health_contribution",
            policy_anchor_ids=("HCP-POL-204",),
            source_anchor_ids=("HC-SHI-REG-2024-09-20",),
        )
    )
    payload = {
        "computation_id": "6f2efe3b-7b4b-5f0a-9b6e-d77d4ac4132e",
        "form_id": "aa230576-57e5-5ab0-a93c-f860db3fd067",
        "report_type": "health_contribution_summary",
        "tax_year": 2024,
        "historical_version_id": "HCH-VER-20241001-A",
        "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/health-contribution/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-health-transition-corr"},
        )

    response_payload = _response_json(response)
    assert response.status_code == 201
    lineage_reference = _as_object(response_payload["lineage_reference"])
    assert lineage_reference["historical_version_id"] == "HCH-VER-20241001-A"
    assert lineage_reference["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"


def test_reports_health_metadata_route_returns_generated_health_report() -> None:
    app = _fresh_app()
    payload = {
        "computation_id": "bf80513f-f7dd-5257-9f4d-656eebc2c2f5",
        "form_id": "85bfa98d-e3e9-5829-aad6-047e7dc97f8c",
        "report_type": "health_contribution_summary",
        "tax_year": 2024,
        "historical_version_id": "HCH-VER-20241001-A",
        "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
    }

    with TestClient(app) as client:
        generated = client.post(
            "/v1/reports/health-contribution/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-health-metadata-create-corr"},
        )
        generated_payload = _response_json(generated)
        metadata = client.get(
            f"/v1/reports/health-contribution/artifacts/{generated_payload['report_id']}/metadata",
            headers={"X-Correlation-ID": "reports-health-metadata-fetch-corr"},
        )

    metadata_payload = _response_json(metadata)
    assert generated.status_code == 201
    assert metadata.status_code == 200
    assert metadata_payload["report_type"] == "health_contribution_summary"
    lineage_reference = _as_object(metadata_payload["lineage_reference"])
    assert lineage_reference["tax_type"] == "health_contribution"


def test_reports_generation_non_ready_health_window_rejected_deterministically() -> None:
    app = _fresh_app()
    repository = _repository(app)
    repository.register_finalized_lineage_reference(
        reference=FinalizedLineageReference(
            computation_id="5fe3a700-5297-5908-9ad1-a7074660d50d",
            form_id="14fc8973-9135-5929-a4fd-8aa5e4b4d6e9",
            historical_version_id="HCH-VER-20031205-A",
            supported_lane_id="health_contribution_nhif_legacy_v1_2003_12_05",
            tax_year=2009,
            tax_type="health_contribution",
            policy_anchor_ids=("HCP-POL-U01",),
            source_anchor_ids=("HC-NHIF-CONTRIB-REG-2003-12-05",),
        )
    )
    payload = {
        "computation_id": "5fe3a700-5297-5908-9ad1-a7074660d50d",
        "form_id": "14fc8973-9135-5929-a4fd-8aa5e4b4d6e9",
        "report_type": "health_contribution_summary",
        "tax_year": 2009,
        "historical_version_id": "HCH-VER-20031205-A",
        "supported_lane_id": "health_contribution_nhif_legacy_v1_2003_12_05",
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/health-contribution/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-health-2003-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 409
    assert detail["error_code"] == "report_generation_not_supported"
    assert detail["reason"] == "report_generation_not_supported"
    assert detail["reason_code"] == "report_generation_not_supported"


def test_reports_generation_worksheet_pdf_metadata_is_returned() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["report_type"] = "income_tax_worksheet"
    payload["format"] = "xlsx"

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-worksheet-corr"},
        )

    response_payload = _response_json(response)
    assert response.status_code == 201
    artifact_metadata = _as_object(response_payload["artifact_metadata"])
    assert artifact_metadata["format"] == "xlsx"
    assert artifact_metadata["artifact_kind"] == "worksheet"
    assert artifact_metadata["report_id"] == response_payload["report_id"]
    assert artifact_metadata["report_version_id"] == response_payload["report_version_id"]


def test_reports_generation_summary_csv_metadata_is_returned() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["format"] = "csv"

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-summary-csv-corr"},
        )

    response_payload = _response_json(response)
    assert response.status_code == 201
    artifact_metadata = _as_object(response_payload["artifact_metadata"])
    assert artifact_metadata["format"] == "csv"
    assert artifact_metadata["artifact_kind"] == "tax_summary"
    assert artifact_metadata["report_id"] == response_payload["report_id"]
    assert artifact_metadata["report_version_id"] == response_payload["report_version_id"]


def test_reports_generation_audit_package_zip_metadata_is_returned() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["report_type"] = "income_tax_audit_package_manifest"
    payload["format"] = "zip"

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-audit-zip-corr"},
        )

    response_payload = _response_json(response)
    assert response.status_code == 201
    artifact_metadata = _as_object(response_payload["artifact_metadata"])
    assert artifact_metadata["format"] == "zip"
    assert artifact_metadata["artifact_kind"] == "audit_package"
    assert artifact_metadata["report_id"] == response_payload["report_id"]
    assert artifact_metadata["report_version_id"] == response_payload["report_version_id"]


def test_reports_generation_renderer_internal_failure_maps_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["format"] = "xlsx"

    def _raise_render_error(**_: object) -> object:
        raise generation_module.ReportExcelRenderingError(
            reason_code="report_rendering_failed",
            message="Failed to render report artifact as Excel workbook.",
        )

    monkeypatch.setattr(generation_module, "render_report_excel", _raise_render_error)
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-render-fail-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 503
    assert detail["error_code"] == "report_rendering_failed"
    assert detail["reason"] == "report_rendering_failed"
    assert detail["reason_code"] == "report_rendering_failed"


def test_reports_generation_zip_packaging_failure_maps_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["report_type"] = "income_tax_audit_package_manifest"
    payload["format"] = "zip"

    def _raise_packaging_error(**_: object) -> object:
        raise generation_module.ReportAuditPackageError(
            reason_code="report_packaging_failed",
            message="Failed to build deterministic audit package zip.",
        )

    monkeypatch.setattr(generation_module, "render_audit_package_zip", _raise_packaging_error)
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-zip-packaging-fail-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 503
    assert detail["error_code"] == "report_packaging_failed"
    assert detail["reason"] == "report_packaging_failed"
    assert detail["reason_code"] == "report_packaging_failed"


def test_reports_generation_csv_renderer_internal_failure_maps_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["format"] = "csv"

    def _raise_render_error(**_: object) -> object:
        raise generation_module.ReportCsvRenderingError(
            reason_code="report_rendering_failed",
            message="Failed to render report artifact as CSV export.",
        )

    monkeypatch.setattr(generation_module, "render_report_csv", _raise_render_error)
    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-csv-render-fail-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 503
    assert detail["error_code"] == "report_rendering_failed"
    assert detail["reason"] == "report_rendering_failed"
    assert detail["reason_code"] == "report_rendering_failed"


def test_reports_generation_unsupported_output_format_rejected_deterministically() -> None:
    app = _fresh_app()
    payload = _valid_generation_payload()
    payload["format"] = "xml"

    with TestClient(app) as client:
        response = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers={"X-Correlation-ID": "reports-unsupported-format-corr"},
        )

    detail = _error_detail(_response_json(response))
    assert response.status_code == 409
    assert detail["error_code"] == "report_generation_not_supported"
    assert detail["reason"] == "report_generation_not_supported"
    assert detail["reason_code"] == "report_generation_not_supported"


def _fresh_app() -> FastAPI:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = _repository(app)
    repository.reset()
    emitter = getattr(app.state, "reports_audit_emitter", None)
    assert isinstance(emitter, ReportsAuditEmitter)
    return app


def _repository(app: FastAPI) -> ReportsRepository:
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    return repository


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


def _audit_events(app: FastAPI) -> tuple[dict[str, object], ...]:
    emitter = getattr(app.state, "reports_audit_emitter", None)
    assert isinstance(emitter, ReportsAuditEmitter)
    return emitter.snapshot()
