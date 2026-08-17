"""End-to-end acceptance scenarios for all governed supported income-tax report lanes."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.reports.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.reports.app.repository import ReportsRepository
from services.reports.app.repository import FinalizedLineageReference

_FIXTURES_DIR = Path("tests/fixtures/reports/e2e")


@dataclass(frozen=True)
class SupportedLaneFixture:
    supported_lane_id: str
    historical_version_id: str
    tax_year: int
    computation_id: str
    form_id: str
    policy_anchor_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> SupportedLaneFixture:
        tax_year_value = payload["tax_year"]
        if not isinstance(tax_year_value, int):
            raise TypeError("Fixture tax_year must be an int.")
        return cls(
            supported_lane_id=str(payload["supported_lane_id"]),
            historical_version_id=str(payload["historical_version_id"]),
            tax_year=tax_year_value,
            computation_id=str(payload["computation_id"]),
            form_id=str(payload["form_id"]),
            policy_anchor_ids=tuple(cast(list[str], payload["policy_anchor_ids"])),
            source_anchor_ids=tuple(cast(list[str], payload["source_anchor_ids"])),
        )


def _load_supported_lane_fixtures() -> tuple[SupportedLaneFixture, ...]:
    fixtures: list[SupportedLaneFixture] = []
    for path in sorted(_FIXTURES_DIR.glob("*.json"), key=lambda item: item.name):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        fixtures.append(SupportedLaneFixture.from_payload(cast(dict[str, object], payload)))
    assert fixtures
    return tuple(fixtures)


SUPPORTED_LANE_FIXTURES = _load_supported_lane_fixtures()


@pytest.mark.parametrize("lane", SUPPORTED_LANE_FIXTURES, ids=lambda lane: lane.supported_lane_id)
def test_reports_e2e_generation_retrieval_capability_flow_for_supported_lane(
    lane: SupportedLaneFixture,
) -> None:
    app = _fresh_app()
    _register_supported_lane(app=app, lane=lane)
    headers = {
        "X-Correlation-ID": f"reports-e2e-{lane.supported_lane_id}",
        "X-User-ID": "owner-e2e",
        "X-Tenant-ID": "tenant-e2e",
    }

    with TestClient(app) as client:
        generated = client.post(
            "/v1/reports/income-tax/artifacts",
            json={
                "computation_id": lane.computation_id,
                "form_id": lane.form_id,
                "report_type": "income_tax_summary",
                "tax_year": lane.tax_year,
                "historical_version_id": lane.historical_version_id,
                "supported_lane_id": lane.supported_lane_id,
            },
            headers=headers,
        )
        generated_payload = _response_json(generated)
        report_id = str(generated_payload["report_id"])
        metadata = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers=headers,
        )

    metadata_payload = _response_json(metadata)
    assert generated.status_code == 201
    assert metadata.status_code == 200
    assert generated_payload["status"] == "generated"
    assert metadata_payload["status"] == "ok"
    assert generated_payload["report_id"] == metadata_payload["report_id"]
    assert generated_payload["report_version_id"] == metadata_payload["report_version_id"]
    generated_lineage = _as_object(generated_payload["lineage_reference"])
    metadata_lineage = _as_object(metadata_payload["lineage_reference"])
    assert generated_lineage["supported_lane_id"] == lane.supported_lane_id
    assert metadata_lineage["supported_lane_id"] == lane.supported_lane_id
    assert metadata_lineage["historical_version_id"] == lane.historical_version_id
    assert metadata_lineage["tax_year"] == lane.tax_year
    assert metadata_lineage["computation_id"] == lane.computation_id
    assert metadata_lineage["form_id"] == lane.form_id
    capability = _as_object(metadata_payload["download_capability"])
    assert capability["report_id"] == report_id
    assert capability["capability_id"]
    assert capability["download_url"]
    assert capability["expires_at"]
    created_at = _parse_iso(str(metadata_payload["created_at"]))
    expires_at = _parse_iso(str(capability["expires_at"]))
    assert expires_at >= created_at


def test_reports_e2e_repeated_supported_lane_is_deterministic() -> None:
    lane = SUPPORTED_LANE_FIXTURES[0]
    app = _fresh_app()
    _register_supported_lane(app=app, lane=lane)
    headers = {
        "X-Correlation-ID": "reports-e2e-determinism",
        "X-User-ID": "owner-e2e",
        "X-Tenant-ID": "tenant-e2e",
    }
    payload = {
        "computation_id": lane.computation_id,
        "form_id": lane.form_id,
        "report_type": "income_tax_summary",
        "tax_year": lane.tax_year,
        "historical_version_id": lane.historical_version_id,
        "supported_lane_id": lane.supported_lane_id,
    }

    with TestClient(app) as client:
        first_generate = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers=headers,
        )
        second_generate = client.post(
            "/v1/reports/income-tax/artifacts",
            json=payload,
            headers=headers,
        )

    first_generate_payload = _response_json(first_generate)
    second_generate_payload = _response_json(second_generate)
    assert first_generate.status_code == 201
    assert second_generate.status_code == 201
    assert first_generate_payload["report_id"] == second_generate_payload["report_id"]
    assert (
        first_generate_payload["report_version_id"] == second_generate_payload["report_version_id"]
    )
    assert canonical_json_dumps(
        _as_object(first_generate_payload["lineage_reference"])
    ) == canonical_json_dumps(_as_object(second_generate_payload["lineage_reference"]))


def test_reports_e2e_expiry_mapping_for_supported_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = SUPPORTED_LANE_FIXTURES[0]
    monkeypatch.setenv("REPORTS_REFERENCE_TIME", "2026-01-01T00:20:00+00:00")
    app = _fresh_app()
    _register_supported_lane(app=app, lane=lane)
    headers = {
        "X-Correlation-ID": "reports-e2e-expiry",
        "X-User-ID": "owner-e2e",
        "X-Tenant-ID": "tenant-e2e",
    }

    with TestClient(app) as client:
        generated = client.post(
            "/v1/reports/income-tax/artifacts",
            json={
                "computation_id": lane.computation_id,
                "form_id": lane.form_id,
                "report_type": "income_tax_summary",
                "tax_year": lane.tax_year,
                "historical_version_id": lane.historical_version_id,
                "supported_lane_id": lane.supported_lane_id,
            },
            headers=headers,
        )
        generated_payload = _response_json(generated)
        report_id = str(generated_payload["report_id"])
        first = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers=headers,
        )
        second = client.get(
            f"/v1/reports/income-tax/artifacts/{report_id}/metadata",
            headers=headers,
        )

    first_detail = _error_detail(_response_json(first))
    second_detail = _error_detail(_response_json(second))
    assert first.status_code == 410
    assert first_detail["error_code"] == "report_artifact_expired"
    assert first_detail["reason"] == "report_artifact_expired"
    assert first_detail["reason_code"] == "report_artifact_expired"
    assert canonical_json_dumps(first_detail) == canonical_json_dumps(second_detail)


def _fresh_app() -> FastAPI:
    app = create_app()
    app.state.reports_repository = ReportsRepository(database_url="")
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    repository.reset()
    return app


def _register_supported_lane(*, app: FastAPI, lane: SupportedLaneFixture) -> None:
    repository = getattr(app.state, "reports_repository", None)
    assert isinstance(repository, ReportsRepository)
    repository.register_finalized_lineage_reference(
        reference=FinalizedLineageReference(
            computation_id=lane.computation_id,
            form_id=lane.form_id,
            historical_version_id=lane.historical_version_id,
            supported_lane_id=lane.supported_lane_id,
            tax_year=lane.tax_year,
            tax_type="income_tax",
            policy_anchor_ids=lane.policy_anchor_ids,
            source_anchor_ids=lane.source_anchor_ids,
        )
    )


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
