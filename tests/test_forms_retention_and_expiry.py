"""Deterministic retention and expiry enforcement coverage for forms artifacts."""

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
from services.forms.app.history_store import get_form_artifact_retention_metadata
from services.forms.app.retention_policy import set_forms_retention_policy_now_override
from services.forms.app.retention_policy import reset_forms_retention_policy_now_override
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_active_retention_allows_metadata_retrieval_and_download_issuance() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    set_forms_retention_policy_now_override(datetime(2026, 3, 30, 9, 0, tzinfo=UTC))
    app = create_app()
    try:
        with TestClient(app) as client:
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
                finalized_at="2026-03-29T10:30:00+03:00",
                user_id="retention-owner",
            )
            metadata_response = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "retention-owner",
                    "X-Correlation-ID": "retention-active-meta",
                },
            )
            download_response = client.post(
                (
                    "/v1/forms/income-tax/artifacts/"
                    f"{artifact_id}/versions/{form_version_id}/download-links"
                ),
                headers={"X-User-ID": "retention-owner", "X-Correlation-ID": "retention-active-dl"},
            )
    finally:
        reset_forms_retention_policy_now_override()

    metadata_payload = _response_json(metadata_response)
    download_payload = _response_json(download_response)
    assert metadata_response.status_code == 200
    assert download_response.status_code == 200
    assert cast(dict[str, object], metadata_payload["artifact_metadata"])["download_metadata"] == {
        "available": True,
        "expires_at": None,
    }
    assert isinstance(download_payload["download_token"], str)
    persisted_retention_metadata = get_form_artifact_retention_metadata(artifact_id)
    assert isinstance(persisted_retention_metadata, dict)
    assert persisted_retention_metadata["retention_status"] == "active"
    assert persisted_retention_metadata["download_expires_at"] == download_payload["expires_at"]


def test_expired_download_link_is_denied_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    try:
        with TestClient(app) as client:
            set_forms_retention_policy_now_override(datetime(2026, 3, 30, 9, 0, tzinfo=UTC))
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
                finalized_at="2026-03-29T11:00:00+03:00",
                user_id="retention-owner",
            )
            first = client.post(
                (
                    "/v1/forms/income-tax/artifacts/"
                    f"{artifact_id}/versions/{form_version_id}/download-links"
                ),
                headers={
                    "X-User-ID": "retention-owner",
                    "X-Correlation-ID": "retention-expired-dl",
                },
            )
            assert first.status_code == 200

            set_forms_retention_policy_now_override(datetime(2026, 3, 30, 11, 0, tzinfo=UTC))
            second = client.post(
                (
                    "/v1/forms/income-tax/artifacts/"
                    f"{artifact_id}/versions/{form_version_id}/download-links"
                ),
                headers={
                    "X-User-ID": "retention-owner",
                    "X-Correlation-ID": "retention-expired-dl",
                },
            )
            third = client.post(
                (
                    "/v1/forms/income-tax/artifacts/"
                    f"{artifact_id}/versions/{form_version_id}/download-links"
                ),
                headers={
                    "X-User-ID": "retention-owner",
                    "X-Correlation-ID": "retention-expired-dl",
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
    assert isinstance(second_error["trace_id"], str)
    assert second_error["correlation_id"] == "retention-expired-dl"


def test_retention_expired_artifact_metadata_access_is_denied_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    try:
        with TestClient(app) as client:
            set_forms_retention_policy_now_override(datetime(2026, 3, 30, 9, 0, tzinfo=UTC))
            artifact_id, form_version_id = _generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
                finalized_at="2026-03-29T12:10:00+03:00",
                user_id="retention-owner",
            )
            set_forms_retention_policy_now_override(datetime(2028, 3, 30, 9, 0, tzinfo=UTC))
            first = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "retention-owner",
                    "X-Correlation-ID": "retention-expired-artifact",
                },
            )
            second = client.get(
                f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
                headers={
                    "X-User-ID": "retention-owner",
                    "X-Correlation-ID": "retention-expired-artifact",
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
    assert isinstance(first_error["trace_id"], str)
    assert first_error["correlation_id"] == "retention-expired-artifact"


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
    assert "trace_id" in detail
    assert "correlation_id" in detail
    return cast(dict[str, object], detail)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
