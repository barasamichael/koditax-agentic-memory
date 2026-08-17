"""Integration coverage for forms download lifecycle success and denial semantics."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path
from datetime import UTC
from datetime import datetime

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.retention_policy import set_forms_retention_policy_now_override
from services.forms.app.retention_policy import reset_forms_retention_policy_now_override
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_download_lifecycle_success_path_is_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    set_forms_retention_policy_now_override(datetime(2026, 3, 31, 8, 15, tzinfo=UTC))
    app = create_app()
    try:
        with TestClient(app) as client:
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
                finalized_at="2026-03-30T09:30:00+03:00",
                user_id="lifecycle-owner",
            )
            metadata_before = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-success",
                },
            )
            issuance = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-success",
                },
            )
            metadata_after = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-success",
                },
            )
    finally:
        reset_forms_retention_policy_now_override()

    metadata_before_payload = _response_json(metadata_before)
    issuance_payload = _response_json(issuance)
    metadata_after_payload = _response_json(metadata_after)

    assert metadata_before.status_code == 200
    assert issuance.status_code == 200
    assert metadata_after.status_code == 200
    assert metadata_before_payload["status"] == "ok"
    assert issuance_payload["status"] == "issued"
    assert metadata_after_payload["status"] == "ok"

    before_download_metadata = cast(
        dict[str, object],
        cast(dict[str, object], metadata_before_payload["artifact_metadata"])["download_metadata"],
    )
    after_download_metadata = cast(
        dict[str, object],
        cast(dict[str, object], metadata_after_payload["artifact_metadata"])["download_metadata"],
    )
    assert before_download_metadata == {"available": True, "expires_at": None}
    assert after_download_metadata["available"] is True
    assert after_download_metadata["expires_at"] == issuance_payload["expires_at"]


def test_forms_download_lifecycle_expired_link_is_rejected_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    try:
        with TestClient(app) as client:
            set_forms_retention_policy_now_override(datetime(2026, 3, 31, 8, 0, tzinfo=UTC))
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
                finalized_at="2026-03-30T09:30:00+03:00",
                user_id="lifecycle-owner",
            )
            first = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-expired",
                },
            )
            assert first.status_code == 200

            set_forms_retention_policy_now_override(datetime(2026, 3, 31, 12, 0, tzinfo=UTC))
            second = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-expired",
                },
            )
            third = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-expired",
                },
            )
    finally:
        reset_forms_retention_policy_now_override()

    second_error = _extract_error_detail(second)
    third_error = _extract_error_detail(third)
    assert second.status_code == 403
    assert third.status_code == 403
    assert second_error["error_code"] == "forms_download_link_expired"
    assert second_error["reason"] == "forms_download_link_expired"
    assert second_error["error_code"] == third_error["error_code"]
    assert second_error["reason"] == third_error["reason"]


def test_forms_download_lifecycle_unauthorized_access_is_rejected_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    set_forms_retention_policy_now_override(datetime(2026, 3, 31, 8, 30, tzinfo=UTC))
    app = create_app()
    try:
        with TestClient(app) as client:
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
                finalized_at="2026-03-30T09:30:00+03:00",
                user_id="lifecycle-owner",
            )
            first = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "unauthorized-user",
                    "X-Correlation-ID": "forms-lifecycle-unauthorized",
                },
            )
            second = client.post(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
                headers={
                    "X-User-ID": "unauthorized-user",
                    "X-Correlation-ID": "forms-lifecycle-unauthorized",
                },
            )
    finally:
        reset_forms_retention_policy_now_override()

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 403
    assert second.status_code == 403
    assert first_error["error_code"] == "forms_download_not_authorized"
    assert first_error["reason"] == "forms_download_not_authorized"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_download_lifecycle_retention_expired_is_rejected_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    try:
        with TestClient(app) as client:
            set_forms_retention_policy_now_override(datetime(2026, 3, 31, 9, 0, tzinfo=UTC))
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
                finalized_at="2026-03-30T09:30:00+03:00",
                user_id="lifecycle-owner",
            )
            set_forms_retention_policy_now_override(datetime(2028, 3, 31, 9, 0, tzinfo=UTC))
            first = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-retention-expired",
                },
            )
            second = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "lifecycle-owner",
                    "X-Correlation-ID": "forms-lifecycle-retention-expired",
                },
            )
    finally:
        reset_forms_retention_policy_now_override()

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 403
    assert second.status_code == 403
    assert first_error["error_code"] == "forms_artifact_retention_expired"
    assert first_error["reason"] == "forms_artifact_retention_expired"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


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
    return cast(str, payload["artifact_id"]), cast(str, payload["form_version_id"])


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
