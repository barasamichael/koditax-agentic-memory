"""Deterministic rejection checks for disabled forms template capabilities."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.forms.app.main import create_app

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
DISABLED_TEMPLATE_CODES = ("IT2", "VAT3", "P10", "P9")


@pytest.mark.parametrize("template_code", DISABLED_TEMPLATE_CODES)
def test_disabled_template_code_rejected_across_forms_endpoints(template_code: str) -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-07T09:00:00+03:00",
    )
    disabled_payload = {
        "template_code": template_code,
        "finalized_output": finalized_output,
        "mapped_output": {},
        "form_ready_output": {},
        "form_version_binding": {},
    }

    with TestClient(app) as client:
        _assert_disabled_error(
            response=client.post(
                "/v1/forms/income-tax/mappings",
                json={
                    "template_code": template_code,
                    "finalized_output": disabled_payload["finalized_output"],
                },
                headers={"X-Correlation-ID": f"forms-template-disabled-map-{template_code}"},
            ),
            expected_template_code=template_code,
        )
        _assert_disabled_error(
            response=client.post(
                "/v1/forms/income-tax/version-bindings",
                json={
                    "template_code": template_code,
                    "mapped_output": disabled_payload["mapped_output"],
                },
                headers={"X-Correlation-ID": f"forms-template-disabled-bind-{template_code}"},
            ),
            expected_template_code=template_code,
        )
        _assert_disabled_error(
            response=client.post(
                "/v1/forms/income-tax/validations",
                json={
                    "template_code": template_code,
                    "form_ready_output": disabled_payload["form_ready_output"],
                    "form_version_binding": disabled_payload["form_version_binding"],
                },
                headers={"X-Correlation-ID": f"forms-template-disabled-validate-{template_code}"},
            ),
            expected_template_code=template_code,
        )
        _assert_disabled_error(
            response=client.post(
                "/v1/forms/income-tax/artifacts",
                json={
                    "template_code": template_code,
                    "finalized_output": disabled_payload["finalized_output"],
                    "form_ready_output": disabled_payload["form_ready_output"],
                    "form_version_binding": disabled_payload["form_version_binding"],
                },
                headers={"X-Correlation-ID": f"forms-template-disabled-artifact-{template_code}"},
            ),
            expected_template_code=template_code,
        )
        _assert_disabled_error(
            response=client.post(
                "/v1/forms/income-tax/pre-populations",
                json={
                    "template_code": template_code,
                    "form_type": "income_tax_return",
                    "target_tax_year": 2023,
                },
                headers={"X-Correlation-ID": f"forms-template-disabled-prepop-{template_code}"},
            ),
            expected_template_code=template_code,
        )
        _assert_disabled_error(
            response=client.get(
                "/v1/forms/income-tax/versions",
                params={"user_id": "disabled-user", "tax_year": 2023, "form_type": template_code},
                headers={"X-Correlation-ID": f"forms-template-disabled-list-{template_code}"},
            ),
            expected_template_code=template_code,
        )


@pytest.mark.parametrize("template_code", DISABLED_TEMPLATE_CODES)
def test_disabled_template_code_rejected_in_batch_item(template_code: str) -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-07T09:15:00+03:00",
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts/batch",
            json={
                "items": [
                    {
                        "scope": "income-tax",
                        "payload": {
                            "template_code": template_code,
                            "finalized_output": finalized_output,
                            "form_ready_output": {},
                            "form_version_binding": {},
                        },
                    }
                ]
            },
            headers={"X-Correlation-ID": f"forms-template-disabled-batch-{template_code}"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["summary"] == {"total": 1, "succeeded": 0, "failed": 1}
    results = cast(list[dict[str, object]], payload["results"])
    error = cast(dict[str, object], results[0]["error"])
    assert error["error_code"] == "forms_template_capability_disabled"
    assert error["reason"] == "forms_template_capability_disabled"


@pytest.mark.parametrize("template_code", DISABLED_TEMPLATE_CODES)
def test_disabled_template_rejection_is_deterministic(template_code: str) -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
        finalized_at="2026-04-07T09:30:00+03:00",
    )
    request_payload = {
        "template_code": template_code,
        "finalized_output": finalized_output,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/mappings",
            json=request_payload,
            headers={"X-Correlation-ID": f"forms-template-disabled-det-{template_code}"},
        )
        second = client.post(
            "/v1/forms/income-tax/mappings",
            json=request_payload,
            headers={"X-Correlation-ID": f"forms-template-disabled-det-{template_code}"},
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 409
    assert second.status_code == 409
    assert first_error["error_code"] == "forms_template_capability_disabled"
    assert first_error["reason"] == "forms_template_capability_disabled"
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_supported_income_tax_mapping_path_works_unchanged() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        finalized_at="2026-04-07T10:00:00+03:00",
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-template-disabled-supported-regression"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["mapping_status"] == "ok"


def _assert_disabled_error(*, response: Any, expected_template_code: str) -> None:
    error = _extract_error_detail(response)
    details = error.get("details")
    assert isinstance(details, dict)
    details_map = cast(dict[str, object], details)
    assert response.status_code == 409
    assert error["error_code"] == "forms_template_capability_disabled"
    assert error["reason"] == "forms_template_capability_disabled"
    assert details_map.get("template_code") == expected_template_code


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
