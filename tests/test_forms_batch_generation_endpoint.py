"""Endpoint coverage for deterministic forms batch-generation behavior."""

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
from services.forms.app.history_store import reset_form_artifact_history_store
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_batch_generation_all_valid_items_succeed() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    batch_payload = {
        "items": [
            {
                "scope": "income-tax",
                "payload": _build_generation_payload(
                    fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
                    finalized_at="2026-04-01T09:00:00+03:00",
                ),
            },
            {
                "scope": "income-tax",
                "payload": _build_generation_payload(
                    fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
                    finalized_at="2026-04-01T09:05:00+03:00",
                ),
            },
        ]
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-all-valid"},
        )

    payload = _response_json(response)
    results = cast(list[dict[str, object]], payload["results"])
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert isinstance(payload["batch_id"], str) and len(payload["batch_id"]) == 64
    assert cast(dict[str, int], payload["summary"]) == {"total": 2, "succeeded": 2, "failed": 0}
    assert [cast(int, item["index"]) for item in results] == [0, 1]
    assert [cast(str, item["status"]) for item in results] == ["succeeded", "succeeded"]
    for result in results:
        artifact = cast(dict[str, object], result["artifact"])
        assert artifact["status"] == "ok"
        assert artifact["generation_status"] == "generated"


def test_forms_batch_generation_mixed_results_are_explicit_and_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    valid_payload = _build_generation_payload(
        fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-01T10:00:00+03:00",
    )
    invalid_payload = copy.deepcopy(valid_payload)
    invalid_binding = cast(dict[str, object], invalid_payload["form_version_binding"])
    invalid_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    invalid_lineage["computation_id"] = "mismatch-computation-id"
    invalid_binding["binding_lineage"] = invalid_lineage
    invalid_payload["form_version_binding"] = invalid_binding

    batch_payload = {
        "items": [
            {"scope": "income-tax", "payload": valid_payload},
            {"scope": "income-tax", "payload": invalid_payload},
            {
                "scope": "income-tax",
                "payload": _build_generation_payload(
                    fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
                    finalized_at="2026-04-01T10:05:00+03:00",
                ),
            },
        ]
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-mixed"},
        )

    payload = _response_json(response)
    results = cast(list[dict[str, object]], payload["results"])
    assert response.status_code == 200
    assert cast(dict[str, int], payload["summary"]) == {"total": 3, "succeeded": 2, "failed": 1}
    assert [cast(str, item["status"]) for item in results] == ["succeeded", "failed", "succeeded"]

    failed = results[1]
    error = cast(dict[str, object], failed["error"])
    _assert_canonical_item_error(error)
    assert error["error_code"] == "forms_generation_blocked_by_validation"
    assert error["reason"] == "forms_generation_blocked_by_validation"


def test_forms_batch_generation_unsupported_scope_item_fails_without_breaking_valid_items() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    valid_payload = _build_generation_payload(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-01T11:10:00+03:00",
    )
    batch_payload = {
        "items": [
            {"scope": "vat", "payload": valid_payload},
            {"scope": "income-tax", "payload": valid_payload},
        ]
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-unsupported"},
        )

    payload = _response_json(response)
    results = cast(list[dict[str, object]], payload["results"])
    assert response.status_code == 200
    assert cast(dict[str, int], payload["summary"]) == {"total": 2, "succeeded": 1, "failed": 1}
    first_error = cast(dict[str, object], results[0]["error"])
    _assert_canonical_item_error(first_error)
    assert first_error["error_code"] == "forms_scope_not_supported"
    assert first_error["reason"] == "forms_scope_not_supported"
    assert results[1]["status"] == "succeeded"


def test_forms_batch_generation_repeated_same_input_is_order_and_reason_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    valid_payload = _build_generation_payload(
        fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-01T12:15:00+03:00",
    )
    invalid_payload = copy.deepcopy(valid_payload)
    invalid_payload["form_ready_output"] = "invalid"
    batch_payload = {
        "items": [
            {"scope": "income-tax", "payload": valid_payload},
            {"scope": "income-tax", "payload": invalid_payload},
            {"scope": "vat", "payload": valid_payload},
        ]
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-determinism"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=batch_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-determinism"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert cast(dict[str, int], first_payload["summary"]) == cast(
        dict[str, int], second_payload["summary"]
    )
    assert _result_signature(first_payload) == _result_signature(second_payload)


def test_forms_batch_generation_invalid_payload_returns_canonical_error_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    request_payload: dict[str, object] = {"items": {}}

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=request_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-invalid"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json=request_payload,
            headers={"X-User-ID": "batch-owner", "X-Correlation-ID": "forms-batch-invalid"},
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert first_error["error_code"] == "forms_request_invalid"
    assert first_error["reason"] == "forms_request_invalid"
    assert second_error["error_code"] == "forms_request_invalid"
    assert second_error["reason"] == "forms_request_invalid"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


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


def _assert_canonical_item_error(error: dict[str, object]) -> None:
    assert isinstance(error.get("error_code"), str) and cast(str, error["error_code"])
    assert isinstance(error.get("message"), str) and cast(str, error["message"])
    assert isinstance(error.get("reason"), str) and cast(str, error["reason"])


def _result_signature(payload: dict[str, Any]) -> list[tuple[int, str, str]]:
    results = cast(list[dict[str, object]], payload["results"])
    signature: list[tuple[int, str, str]] = []
    for item in results:
        status = cast(str, item["status"])
        index = cast(int, item["index"])
        if status == "failed":
            error = cast(dict[str, object], item["error"])
            signature.append((index, status, cast(str, error["reason"])))
            continue
        artifact = cast(dict[str, object], item["artifact"])
        signature.append((index, status, cast(str, artifact["artifact_id"])))
    return signature


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _extract_error_detail(response: Any) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    return cast(dict[str, object], detail)
