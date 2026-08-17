"""Endpoint coverage for forms income-tax mapping runtime wiring."""

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

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-15T09:00:00+03:00"


def test_forms_mapping_endpoint_maps_valid_finalized_output() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-mapping-success-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["mapping_status"] == "ok"
    assert payload["form_type"] == "income_tax_return"
    assert payload["form_version"] == "income_tax_vertical_slice_v1"
    mapping_output = cast(dict[str, object], payload["mapping_output"])
    assert mapping_output["supported_lane_id"] == "resident_employment_income_2023_07_01"
    traceability = cast(dict[str, object], payload["traceability"])
    assert traceability["correlation_id"] == "forms-mapping-success-corr"
    assert isinstance(traceability["trace_id"], str)


def test_forms_mapping_endpoint_maps_supported_health_nhif_output() -> None:
    app = create_app()
    finalized_output = _build_finalized_output("health_contribution_nhif_legacy_2010_case_001.json")

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-health-nhif-mapping-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["mapping_status"] == "ok"
    assert payload["form_type"] == "health_contribution_summary"
    mapping_output = cast(dict[str, object], payload["mapping_output"])
    assert mapping_output["supported_lane_id"] == "health_contribution_nhif_legacy_v1_2010_07_16"


def test_forms_mapping_endpoint_maps_supported_health_sha_output() -> None:
    app = create_app()
    finalized_output = _build_finalized_output("health_contribution_sha_shif_case_001.json")

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-health-sha-mapping-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    mapping_output = cast(dict[str, object], payload["mapping_output"])
    assert mapping_output["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"
    contribution_fields = cast(dict[str, object], mapping_output["contribution_fields"])
    assert contribution_fields["total_contribution_kes"] == "1100.00"


def test_forms_mapping_endpoint_maps_supported_health_transition_output() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "health_contribution_transition_boundary_sha_case_001.json"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-health-transition-mapping-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    mapping_output = cast(dict[str, object], payload["mapping_output"])
    version_identity = cast(dict[str, object], mapping_output["version_identity"])
    assert version_identity["historical_version_id"] == "HCH-VER-20241001-A"
    assert mapping_output["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"


def test_forms_mapping_endpoint_maps_supported_health_open_ended_2025_output() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "health_contribution_sha_shif_2025_salaried_case_001.json"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-health-open-ended-mapping-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    mapping_output = cast(dict[str, object], payload["mapping_output"])
    version_identity = cast(dict[str, object], mapping_output["version_identity"])
    assert version_identity["historical_version_id"] == "HCH-VER-20250228-PIT"
    assert version_identity["effective_end"] is None
    assert mapping_output["supported_lane_id"] == "health_contribution_sha_shif_v1_2025_02_28_pit"


def test_forms_mapping_endpoint_is_deterministic_for_identical_input() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    request_payload = {"finalized_output": finalized_output}

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/mappings",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-mapping-determinism-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/mappings",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-mapping-determinism-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert canonical_json_dumps(first_payload) == canonical_json_dumps(second_payload)


def test_forms_mapping_endpoint_rejects_missing_finalized_context_deterministically() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    del finalized_output["finalization_status"]

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-mapping-missing-finalization-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "forms_request_invalid"
    assert error["reason"] == "forms_request_invalid"


def test_forms_mapping_endpoint_rejects_not_finalized_input() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    finalized_output["finalization_status"] = "pending"

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-mapping-not-finalized-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "forms_mapping_input_not_finalized"
    assert error["reason"] == "forms_mapping_input_not_finalized"


def test_forms_mapping_endpoint_rejects_non_ready_health_window_canonically() -> None:
    app = create_app()
    finalized_output = _build_finalized_output("health_contribution_sha_shif_case_001.json")
    result_payload = cast(dict[str, object], finalized_output["result_payload"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    version_identity["historical_version_id"] = "HCH-VER-20031205-A"
    contribution_summary["coverage_status"] = "partially_specified"

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-mapping-unsupported-scope-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "forms_scope_not_supported"
    assert error["reason"] == "forms_scope_not_supported"


def test_forms_mapping_endpoint_rejects_unresolved_health_output_canonically() -> None:
    app = create_app()
    finalized_output = _build_finalized_output("health_contribution_sha_shif_case_001.json")
    result_payload = cast(dict[str, object], finalized_output["result_payload"])
    result_payload["unsupported_or_unresolved"] = [
        {
            "domain_id": "HCD-XCUT-MIXED-CONTEXT-PATHS",
            "reason_code": "mixed_context_requires_separate_path",
        }
    ]

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-health-unresolved-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "forms_scope_not_supported"
    assert error["reason"] == "forms_scope_not_supported"


def test_forms_mapping_wiring_preserves_health_and_supports_version_binding() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    with TestClient(app) as client:
        health = client.get("/healthz", headers={"X-Correlation-ID": "forms-mapping-health-corr"})
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-mapping-to-binding-corr"},
        )
        mapping_payload = _response_json(mapping)
        version_binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-mapping-version-binding-corr"},
        )
        validation = client.post(
            "/v1/forms/income-tax/validations",
            json={"form_ready_output": {}, "form_version_binding": {}},
            headers={"X-Correlation-ID": "forms-mapping-validation-corr"},
        )

    health_payload = _response_json(health)
    version_binding_payload = _response_json(version_binding)
    validation_payload = _response_json(validation)
    assert health.status_code == 200
    assert health_payload["status"] == "ok"
    assert version_binding.status_code == 200
    assert version_binding_payload["binding_status"] == "bound"
    assert validation.status_code == 200
    assert validation_payload["status"] == "ok"
    assert validation_payload["validation_status"] == "invalid"
    assert validation_payload["is_valid"] is False
    assert isinstance(validation_payload["findings"], list)


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
