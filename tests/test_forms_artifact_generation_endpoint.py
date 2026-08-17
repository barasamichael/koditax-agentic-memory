"""Endpoint coverage for forms income-tax artifact-generation runtime wiring."""

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
FINALIZED_AT = "2026-03-20T10:30:00+03:00"


def test_forms_artifact_generation_endpoint_generates_lineage_bound_artifact() -> None:
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
            headers={"X-Correlation-ID": "forms-artifact-success-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 201
    assert payload["status"] == "ok"
    assert payload["generation_status"] == "generated"
    assert payload["artifact_type"] == "income_tax_form_artifact"
    assert payload["artifact_id"]
    assert payload["artifact_hash"]
    storage_metadata = cast(dict[str, object], payload["storage_metadata"])
    assert set(storage_metadata.keys()) == {
        "storage_object_id",
        "storage_backend",
        "content_type",
        "size_bytes",
        "artifact_hash",
    }
    assert storage_metadata["artifact_hash"] == payload["artifact_hash"]
    assert payload["form_version_id"] == "ITX-FORM-20230701-RES-EMP-V1"
    assert payload["immutability_status"] == "immutable"
    assert payload["immutable"] is True
    assert payload["created_at"] == FINALIZED_AT
    assert payload["generated_at"] == FINALIZED_AT
    audit_evidence = cast(dict[str, object], payload["audit_evidence"])
    assert audit_evidence["event_type"] == "forms_artifact_generated"
    assert audit_evidence["event_timestamp"] == FINALIZED_AT
    assert audit_evidence["correlation_id"] == "forms-artifact-success-corr"
    assert isinstance(audit_evidence["audit_event_id"], str)
    assert isinstance(audit_evidence["lineage_reference"], dict)
    assert isinstance(audit_evidence["actor_context"], dict)
    lineage_reference = cast(dict[str, object], payload["lineage_reference"])
    assert lineage_reference["computation_id"] == finalized_output["computation_id"]
    assert lineage_reference["form_version_id"] == payload["form_version_id"]
    assert lineage_reference["historical_version_id"] == payload["historical_version_id"]
    traceability = cast(dict[str, object], payload["traceability"])
    assert traceability["correlation_id"] == "forms-artifact-success-corr"


def test_forms_artifact_generation_endpoint_is_immutable_for_repeated_request() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    request_payload = {
        "finalized_output": finalized_output,
        "form_ready_output": form_ready_output,
        "form_version_binding": form_version_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-artifact-immutable-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-artifact-immutable-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first_payload["artifact_id"] == second_payload["artifact_id"]
    assert first_payload["artifact_hash"] == second_payload["artifact_hash"]
    assert canonical_json_dumps(first_payload["artifact_output"]) == canonical_json_dumps(
        second_payload["artifact_output"]
    )


def test_forms_artifact_generation_endpoint_rejects_missing_preconditions() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={"form_ready_output": {}, "form_version_binding": {}},
            headers={"X-Correlation-ID": "forms-artifact-missing-preconditions-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "forms_generation_precondition_missing"
    assert error["reason"] == "forms_generation_precondition_missing"


def test_forms_artifact_generation_endpoint_rejects_unsupported_scope_or_version() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_version_binding["supported_lane_id"] = "resident_employment_income_2021_01_01"

    with TestClient(app) as client:
        unsupported_scope = client.post(
            "/v1/forms/vat/artifacts",
            json={},
            headers={"X-Correlation-ID": "forms-artifact-unsupported-scope-corr"},
        )
        unsupported_version = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": form_version_binding,
            },
            headers={"X-Correlation-ID": "forms-artifact-unsupported-version-corr"},
        )

    unsupported_scope_error = _extract_error_detail(unsupported_scope)
    unsupported_version_payload = _response_json(unsupported_version)
    assert unsupported_scope.status_code == 404
    assert unsupported_scope_error["error_code"] == "forms_scope_not_supported"
    assert unsupported_scope_error["reason"] == "forms_scope_not_supported"
    assert unsupported_version.status_code == 409
    assert unsupported_version_payload["status"] == "blocked"
    assert unsupported_version_payload["reason"] == "forms_generation_blocked_by_validation"
    validation = cast(dict[str, object], unsupported_version_payload["validation"])
    assert validation["is_valid"] is False


def test_forms_artifact_generation_endpoint_is_deterministic_for_same_valid_input() -> None:
    app = create_app()
    finalized_output, form_ready_output, form_version_binding = _build_generation_inputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    request_payload = {
        "finalized_output": finalized_output,
        "form_ready_output": form_ready_output,
        "form_version_binding": form_version_binding,
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-artifact-determinism-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-artifact-determinism-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 201
    assert second.status_code == 201
    stable_keys = [
        "generation_status",
        "artifact_id",
        "artifact_hash",
        "form_version_id",
        "historical_version_id",
        "created_at",
        "immutability_status",
        "immutable",
        "lineage_reference",
        "storage_metadata",
    ]
    for key in stable_keys:
        assert canonical_json_dumps(first_payload[key]) == canonical_json_dumps(second_payload[key])


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
