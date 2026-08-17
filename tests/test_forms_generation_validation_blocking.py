"""Regression coverage for deterministic generation blocking on validation failures."""

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
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-26T10:00:00+03:00"


def test_generation_validation_blocking_valid_payload_still_generates() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": form_version_binding,
            },
            headers={"X-Correlation-ID": "forms-generation-block-valid-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["status"] == "ok"
    assert payload["generation_status"] == "generated"


def test_generation_validation_blocking_invalid_payload_returns_structured_block() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    invalid_binding = copy.deepcopy(form_version_binding)
    binding_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    binding_lineage["computation_id"] = "mismatched-computation-id"
    invalid_binding["binding_lineage"] = binding_lineage

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-generation-block-invalid-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 409
    assert payload["status"] == "blocked"
    assert payload["reason"] == "forms_generation_blocked_by_validation"
    validation = cast(dict[str, object], payload["validation"])
    assert validation["is_valid"] is False
    findings = cast(list[dict[str, object]], validation["findings"])
    assert findings
    assert all("code" in finding and "message" in finding for finding in findings)


def test_generation_validation_blocking_multiple_violations_have_stable_order() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    invalid_form_ready = copy.deepcopy(form_ready_output)
    invalid_binding = copy.deepcopy(form_version_binding)
    del invalid_binding["form_version_id"]
    invalid_binding["binding_status"] = "pending"
    binding_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    binding_lineage["computation_id"] = "mismatch-computation-id"
    invalid_binding["binding_lineage"] = binding_lineage
    form_fields = cast(dict[str, object], invalid_form_ready["form_fields"])
    form_fields["refund_due_kes"] = "invalid-money"
    invalid_form_ready["form_fields"] = form_fields

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": invalid_form_ready,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-generation-block-multi-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": invalid_form_ready,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-generation-block-multi-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 409
    assert second.status_code == 409
    first_validation = cast(dict[str, object], first_payload["validation"])
    first_findings = cast(list[dict[str, object]], first_validation["findings"])
    codes = {cast(str, finding["code"]) for finding in first_findings}
    assert "forms_required_field_missing" in codes
    assert "forms_field_value_invalid" in codes
    assert "forms_cross_field_inconsistent" in codes
    ordered_keys = [
        (
            cast(str, finding["field"]),
            cast(str, finding["code"]),
            cast(str, finding["message"]),
        )
        for finding in first_findings
    ]
    assert ordered_keys == sorted(ordered_keys)
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_generation_validation_blocking_malformed_and_unsupported_stay_canonical() -> None:
    app = create_app()
    with TestClient(app) as client:
        malformed = client.post(
            "/v1/forms/income-tax/artifacts",
            json={"form_ready_output": {}, "form_version_binding": {}},
            headers={"X-Correlation-ID": "forms-generation-block-malformed-corr"},
        )
        unsupported = client.post(
            "/v1/forms/vat/artifacts",
            json={},
            headers={"X-Correlation-ID": "forms-generation-block-unsupported-corr"},
        )

    malformed_error = _extract_error_detail(malformed)
    unsupported_error = _extract_error_detail(unsupported)
    assert malformed.status_code == 400
    assert malformed_error["error_code"] == "forms_generation_precondition_missing"
    assert malformed_error["reason"] == "forms_generation_precondition_missing"
    assert unsupported.status_code == 404
    assert unsupported_error["error_code"] == "forms_scope_not_supported"
    assert unsupported_error["reason"] == "forms_scope_not_supported"


def _build_generation_inputs(
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return finalized_output, form_ready_output, form_version_binding


def _build_finalized_output(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = fixture["fixture_id"]
    expected_output = copy.deepcopy(fixture["expected_output"])

    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": FINALIZED_AT,
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
