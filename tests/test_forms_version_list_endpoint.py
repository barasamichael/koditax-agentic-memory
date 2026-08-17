"""Endpoint coverage for deterministic forms version-list history retrieval."""

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


def test_forms_version_list_endpoint_returns_exact_filtered_records() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-20T09:00:00+03:00",
            user_id="user-a",
        )
        _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-22T09:00:00+03:00",
            user_id="user-a",
        )
        _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-03-23T09:00:00+03:00",
            user_id="user-b",
        )
        response = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "user-a",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={"X-User-ID": "user-a", "X-Correlation-ID": "forms-version-list-filter-corr"},
        )

    payload = _response_json(response)
    versions = cast(list[dict[str, object]], payload["versions"])
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["user_id"] == "user-a"
    assert payload["tax_year"] == 2021
    assert payload["form_type"] == "income_tax_return"
    assert len(versions) == 2
    for item in versions:
        assert set(item.keys()) == {
            "artifact_id",
            "form_type",
            "form_version_id",
            "tax_year",
            "historical_version_id",
            "status",
            "created_at",
            "lineage_reference",
        }
        assert item["tax_year"] == 2021
        assert item["form_type"] == "income_tax_return"


def test_forms_version_list_endpoint_orders_newest_first_with_tiebreaker() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        first_artifact_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-21T09:00:00+03:00",
            user_id="user-order",
        )
        second_artifact_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-21T09:00:00+03:00",
            user_id="user-order",
        )
        newest_artifact_id = _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
            finalized_at="2026-03-25T09:00:00+03:00",
            user_id="user-order",
        )
        response = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "user-order",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={
                "X-User-ID": "user-order",
                "X-Correlation-ID": "forms-version-list-order-corr",
            },
        )

    payload = _response_json(response)
    versions = cast(list[dict[str, object]], payload["versions"])
    assert response.status_code == 200
    ordered_artifact_ids = [cast(str, item["artifact_id"]) for item in versions]
    assert ordered_artifact_ids == sorted(
        [first_artifact_id, second_artifact_id],
        reverse=True,
    )
    assert newest_artifact_id not in ordered_artifact_ids


def test_forms_version_list_endpoint_not_found_is_deterministic() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()

    with TestClient(app) as client:
        first = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "user-missing",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={
                "X-User-ID": "user-missing",
                "X-Correlation-ID": "forms-version-list-missing-corr",
            },
        )
        second = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "user-missing",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={
                "X-User-ID": "user-missing",
                "X-Correlation-ID": "forms-version-list-missing-corr",
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


def test_forms_version_list_endpoint_blocks_cross_user_access() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-20T09:00:00+03:00",
            user_id="owner-user",
        )
        response = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "owner-user",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={
                "X-User-ID": "other-user",
                "X-Correlation-ID": "forms-version-list-unauth-corr",
            },
        )

    error = _extract_error_detail(response)
    assert response.status_code == 403
    assert error["error_code"] == "forms_unauthorized_access"
    assert error["reason"] == "forms_unauthorized_access"


def test_forms_version_list_endpoint_repeated_query_is_deterministic() -> None:
    reset_form_artifact_history_store()
    set_form_artifact_history_store_failure_mode(enabled=False)
    app = create_app()
    with TestClient(app) as client:
        _generate_artifact(
            client=client,
            fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-20T09:00:00+03:00",
            user_id="det-user",
        )
        _generate_artifact(
            client=client,
            fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
            finalized_at="2026-03-21T09:00:00+03:00",
            user_id="det-user",
        )
        first = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "det-user",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-version-list-det-corr"},
        )
        second = client.get(
            "/v1/forms/income-tax/versions",
            params={
                "user_id": "det-user",
                "tax_year": 2021,
                "form_type": "income_tax_return",
            },
            headers={"X-User-ID": "det-user", "X-Correlation-ID": "forms-version-list-det-corr"},
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
