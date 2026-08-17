"""Deterministic regression matrix for forms pre-generation validation behavior."""

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
FINALIZED_AT = "2026-03-27T09:30:00+03:00"


def test_forms_validation_regression_happy_path_payload_is_consistently_valid() -> None:
    app = create_app()
    _, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    request_payload = {
        "form_ready_output": form_ready_output,
        "form_version_binding": form_version_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-happy-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-happy-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["is_valid"] is True
    assert first_payload["findings"] == []
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_forms_validation_regression_single_required_field_violation_is_deterministic() -> None:
    app = create_app()
    _, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    invalid_binding = copy.deepcopy(form_version_binding)
    del invalid_binding["form_version_id"]
    request_payload = {
        "form_ready_output": form_ready_output,
        "form_version_binding": invalid_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-required-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-required-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    findings = cast(list[dict[str, object]], first_payload["findings"])
    assert first.status_code == 200
    assert first_payload["is_valid"] is False
    assert any(
        finding["code"] == "forms_required_field_missing"
        and finding["field"] == "form_version_binding.form_version_id"
        for finding in findings
    )
    _assert_findings_shape(findings)
    assert canonical_json_dumps(first_payload["findings"]) == canonical_json_dumps(
        second_payload["findings"]
    )


def test_forms_validation_regression_cross_field_inconsistency_is_stable() -> None:
    app = create_app()
    _, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    invalid_binding = copy.deepcopy(form_version_binding)
    binding_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    binding_lineage["input_hash"] = "deadbeef"
    invalid_binding["binding_lineage"] = binding_lineage

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/validations",
            json={
                "form_ready_output": form_ready_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-validation-regression-cross-field-corr"},
        )

    payload = _response_json(response)
    findings = cast(list[dict[str, object]], payload["findings"])
    assert response.status_code == 200
    assert payload["is_valid"] is False
    assert any(
        finding["code"] == "forms_cross_field_inconsistent"
        and finding["field"] == "form_version_binding.binding_lineage.input_hash"
        for finding in findings
    )
    _assert_findings_shape(findings)


def test_forms_validation_regression_multiple_violations_are_ordered_and_repeatable() -> None:
    app = create_app()
    _, form_ready_output, form_version_binding = _build_generation_inputs(
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
    request_payload = {
        "form_ready_output": invalid_form_ready,
        "form_version_binding": invalid_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-multi-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-multi-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    findings = cast(list[dict[str, object]], first_payload["findings"])
    assert first.status_code == 200
    assert first_payload["is_valid"] is False
    _assert_findings_shape(findings)
    finding_codes = {cast(str, finding["code"]) for finding in findings}
    assert "forms_required_field_missing" in finding_codes
    assert "forms_field_value_invalid" in finding_codes
    assert "forms_cross_field_inconsistent" in finding_codes
    ordered_keys = [
        (
            cast(str, finding["field"]),
            cast(str, finding["code"]),
            cast(str, finding["message"]),
        )
        for finding in findings
    ]
    assert ordered_keys == sorted(ordered_keys)
    assert canonical_json_dumps(first_payload["findings"]) == canonical_json_dumps(
        second_payload["findings"]
    )


def test_forms_validation_regression_edge_shape_and_boundary_values_block_generation() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    edge_form_ready = copy.deepcopy(form_ready_output)
    edge_binding = copy.deepcopy(form_version_binding)
    edge_form_ready["computation_identity"] = {}
    edge_form_ready["version_identity"] = {}
    edge_form_ready["liability_fields"] = {}
    edge_form_ready["form_fields"] = {
        "chargeable_income_kes": "0.0",
        "net_income_tax_due_kes": "1.0",
        "refund_due_kes": "1",
        "investment_income_kes": "",
    }
    edge_binding["binding_lineage"] = {}
    edge_binding["tax_year"] = "2023"
    request_payload = {
        "form_ready_output": edge_form_ready,
        "form_version_binding": edge_binding,
    }

    with TestClient(app) as client:
        validation = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-regression-edge-corr"},
        )
        blocked_generation = client.post(
            "/v1/forms/income-tax/artifacts",
            json={**request_payload, "finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-validation-regression-edge-corr"},
        )

    validation_payload = _response_json(validation)
    generation_payload = _response_json(blocked_generation)
    validation_findings = cast(list[dict[str, object]], validation_payload["findings"])
    generation_validation = cast(dict[str, object], generation_payload["validation"])
    generation_findings = cast(list[dict[str, object]], generation_validation["findings"])
    assert validation.status_code == 200
    assert validation_payload["is_valid"] is False
    assert any(finding["code"] == "forms_required_field_missing" for finding in validation_findings)
    assert any(finding["code"] == "forms_field_value_invalid" for finding in validation_findings)
    _assert_findings_shape(validation_findings)
    assert blocked_generation.status_code == 409
    assert generation_payload["status"] == "blocked"
    assert generation_payload["reason"] == "forms_generation_blocked_by_validation"
    assert generation_validation["is_valid"] is False
    assert canonical_json_dumps(validation_findings) == canonical_json_dumps(generation_findings)


def _assert_findings_shape(findings: list[dict[str, object]]) -> None:
    for finding in findings:
        assert isinstance(finding.get("code"), str) and finding["code"]
        assert isinstance(finding.get("message"), str) and finding["message"]
        assert isinstance(finding.get("field"), str) and finding["field"]
        assert finding.get("severity") == "error"


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


def _response_json(response: Any) -> dict[str, Any]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)
