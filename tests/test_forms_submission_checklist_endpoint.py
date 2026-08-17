"""Endpoint coverage for deterministic forms submission-checklist generation."""

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
from services.forms.app.history_store import set_form_artifact_history_store_failure_mode
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_submission_checklist_endpoint_returns_ready_for_fully_prepared_artifact() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-04-05T09:00:00+03:00",
            user_id="checklist-user",
        )
        issue_download_response = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={
                "X-User-ID": "checklist-user",
                "X-Correlation-ID": "forms-checklist-ready-corr",
            },
        )
        assert issue_download_response.status_code == 200
        response = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={
                "X-User-ID": "checklist-user",
                "X-Correlation-ID": "forms-checklist-ready-corr",
            },
        )

    payload = _response_json(response)
    items = cast(list[dict[str, object]], payload["items"])
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["overall_status"] == "ready"
    assert isinstance(payload["checklist_id"], str)
    assert len(payload["checklist_id"]) == 64
    expected_codes = [
        "source_artifact_record_resolved",
        "artifact_lineage_complete",
        "storage_reference_available",
        "retention_policy_active",
        "download_window_issued",
        "pre_generation_validation_passed",
        "pre_population_snapshot_available",
    ]
    assert [cast(str, item["code"]) for item in items] == expected_codes
    assert all(item["status"] in {"pass", "warn"} for item in items)
    assert all(
        cast(str, item["code"]) != "download_window_issued" or item["status"] == "pass"
        for item in items
    )


def test_forms_submission_checklist_returns_not_ready_when_blocking_prerequisite_missing() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-05T10:00:00+03:00",
            user_id="checklist-not-ready-user",
        )
        response = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={
                "X-User-ID": "checklist-not-ready-user",
                "X-Correlation-ID": "forms-checklist-not-ready-corr",
            },
        )

    payload = _response_json(response)
    items = cast(list[dict[str, object]], payload["items"])
    assert response.status_code == 200
    assert payload["overall_status"] == "not_ready"
    download_item = next(item for item in items if item["code"] == "download_window_issued")
    assert download_item["status"] == "fail"
    assert download_item["blocking"] is True


def test_forms_submission_checklist_endpoint_blocks_cross_user_access() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-05T11:00:00+03:00",
            user_id="owner-user",
        )
        response = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={"X-User-ID": "other-user", "X-Correlation-ID": "forms-checklist-unauth-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 403
    assert error["error_code"] == "forms_submission_checklist_not_authorized"
    assert error["reason"] == "forms_submission_checklist_not_authorized"


def test_forms_submission_checklist_source_missing_returns_canonical_error_deterministically() -> (
    None
):
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    reset_forms_storage_integration_state()
    app = create_app()
    missing_artifact_id = "a" * 64
    missing_form_version_id = "KIT-2023.07-R"
    checklist_path = (
        f"/v1/forms/income-tax/artifacts/{missing_artifact_id}/"
        f"versions/{missing_form_version_id}/submission-checklist"
    )

    with TestClient(app) as client:
        first = client.get(
            checklist_path,
            headers={
                "X-User-ID": "missing-user",
                "X-Correlation-ID": "forms-checklist-missing-corr",
            },
        )
        second = client.get(
            checklist_path,
            headers={
                "X-User-ID": "missing-user",
                "X-Correlation-ID": "forms-checklist-missing-corr",
            },
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 404
    assert second.status_code == 404
    assert first_error["error_code"] == "forms_submission_checklist_source_missing"
    assert first_error["reason"] == "forms_submission_checklist_source_missing"
    assert second_error["error_code"] == "forms_submission_checklist_source_missing"
    assert second_error["reason"] == "forms_submission_checklist_source_missing"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_submission_checklist_endpoint_is_deterministic_for_same_request() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-04-05T12:00:00+03:00",
            user_id="det-user",
        )
        client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-checklist-det-corr"},
        )
        first = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-checklist-det-corr"},
        )
        second = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-checklist-det-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["checklist_id"] == second_payload["checklist_id"]
    assert first_payload["overall_status"] == second_payload["overall_status"]
    first_items = cast(list[dict[str, object]], first_payload["items"])
    second_items = cast(list[dict[str, object]], second_payload["items"])
    assert canonical_json_dumps(first_items) == canonical_json_dumps(second_items)


def _generate_artifact(
    *,
    client: TestClient,
    fixture_name: str,
    finalized_at: str,
    user_id: str,
) -> tuple[str, str]:
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        fixture_name=fixture_name,
        finalized_at=finalized_at,
    )
    response = client.post(
        "/v1/forms/income-tax/artifacts",
        json={
            "finalized_output": finalized_output,
            "form_ready_output": form_ready_output,
            "form_version_binding": form_version_binding,
        },
        headers={"X-User-ID": user_id, "X-Correlation-ID": f"{user_id}-{fixture_name}"},
    )
    payload = _response_json(response)
    assert response.status_code == 201
    artifact_id = cast(str, payload["artifact_id"])
    form_version_id = cast(str, payload["form_version_id"])
    return artifact_id, form_version_id


def _build_generation_inputs(
    *,
    fixture_name: str,
    finalized_at: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(
        fixture_name=fixture_name,
        finalized_at=finalized_at,
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return finalized_output, form_ready_output, form_version_binding


def _build_finalized_output(
    *,
    fixture_name: str,
    finalized_at: str,
) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = fixture["fixture_id"]
    expected_output = copy.deepcopy(fixture["expected_output"])
    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": finalized_at,
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
