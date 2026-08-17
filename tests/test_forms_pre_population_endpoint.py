"""Endpoint coverage for deterministic prior-year forms pre-population behavior."""

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
from services.forms.app.pre_population import PRE_POPULATION_POLICY_TAG
from services.forms.app.pre_population import PRE_POPULATION_FIELD_WHITELIST
from services.forms.app.storage_integration import reset_forms_storage_integration_state
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")


def test_forms_pre_population_endpoint_returns_whitelisted_prior_year_fields() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        source_artifact_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-01T09:00:00+03:00",
            user_id="prepop-user",
        )
        response = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={"form_type": "income_tax_return", "target_tax_year": 2022},
            headers={"X-User-ID": "prepop-user", "X-Correlation-ID": "forms-prepop-happy-corr"},
        )

    payload = _response_json(response)
    populated_fields = cast(list[dict[str, object]], payload["populated_fields"])
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["pre_population_status"] == "applied"
    source_context = cast(dict[str, object], payload["source_context"])
    assert source_context["selection_mode"] == "auto_previous_year"
    assert source_context["source_tax_year"] == 2021
    assert source_context["source_artifact_id"] == source_artifact_id
    expected_fields = list(PRE_POPULATION_FIELD_WHITELIST)
    actual_fields = [cast(str, item["field"]) for item in populated_fields]
    assert actual_fields == expected_fields
    for item in populated_fields:
        assert item["source_artifact_id"] == source_artifact_id
        assert item["source_tax_year"] == 2021
        assert item["policy_tag"] == PRE_POPULATION_POLICY_TAG
        assert "value" in item


def test_forms_pre_population_endpoint_includes_provenance_for_explicit_source_year() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        source_artifact_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-02T09:00:00+03:00",
            user_id="prepop-user-explicit",
        )
        response = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={
                "form_type": "income_tax_return",
                "target_tax_year": 2022,
                "source_tax_year": 2021,
            },
            headers={
                "X-User-ID": "prepop-user-explicit",
                "X-Correlation-ID": "forms-prepop-explicit-corr",
            },
        )

    payload = _response_json(response)
    populated_fields = cast(list[dict[str, object]], payload["populated_fields"])
    assert response.status_code == 200
    assert payload["pre_population_status"] == "applied"
    source_context = cast(dict[str, object], payload["source_context"])
    assert source_context["selection_mode"] == "explicit_source_tax_year"
    assert source_context["source_artifact_id"] == source_artifact_id
    assert all(field["source_artifact_id"] == source_artifact_id for field in populated_fields)


def test_forms_pre_population_endpoint_source_not_found_is_graceful_and_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={"form_type": "income_tax_return", "target_tax_year": 2025},
            headers={"X-User-ID": "missing-user", "X-Correlation-ID": "forms-prepop-missing-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={"form_type": "income_tax_return", "target_tax_year": 2025},
            headers={"X-User-ID": "missing-user", "X-Correlation-ID": "forms-prepop-missing-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["pre_population_status"] == "source_not_found"
    assert first_payload["reason"] == "forms_pre_population_source_not_found"
    assert first_payload["populated_fields"] == []
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_forms_pre_population_endpoint_blocks_cross_user_source_access() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-03T09:00:00+03:00",
            user_id="owner-user",
        )
        first = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={
                "form_type": "income_tax_return",
                "target_tax_year": 2022,
                "source_user_id": "owner-user",
            },
            headers={"X-User-ID": "other-user", "X-Correlation-ID": "forms-prepop-unauth-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/pre-populations",
            json={
                "form_type": "income_tax_return",
                "target_tax_year": 2022,
                "source_user_id": "owner-user",
            },
            headers={"X-User-ID": "other-user", "X-Correlation-ID": "forms-prepop-unauth-corr"},
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 403
    assert second.status_code == 403
    assert first_error["error_code"] == "forms_pre_population_not_authorized"
    assert first_error["reason"] == "forms_pre_population_not_authorized"
    assert second_error["error_code"] == "forms_pre_population_not_authorized"
    assert second_error["reason"] == "forms_pre_population_not_authorized"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_pre_population_endpoint_repeated_request_is_deterministic() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    with TestClient(app) as client:
        _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-04-04T09:00:00+03:00",
            user_id="det-user",
        )
        request_payload = {"form_type": "income_tax_return", "target_tax_year": 2022}
        first = client.post(
            "/v1/forms/income-tax/pre-populations",
            json=request_payload,
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-prepop-det-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/pre-populations",
            json=request_payload,
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-prepop-det-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_forms_pre_population_invalid_payload_returns_canonical_error_deterministically() -> None:
    reset_form_artifact_history_store()
    reset_forms_storage_integration_state()
    app = create_app()
    request_payload = {"form_type": "income_tax_return", "target_tax_year": "2022"}

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/pre-populations",
            json=request_payload,
            headers={"X-User-ID": "invalid-user", "X-Correlation-ID": "forms-prepop-invalid-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/pre-populations",
            json=request_payload,
            headers={"X-User-ID": "invalid-user", "X-Correlation-ID": "forms-prepop-invalid-corr"},
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


def _generate_artifact(
    *,
    client: TestClient,
    fixture_name: str,
    finalized_at: str,
    user_id: str,
) -> str:
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
    return cast(str, payload["artifact_id"])


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
