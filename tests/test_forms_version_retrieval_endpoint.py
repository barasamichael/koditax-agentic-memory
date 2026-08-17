"""Endpoint coverage for deterministic specific-version metadata retrieval."""

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
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_version_retrieval_returns_expected_metadata() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-03-27T09:30:00+03:00",
            user_id="metadata-user",
        )
        response = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={
                "X-User-ID": "metadata-user",
                "X-Correlation-ID": "forms-version-retrieval-success-corr",
            },
        )

    payload = _response_json(response)
    metadata = cast(dict[str, object], payload["artifact_metadata"])
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert set(metadata.keys()) == {
        "artifact_id",
        "form_type",
        "form_version_id",
        "tax_year",
        "historical_version_id",
        "status",
        "created_at",
        "lineage_reference",
        "download_metadata",
    }
    assert metadata["artifact_id"] == artifact_id
    assert metadata["form_version_id"] == form_version_id
    assert metadata["form_type"] == "income_tax_return"
    assert isinstance(metadata["lineage_reference"], dict)
    download_metadata = cast(dict[str, object], metadata["download_metadata"])
    assert download_metadata == {"available": True, "expires_at": None}


def test_forms_version_retrieval_unknown_identity_is_deterministic_not_found() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    unknown_artifact_id = "a" * 64
    unknown_form_version_id = "ITX-FORM-20230701-RES-EMP-V1"

    with TestClient(app) as client:
        first = client.get(
            f"/v1/forms/income-tax/artifacts/{unknown_artifact_id}/versions/{unknown_form_version_id}/metadata",
            headers={
                "X-User-ID": "metadata-user",
                "X-Correlation-ID": "forms-version-retrieval-missing-corr",
            },
        )
        second = client.get(
            f"/v1/forms/income-tax/artifacts/{unknown_artifact_id}/versions/{unknown_form_version_id}/metadata",
            headers={
                "X-User-ID": "metadata-user",
                "X-Correlation-ID": "forms-version-retrieval-missing-corr",
            },
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 404
    assert second.status_code == 404
    assert first_error["error_code"] == "forms_history_not_found"
    assert first_error["reason"] == "forms_history_not_found"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_version_retrieval_blocks_cross_user_access() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-27T10:10:00+03:00",
            user_id="owner-user",
        )
        response = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-version-retrieval-unauthorized-corr",
            },
        )

    error = _extract_error_detail(response)
    assert response.status_code == 403
    assert error["error_code"] == "forms_unauthorized_access"
    assert error["reason"] == "forms_unauthorized_access"


def test_forms_version_retrieval_repeated_request_is_deterministic() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-27T11:45:00+03:00",
            user_id="deterministic-user",
        )
        first = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={
                "X-User-ID": "deterministic-user",
                "X-Correlation-ID": "forms-version-retrieval-deterministic-corr",
            },
        )
        second = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/metadata",
            headers={
                "X-User-ID": "deterministic-user",
                "X-Correlation-ID": "forms-version-retrieval-deterministic-corr",
            },
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


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
