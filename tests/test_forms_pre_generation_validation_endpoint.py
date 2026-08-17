"""Runtime coverage for deterministic pre-generation validation endpoint behavior."""

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
FINALIZED_AT = "2026-03-24T08:10:00+03:00"


def test_forms_pre_generation_validation_endpoint_returns_valid_for_supported_context() -> None:
    app = create_app()
    form_ready_output, form_version_binding = _build_mapped_and_bound_outputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/validations",
            json={
                "form_ready_output": form_ready_output,
                "form_version_binding": form_version_binding,
            },
            headers={"X-Correlation-ID": "forms-validation-valid-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["validation_status"] == "valid"
    assert payload["is_valid"] is True
    assert payload["findings"] == []
    traceability = cast(dict[str, object], payload["traceability"])
    assert traceability["correlation_id"] == "forms-validation-valid-corr"


def test_forms_pre_generation_validation_endpoint_returns_deterministic_invalid_findings() -> None:
    app = create_app()
    form_ready_output, form_version_binding = _build_mapped_and_bound_outputs(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    invalid_binding = copy.deepcopy(form_version_binding)
    invalid_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    invalid_lineage["computation_id"] = "mismatched-computation-id"
    invalid_binding["binding_lineage"] = invalid_lineage
    request_payload = {
        "form_ready_output": form_ready_output,
        "form_version_binding": invalid_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-invalid-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/validations",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-validation-invalid-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["validation_status"] == "invalid"
    assert first_payload["is_valid"] is False
    findings = cast(list[dict[str, object]], first_payload["findings"])
    assert findings
    finding_codes = {cast(str, finding["code"]) for finding in findings}
    assert "forms_cross_field_inconsistent" in finding_codes
    for finding in findings:
        assert isinstance(finding.get("code"), str) and finding["code"]
        assert isinstance(finding.get("message"), str) and finding["message"]
        assert isinstance(finding.get("field"), str) and finding["field"]
        assert finding.get("severity") == "error"
    assert canonical_json_dumps(first_payload["findings"]) == canonical_json_dumps(
        second_payload["findings"]
    )


def test_forms_pre_generation_validation_endpoint_rejects_malformed_payload() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/validations",
            json={"form_ready_output": [], "form_version_binding": {}},
            headers={"X-Correlation-ID": "forms-validation-malformed-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "forms_request_invalid"
    assert error["reason"] == "forms_request_invalid"


def test_forms_generation_rejects_payload_that_fails_validation_contract() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    invalid_binding = copy.deepcopy(form_version_binding)
    invalid_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    invalid_lineage["computation_id"] = "mismatched-computation-id"
    invalid_binding["binding_lineage"] = invalid_lineage

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-validation-generation-block-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-validation-generation-block-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 409
    assert second.status_code == 409
    assert first_payload["status"] == "blocked"
    assert first_payload["reason"] == "forms_generation_blocked_by_validation"
    validation = cast(dict[str, object], first_payload["validation"])
    assert validation["is_valid"] is False
    findings = cast(list[dict[str, object]], validation.get("findings", []))
    assert findings
    assert any(finding.get("code") == "forms_cross_field_inconsistent" for finding in findings)
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def _build_mapped_and_bound_outputs(
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return form_ready_output, form_version_binding


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
