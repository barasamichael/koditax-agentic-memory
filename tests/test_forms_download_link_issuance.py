"""Endpoint coverage for deterministic forms download-link issuance semantics."""

from __future__ import annotations

import re
import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path
from datetime import datetime

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$")
GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_download_link_issuance_authorized_success() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-03-29T09:45:00+03:00",
            user_id="download-owner",
        )
        response = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "download-owner",
                "X-Correlation-ID": "forms-download-issuance-success-corr",
            },
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert set(payload.keys()) == {
        "status",
        "artifact_id",
        "download_token",
        "issued_at",
        "expires_at",
        "ttl_seconds",
        "audit_evidence",
        "traceability",
    }
    assert payload["status"] == "issued"
    assert payload["artifact_id"] == artifact_id
    assert TOKEN_PATTERN.fullmatch(cast(str, payload["download_token"])) is not None
    ttl_seconds = payload["ttl_seconds"]
    assert isinstance(ttl_seconds, int)
    assert ttl_seconds > 0

    issued_at = datetime.fromisoformat(cast(str, payload["issued_at"]))
    expires_at = datetime.fromisoformat(cast(str, payload["expires_at"]))
    assert int((expires_at - issued_at).total_seconds()) == ttl_seconds

    audit_evidence = cast(dict[str, object], payload["audit_evidence"])
    assert audit_evidence["event_type"] == "forms_download_link_issued"
    assert isinstance(audit_evidence["audit_event_id"], str)
    assert "storage_object_id" not in json.dumps(payload, sort_keys=True)
    assert "forms_governed_storage_inmemory" not in json.dumps(payload, sort_keys=True)


def test_forms_download_link_issuance_unauthorized_is_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-29T10:15:00+03:00",
            user_id="download-owner",
        )
        first = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-download-issuance-unauth-corr",
            },
        )
        second = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-download-issuance-unauth-corr",
            },
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 403
    assert second.status_code == 403
    assert first_error["error_code"] == "forms_download_not_authorized"
    assert first_error["reason"] == "forms_download_not_authorized"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_download_link_issuance_unknown_artifact_is_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    artifact_id = "a" * 64
    form_version_id = "ITX-FORM-20230701-RES-EMP-V1"

    with TestClient(app) as client:
        first = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "download-owner",
                "X-Correlation-ID": "forms-download-issuance-missing-corr",
            },
        )
        second = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "download-owner",
                "X-Correlation-ID": "forms-download-issuance-missing-corr",
            },
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 404
    assert second.status_code == 404
    assert first_error["error_code"] == "forms_download_artifact_not_found"
    assert first_error["reason"] == "forms_download_artifact_not_found"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_download_link_issuance_ttl_semantics_are_stable() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        artifact_id, form_version_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-29T11:20:00+03:00",
            user_id="download-owner",
        )
        first = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "download-owner",
                "X-Correlation-ID": "forms-download-issuance-determinism-corr",
            },
        )
        second = client.post(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_id}/versions/{form_version_id}/download-links"
            ),
            headers={
                "X-User-ID": "download-owner",
                "X-Correlation-ID": "forms-download-issuance-determinism-corr",
            },
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert set(first_payload.keys()) == set(second_payload.keys())
    assert first_payload["ttl_seconds"] == second_payload["ttl_seconds"]

    first_issued_at = datetime.fromisoformat(cast(str, first_payload["issued_at"]))
    first_expires_at = datetime.fromisoformat(cast(str, first_payload["expires_at"]))
    second_issued_at = datetime.fromisoformat(cast(str, second_payload["issued_at"]))
    second_expires_at = datetime.fromisoformat(cast(str, second_payload["expires_at"]))
    assert int((first_expires_at - first_issued_at).total_seconds()) == cast(
        int, first_payload["ttl_seconds"]
    )
    assert int((second_expires_at - second_issued_at).total_seconds()) == cast(
        int, second_payload["ttl_seconds"]
    )


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
