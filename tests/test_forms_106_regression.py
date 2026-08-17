"""Deterministic regression coverage for Phase 10.6 forms workflows."""

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
from services.forms.app.pre_population import PRE_POPULATION_FIELD_WHITELIST
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_106_integrated_batch_pre_population_and_checklist_are_coherent() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        source_artifact_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-06T09:00:00+03:00",
            user_id="forms-106-user",
        )

        pre_population_request = {"form_type": "income_tax_return", "target_tax_year": 2022}
        pre_population_first = client.post(
            "/v1/forms/income-tax/pre-populations",
            json=pre_population_request,
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-prepop-corr",
            },
        )
        pre_population_second = client.post(
            "/v1/forms/income-tax/pre-populations",
            json=pre_population_request,
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-prepop-corr",
            },
        )

        valid_payload = _build_generation_payload(
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-04-06T10:00:00+03:00",
        )
        invalid_payload = copy.deepcopy(valid_payload)
        invalid_binding = cast(dict[str, object], invalid_payload["form_version_binding"])
        invalid_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
        invalid_lineage["computation_id"] = "mismatch-computation-id"
        invalid_binding["binding_lineage"] = invalid_lineage
        invalid_payload["form_version_binding"] = invalid_binding

        batch_request = {
            "items": [
                {"scope": "income-tax", "payload": valid_payload},
                {"scope": "income-tax", "payload": invalid_payload},
            ]
        }
        batch_first = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_request,
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-batch-corr",
            },
        )
        batch_second = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_request,
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-batch-corr",
            },
        )

        batch_first_payload = _response_json(batch_first)
        batch_first_results = cast(list[dict[str, object]], batch_first_payload["results"])
        succeeded_artifact = cast(dict[str, object], batch_first_results[0]["artifact"])
        artifact_id = cast(str, succeeded_artifact["artifact_id"])
        form_version_id = cast(str, succeeded_artifact["form_version_id"])

        issue_download = client.post(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/download-links",
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-checklist-corr",
            },
        )
        checklist_first = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-checklist-corr",
            },
        )
        checklist_second = client.get(
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/{form_version_id}/submission-checklist",
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-checklist-corr",
            },
        )

    pre_population_first_payload = _response_json(pre_population_first)
    pre_population_second_payload = _response_json(pre_population_second)
    assert pre_population_first.status_code == 200
    assert pre_population_second.status_code == 200
    assert pre_population_first_payload["pre_population_status"] == "applied"
    source_context = cast(dict[str, object], pre_population_first_payload["source_context"])
    assert source_context["source_artifact_id"] == source_artifact_id
    pre_population_fields = cast(
        list[dict[str, object]], pre_population_first_payload["populated_fields"]
    )
    assert [cast(str, field["field"]) for field in pre_population_fields] == list(
        PRE_POPULATION_FIELD_WHITELIST
    )
    assert canonical_json_dumps(pre_population_first_payload) == canonical_json_dumps(
        pre_population_second_payload
    )

    batch_second_payload = _response_json(batch_second)
    assert batch_first.status_code == 200
    assert batch_second.status_code == 200
    batch_first_summary = batch_first_payload["summary"]
    assert isinstance(batch_first_summary, dict)
    assert batch_first_summary == {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
    }
    batch_second_summary = batch_second_payload["summary"]
    assert isinstance(batch_second_summary, dict)
    assert batch_second_summary == {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
    }
    assert _batch_result_signature(batch_first_payload) == _batch_result_signature(
        batch_second_payload
    )
    failed_item = batch_first_results[1]
    failed_error = cast(dict[str, object], failed_item["error"])
    _assert_canonical_error_envelope(failed_error)
    assert failed_error["reason"] == "forms_generation_blocked_by_validation"

    assert issue_download.status_code == 200
    checklist_first_payload = _response_json(checklist_first)
    checklist_second_payload = _response_json(checklist_second)
    checklist_items = cast(list[dict[str, object]], checklist_first_payload["items"])
    assert checklist_first.status_code == 200
    assert checklist_second.status_code == 200
    assert checklist_first_payload["overall_status"] == "ready"
    assert checklist_first_payload["checklist_id"] == checklist_second_payload["checklist_id"]
    assert [cast(str, item["code"]) for item in checklist_items] == [
        "source_artifact_record_resolved",
        "artifact_lineage_complete",
        "storage_reference_available",
        "retention_policy_active",
        "download_window_issued",
        "pre_generation_validation_passed",
        "pre_population_snapshot_available",
    ]
    assert canonical_json_dumps(checklist_first_payload["items"]) == canonical_json_dumps(
        checklist_second_payload["items"]
    )


def test_forms_106_negative_paths_are_deterministic_and_canonical() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()

    with TestClient(app) as client:
        batch_invalid_first = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json={"items": {}},
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-invalid-corr",
            },
        )
        batch_invalid_second = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json={"items": {}},
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-invalid-corr",
            },
        )
        batch_scope = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json={"items": [{"scope": "vat", "payload": {}}]},
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-scope-corr",
            },
        )

        pre_population_missing_first = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={"form_type": "income_tax_return", "target_tax_year": 2029},
            headers={
                "X-User-ID": "forms-106-missing",
                "X-Correlation-ID": "forms-106-prepop-missing",
            },
        )
        pre_population_missing_second = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={"form_type": "income_tax_return", "target_tax_year": 2029},
            headers={
                "X-User-ID": "forms-106-missing",
                "X-Correlation-ID": "forms-106-prepop-missing",
            },
        )

        missing_checklist_path = (
            f"/v1/forms/income-tax/artifacts/{'b' * 64}/versions/KIT-2023.07-R/submission-checklist"
        )
        checklist_missing_first = client.get(
            missing_checklist_path,
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-checklist-missing",
            },
        )
        checklist_missing_second = client.get(
            missing_checklist_path,
            headers={
                "X-User-ID": "forms-106-user",
                "X-Correlation-ID": "forms-106-checklist-missing",
            },
        )

    invalid_first_error = _extract_error_detail(batch_invalid_first)
    invalid_second_error = _extract_error_detail(batch_invalid_second)
    assert batch_invalid_first.status_code == 400
    assert batch_invalid_second.status_code == 400
    assert invalid_first_error["error_code"] == "forms_request_invalid"
    assert invalid_first_error["reason"] == "forms_request_invalid"
    assert invalid_first_error["error_code"] == invalid_second_error["error_code"]
    assert invalid_first_error["reason"] == invalid_second_error["reason"]

    scope_payload = _response_json(batch_scope)
    scope_results = cast(list[dict[str, object]], scope_payload["results"])
    scope_error = cast(dict[str, object], scope_results[0]["error"])
    _assert_canonical_error_envelope(scope_error)
    assert batch_scope.status_code == 200
    assert scope_error["error_code"] == "forms_scope_not_supported"
    assert scope_error["reason"] == "forms_scope_not_supported"

    pre_population_missing_first_payload = _response_json(pre_population_missing_first)
    pre_population_missing_second_payload = _response_json(pre_population_missing_second)
    assert pre_population_missing_first.status_code == 200
    assert pre_population_missing_second.status_code == 200
    assert pre_population_missing_first_payload["reason"] == "forms_pre_population_source_not_found"
    assert pre_population_missing_first_payload["populated_fields"] == []
    assert canonical_json_dumps(pre_population_missing_first_payload) == canonical_json_dumps(
        pre_population_missing_second_payload
    )

    checklist_missing_first_error = _extract_error_detail(checklist_missing_first)
    checklist_missing_second_error = _extract_error_detail(checklist_missing_second)
    assert checklist_missing_first.status_code == 404
    assert checklist_missing_second.status_code == 404
    assert (
        checklist_missing_first_error["error_code"] == "forms_submission_checklist_source_missing"
    )
    assert checklist_missing_first_error["reason"] == "forms_submission_checklist_source_missing"
    assert (
        checklist_missing_first_error["error_code"] == checklist_missing_second_error["error_code"]
    )
    assert checklist_missing_first_error["reason"] == checklist_missing_second_error["reason"]


def _build_generation_payload(*, fixture_name: str, finalized_at: str) -> dict[str, object]:
    finalized_output = _build_finalized_output(
        fixture_name=fixture_name,
        finalized_at=finalized_at,
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return {
        "finalized_output": finalized_output,
        "form_ready_output": form_ready_output,
        "form_version_binding": form_version_binding,
    }


def _generate_artifact(
    *,
    client: TestClient,
    fixture_name: str,
    finalized_at: str,
    user_id: str,
) -> str:
    payload = _build_generation_payload(
        fixture_name=fixture_name,
        finalized_at=finalized_at,
    )
    response = client.post(
        "/v1/forms/income-tax/artifacts",
        json=payload,
        headers={"X-User-ID": user_id, "X-Correlation-ID": f"{user_id}-{fixture_name}"},
    )
    response_payload = _response_json(response)
    assert response.status_code == 201
    return cast(str, response_payload["artifact_id"])


def _build_finalized_output(*, fixture_name: str, finalized_at: str) -> dict[str, object]:
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


def _batch_result_signature(payload: dict[str, Any]) -> list[tuple[int, str, str]]:
    results = cast(list[dict[str, object]], payload["results"])
    signature: list[tuple[int, str, str]] = []
    for item in results:
        index = cast(int, item["index"])
        status = cast(str, item["status"])
        if status == "failed":
            error = cast(dict[str, object], item["error"])
            signature.append((index, status, cast(str, error["reason"])))
            continue
        artifact = cast(dict[str, object], item["artifact"])
        signature.append((index, status, cast(str, artifact["artifact_id"])))
    return signature


def _extract_error_detail(response: Any) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_map = cast(dict[str, object], detail)
    _assert_canonical_error_envelope(detail_map)
    return detail_map


def _assert_canonical_error_envelope(error: dict[str, object]) -> None:
    assert isinstance(error.get("error_code"), str) and cast(str, error["error_code"])
    assert isinstance(error.get("message"), str) and cast(str, error["message"])
    assert isinstance(error.get("reason"), str) and cast(str, error["reason"])


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
