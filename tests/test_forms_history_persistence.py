"""History persistence coverage for generated forms artifact metadata records."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.history_store import list_form_artifact_history_records
from services.forms.app.history_store import set_form_artifact_history_store_failure_mode
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-28T10:45:00+03:00"


def test_forms_history_persistence_records_metadata_on_generation_success() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": form_version_binding,
            },
            headers={"X-Correlation-ID": "forms-history-success-corr"},
        )

    payload = _response_json(response)
    records = list_form_artifact_history_records()
    assert response.status_code == 201
    assert len(records) == 1
    record = records[0]
    assert set(record.keys()) == {
        "user_id",
        "artifact_id",
        "form_type",
        "form_version_id",
        "tax_year",
        "historical_version_id",
        "lineage_reference",
        "artifact_hash",
        "created_at",
        "status",
        "pre_population_source_fields",
    }
    assert record["user_id"] == "anonymous_user"
    assert record["artifact_id"] == payload["artifact_id"]
    assert record["form_type"] == payload["form_type"]
    assert record["form_version_id"] == payload["form_version_id"]
    assert record["tax_year"] == payload["tax_year"]
    assert record["historical_version_id"] == payload["historical_version_id"]
    assert record["artifact_hash"] == payload["artifact_hash"]
    assert record["created_at"] == payload["created_at"]
    assert record["status"] == "current"
    assert isinstance(record["lineage_reference"], dict)
    assert isinstance(record["pre_population_source_fields"], dict)


def test_forms_history_persistence_repeated_distinct_generations_create_distinct_records() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    first_inputs = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    second_inputs = _build_generation_inputs(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )

    with TestClient(app) as client:
        first_response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": first_inputs[0],
                "form_ready_output": first_inputs[1],
                "form_version_binding": first_inputs[2],
            },
            headers={"X-Correlation-ID": "forms-history-distinct-1-corr"},
        )
        second_response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": second_inputs[0],
                "form_ready_output": second_inputs[1],
                "form_version_binding": second_inputs[2],
            },
            headers={"X-Correlation-ID": "forms-history-distinct-2-corr"},
        )

    first_payload = _response_json(first_response)
    second_payload = _response_json(second_response)
    records = list_form_artifact_history_records()
    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert len(records) == 2
    artifact_ids = {record["artifact_id"] for record in records}
    assert first_payload["artifact_id"] in artifact_ids
    assert second_payload["artifact_id"] in artifact_ids
    assert len(artifact_ids) == 2


def test_forms_history_persistence_failure_returns_canonical_error() -> None:
    reset_form_artifact_history_store()
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )

    set_form_artifact_history_store_failure_mode(enabled=True, reason="simulated_outage")
    try:
        with TestClient(app) as client:
            first = client.post(
                "/v1/forms/income-tax/artifacts",
                json={
                    "finalized_output": finalized_output,
                    "form_ready_output": form_ready_output,
                    "form_version_binding": form_version_binding,
                },
                headers={"X-Correlation-ID": "forms-history-failure-corr"},
            )
            second = client.post(
                "/v1/forms/income-tax/artifacts",
                json={
                    "finalized_output": finalized_output,
                    "form_ready_output": form_ready_output,
                    "form_version_binding": form_version_binding,
                },
                headers={"X-Correlation-ID": "forms-history-failure-corr"},
            )
    finally:
        set_form_artifact_history_store_failure_mode(enabled=False)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 500
    assert second.status_code == 500
    assert first_error["error_code"] == "forms_history_persistence_failed"
    assert first_error["reason"] == "forms_history_persistence_failed"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]
    assert list_form_artifact_history_records() == []


def test_forms_history_persistence_same_input_keeps_stable_metadata_shape() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    request_payload = {
        "finalized_output": finalized_output,
        "form_ready_output": form_ready_output,
        "form_version_binding": form_version_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-history-stable-1-corr"},
        )
        first_records = list_form_artifact_history_records()
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-history-stable-2-corr"},
        )
        second_records = list_form_artifact_history_records()

    assert first.status_code == 201
    assert second.status_code == 201
    assert len(first_records) == 1
    assert len(second_records) == 1
    assert canonical_json_dumps(first_records[0]) == canonical_json_dumps(second_records[0])
    lineage_reference = first_records[0]["lineage_reference"]
    assert isinstance(lineage_reference, dict)
    assert set(lineage_reference.keys()) == {
        "computation_id",
        "input_hash",
        "supported_lane_id",
        "historical_version_id",
        "form_version_id",
        "finalized_audit_event_id",
    }


def _build_generation_inputs(
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return finalized_output, form_ready_output, form_version_binding


def _build_finalized_output(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = fixture["fixture_id"]
    expected_output = copy.deepcopy(fixture["expected_output"])
    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": FINALIZED_AT,
        "finalized_audit_event_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:finalized-audit")),
        "tax_type": expected_output["tax_type"],
        "regime_type": expected_output["regime_type"],
        "tax_year": expected_output["tax_year"],
        "rule_version": expected_output["rule_version"],
        "input_hash": expected_output["input_hash"],
        "result_payload": expected_output["result_payload"],
    }


def _extract_error_detail(response: Any) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    return cast(dict[str, object], detail)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
