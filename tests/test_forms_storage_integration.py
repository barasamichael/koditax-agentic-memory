"""Focused runtime coverage for forms governed storage integration behavior."""

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
from services.forms.app.history_store import get_form_artifact_storage_metadata
from services.forms.app.history_store import list_form_artifact_history_records
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.storage_integration import set_forms_storage_integration_failure_mode
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_storage_integration_persists_metadata_in_history() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        artifact_payload = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-03-28T08:40:00+03:00",
            user_id="storage-user",
        )

    records = list_form_artifact_history_records()
    assert len(records) == 1
    persisted_storage_metadata = get_form_artifact_storage_metadata(
        cast(str, artifact_payload["artifact_id"])
    )
    assert isinstance(persisted_storage_metadata, dict)
    assert persisted_storage_metadata == artifact_payload["storage_metadata"]
    assert persisted_storage_metadata["artifact_hash"] == artifact_payload["artifact_hash"]


def test_forms_storage_integration_retrieval_metadata_includes_storage_reference_fields() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        artifact_payload = _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-28T09:10:00+03:00",
            user_id="storage-user",
        )
        response = client.get(
            (
                "/v1/forms/income-tax/artifacts/"
                f"{artifact_payload['artifact_id']}/versions/{artifact_payload['form_version_id']}/metadata"
            ),
            headers={
                "X-User-ID": "storage-user",
                "X-Correlation-ID": "forms-storage-retrieval-corr",
            },
        )

    payload = _response_json(response)
    metadata = cast(dict[str, object], payload["artifact_metadata"])
    lineage_reference = cast(dict[str, object], metadata["lineage_reference"])
    assert response.status_code == 200
    assert lineage_reference["storage_object_id"]
    assert lineage_reference["storage_backend"] == "forms_governed_storage_inmemory"
    assert lineage_reference["content_type"] == "application/json"
    size_bytes = lineage_reference["size_bytes"]
    assert isinstance(size_bytes, int)
    assert size_bytes >= 0
    assert lineage_reference["artifact_hash"] == artifact_payload["artifact_hash"]


def test_forms_storage_integration_write_failure_is_canonical_and_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    set_forms_storage_integration_failure_mode(enabled=True, reason="simulated_storage_outage")
    app = create_app()
    try:
        with TestClient(app) as client:
            first = _attempt_generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
                finalized_at="2026-03-28T10:15:00+03:00",
                user_id="storage-user",
            )
            second = _attempt_generate_artifact(
                client=client,
                fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
                finalized_at="2026-03-28T10:15:00+03:00",
                user_id="storage-user",
            )
    finally:
        set_forms_storage_integration_failure_mode(enabled=False)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 500
    assert second.status_code == 500
    assert first_error["error_code"] == "forms_storage_write_failed"
    assert first_error["reason"] == "forms_storage_write_failed"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_storage_integration_repeated_success_is_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        first_payload = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-28T12:00:00+03:00",
            user_id="storage-user",
        )
        second_payload = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-28T12:00:00+03:00",
            user_id="storage-user",
        )

    assert canonical_json_dumps(first_payload["storage_metadata"]) == canonical_json_dumps(
        second_payload["storage_metadata"]
    )


def _generate_artifact(
    *,
    client: TestClient,
    fixture_name: str,
    finalized_at: str,
    user_id: str,
) -> dict[str, object]:
    response = _attempt_generate_artifact(
        client=client,
        fixture_name=fixture_name,
        finalized_at=finalized_at,
        user_id=user_id,
    )
    payload = _response_json(response)
    assert response.status_code == 201
    assert isinstance(payload["storage_metadata"], dict)
    return payload


def _attempt_generate_artifact(
    *,
    client: TestClient,
    fixture_name: str,
    finalized_at: str,
    user_id: str,
) -> Any:
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        fixture_name=fixture_name,
        finalized_at=finalized_at,
    )
    return client.post(
        "/v1/forms/income-tax/artifacts",
        json={
            "finalized_output": finalized_output,
            "form_ready_output": form_ready_output,
            "form_version_binding": form_version_binding,
        },
        headers={"X-User-ID": user_id, "X-Correlation-ID": f"{user_id}-{fixture_name}"},
    )


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
