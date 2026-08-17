"""Endpoint coverage for forms income-tax version-binding runtime wiring."""

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

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-15T09:00:00+03:00"


def test_forms_version_binding_endpoint_binds_supported_input() -> None:
    app = create_app()
    mapped_output = _build_mapped_output("income_tax_resident_employment_2023_07_01_case_001.json")

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapped_output},
            headers={"X-Correlation-ID": "forms-binding-success-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["binding_status"] == "bound"
    assert payload["form_type"] == "income_tax_return"
    assert payload["form_version_id"] == "ITX-FORM-20230701-RES-EMP-V1"
    assert payload["form_template_id"] == "income_tax_return_resident_employment_2023_07_01_v1"
    assert payload["historical_version_id"] == "KIT-VER-20230701-A"
    assert payload["effective_start"] == "2023-07-01"
    assert payload["effective_end"] == "2023-08-31"
    traceability = cast(dict[str, object], payload["traceability"])
    assert traceability["correlation_id"] == "forms-binding-success-corr"


def test_forms_version_binding_endpoint_is_deterministic_for_same_input() -> None:
    app = create_app()
    mapped_output = _build_mapped_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    request_payload = {"mapped_output": mapped_output}

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/version-bindings",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-binding-determinism-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/version-bindings",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-binding-determinism-corr"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(_response_json(first)) == canonical_json_dumps(
        _response_json(second)
    )


def test_forms_version_binding_endpoint_rejects_unsupported_window() -> None:
    app = create_app()
    mapped_output = _build_mapped_output("income_tax_resident_employment_2023_07_01_case_001.json")
    version_identity = cast(dict[str, object], mapped_output["version_identity"])
    version_identity["historical_version_id"] = "KIT-VER-20200901-A"
    mapped_output["version_identity"] = version_identity

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapped_output},
            headers={"X-Correlation-ID": "forms-binding-unsupported-window-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "forms_version_not_supported"
    assert error["reason"] == "forms_version_not_supported"


def test_forms_version_binding_endpoint_rejects_ambiguous_binding_context() -> None:
    app = create_app()
    mapped_output = _build_mapped_output("income_tax_resident_employment_2023_07_01_case_001.json")
    taxpayer = cast(dict[str, object], mapped_output["taxpayer"])
    taxpayer["resident_status"] = "non_resident"
    mapped_output["taxpayer"] = taxpayer

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapped_output},
            headers={"X-Correlation-ID": "forms-binding-ambiguous-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "forms_version_binding_ambiguous"
    assert error["reason"] == "forms_version_binding_ambiguous"


def test_forms_version_binding_endpoint_rejects_invalid_payload() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": []},
            headers={"X-Correlation-ID": "forms-binding-invalid-payload-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "forms_request_invalid"
    assert error["reason"] == "forms_request_invalid"


def _build_mapped_output(fixture_name: str) -> dict[str, object]:
    finalized_output = _build_finalized_output(fixture_name)
    return map_finalized_income_tax_output_to_form_ready(finalized_output)


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
