"""Integrated runtime regression coverage for forms mapping->binding->generation pipeline."""

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

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-23T09:15:00+03:00"


def test_forms_runtime_pipeline_happy_path_is_lineage_and_audit_stable() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-runtime-pipeline-happy-corr"},
        )
        mapping_payload = _response_json(mapping)

        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-runtime-pipeline-happy-corr"},
        )
        binding_payload = _response_json(binding)

        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-Correlation-ID": "forms-runtime-pipeline-happy-corr"},
        )
        artifact_payload = _response_json(artifact)

    assert mapping.status_code == 200
    assert binding.status_code == 200
    assert artifact.status_code == 201

    mapping_output = cast(dict[str, object], mapping_payload["mapping_output"])
    mapping_identity = cast(dict[str, object], mapping_output["computation_identity"])
    mapping_version = cast(dict[str, object], mapping_output["version_identity"])
    binding_output = cast(dict[str, object], binding_payload["binding_output"])
    binding_lineage = cast(dict[str, object], binding_output["binding_lineage"])
    artifact_output = cast(dict[str, object], artifact_payload["artifact_output"])

    assert mapping_identity["computation_id"] == finalized_output["computation_id"]
    assert binding_lineage["computation_id"] == finalized_output["computation_id"]
    assert artifact_output["computation_id"] == finalized_output["computation_id"]
    assert mapping_version["historical_version_id"] == binding_output["historical_version_id"]
    assert binding_output["historical_version_id"] == artifact_output["historical_version_id"]
    assert binding_output["form_version_id"] == artifact_output["form_version_id"]

    mapping_audit = cast(dict[str, object], mapping_payload["audit_evidence"])
    binding_audit = cast(dict[str, object], binding_payload["audit_evidence"])
    artifact_audit = cast(dict[str, object], artifact_payload["audit_evidence"])
    assert mapping_audit["event_type"] == "forms_mapping_completed"
    assert binding_audit["event_type"] == "forms_version_binding_completed"
    assert artifact_audit["event_type"] == "forms_artifact_generated"
    assert mapping_audit["correlation_id"] == "forms-runtime-pipeline-happy-corr"
    assert binding_audit["correlation_id"] == "forms-runtime-pipeline-happy-corr"
    assert artifact_audit["correlation_id"] == "forms-runtime-pipeline-happy-corr"
    assert isinstance(mapping_audit["audit_event_id"], str) and mapping_audit["audit_event_id"]
    assert isinstance(binding_audit["audit_event_id"], str) and binding_audit["audit_event_id"]
    assert isinstance(artifact_audit["audit_event_id"], str) and artifact_audit["audit_event_id"]

    mapping_lineage = cast(dict[str, object], mapping_audit["lineage_reference"])
    binding_lineage_ref = cast(dict[str, object], binding_audit["lineage_reference"])
    artifact_lineage = cast(dict[str, object], artifact_audit["lineage_reference"])
    assert mapping_lineage["form_type"] == "income_tax_return"
    assert isinstance(mapping_lineage.get("tax_year"), int)
    assert binding_lineage_ref["form_type"] == "income_tax_return"
    assert isinstance(binding_lineage_ref.get("tax_year"), int)
    assert artifact_lineage["form_type"] == "income_tax_return"
    assert isinstance(artifact_lineage.get("tax_year"), int)


def test_forms_runtime_pipeline_invalid_mapping_input_is_deterministic() -> None:
    app = create_app()
    request_payload: dict[str, object] = {"finalized_output": []}

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/mappings",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-runtime-invalid-mapping-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/mappings",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-runtime-invalid-mapping-corr"},
        )

    _assert_error_envelope(
        response=first,
        expected_status=400,
        expected_reason="forms_request_invalid",
    )
    _assert_error_envelope(
        response=second,
        expected_status=400,
        expected_reason="forms_request_invalid",
    )
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_runtime_pipeline_recognized_tax_domain_mapping_rejected_canonically() -> None:
    app = create_app()
    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/vat/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-runtime-unsupported-scope-corr"},
        )
        second = client.post(
            "/v1/forms/vat/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-runtime-unsupported-scope-corr"},
        )

    _assert_error_envelope(
        response=first,
        expected_status=501,
        expected_reason="unimplemented_tax_domain_mapping",
    )
    _assert_error_envelope(
        response=second,
        expected_status=501,
        expected_reason="unimplemented_tax_domain_mapping",
    )
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_runtime_pipeline_version_rejections_cover_unsupported_and_ambiguous() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-runtime-version-rejections-corr"},
        )

    mapping_payload = _response_json(mapping)
    mapped_output = cast(dict[str, object], mapping_payload["mapping_output"])

    unsupported_mapped = copy.deepcopy(mapped_output)
    unsupported_version_identity = cast(dict[str, object], unsupported_mapped["version_identity"])
    unsupported_version_identity["historical_version_id"] = "KIT-VER-19990101-A"
    unsupported_mapped["version_identity"] = unsupported_version_identity

    ambiguous_mapped = copy.deepcopy(mapped_output)
    ambiguous_taxpayer = cast(dict[str, object], ambiguous_mapped["taxpayer"])
    ambiguous_taxpayer["resident_status"] = "non_resident"
    ambiguous_mapped["taxpayer"] = ambiguous_taxpayer

    with TestClient(app) as client:
        unsupported = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": unsupported_mapped},
            headers={"X-Correlation-ID": "forms-runtime-version-rejections-corr"},
        )
        ambiguous = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": ambiguous_mapped},
            headers={"X-Correlation-ID": "forms-runtime-version-rejections-corr"},
        )

    _assert_error_envelope(
        response=unsupported,
        expected_status=409,
        expected_reason="forms_version_not_supported",
    )
    _assert_error_envelope(
        response=ambiguous,
        expected_status=409,
        expected_reason="forms_version_binding_ambiguous",
    )


def test_forms_runtime_pipeline_generation_precondition_missing_rejected_deterministically() -> (
    None
):
    app = create_app()
    request_payload: dict[str, object] = {
        "form_ready_output": {},
        "form_version_binding": {},
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-runtime-generation-precondition-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json=request_payload,
            headers={"X-Correlation-ID": "forms-runtime-generation-precondition-corr"},
        )

    _assert_error_envelope(
        response=first,
        expected_status=400,
        expected_reason="forms_generation_precondition_missing",
    )
    _assert_error_envelope(
        response=second,
        expected_status=400,
        expected_reason="forms_generation_precondition_missing",
    )
    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first_error["error_code"] == second_error["error_code"]
    assert first_error["reason"] == second_error["reason"]


def test_forms_runtime_pipeline_generation_blocks_validation_contract_violations() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-runtime-validation-gate-corr"},
        )
        mapped_payload = _response_json(mapping)
        version_binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapped_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-runtime-validation-gate-corr"},
        )
        bound_payload = _response_json(version_binding)

    form_ready_output = cast(dict[str, object], mapped_payload["mapping_output"])
    form_version_binding = cast(dict[str, object], bound_payload["binding_output"])
    invalid_binding = copy.deepcopy(form_version_binding)
    invalid_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    invalid_lineage["computation_id"] = "mismatch-computation-id"
    invalid_binding["binding_lineage"] = invalid_lineage

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-runtime-validation-gate-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": form_ready_output,
                "form_version_binding": invalid_binding,
            },
            headers={"X-Correlation-ID": "forms-runtime-validation-gate-corr"},
        )

    first_payload = _response_json(first)
    second_payload = _response_json(second)
    assert first.status_code == 409
    assert second.status_code == 409
    assert first_payload["status"] == "blocked"
    assert second_payload["status"] == "blocked"
    assert first_payload["reason"] == "forms_generation_blocked_by_validation"
    assert second_payload["reason"] == "forms_generation_blocked_by_validation"
    first_validation = cast(dict[str, object], first_payload["validation"])
    second_validation = cast(dict[str, object], second_payload["validation"])
    assert first_validation["is_valid"] is False
    assert second_validation["is_valid"] is False
    first_findings = cast(list[dict[str, object]], first_validation["findings"])
    second_findings = cast(list[dict[str, object]], second_validation["findings"])
    assert first_findings and second_findings
    first_finding_codes = [cast(str, finding["code"]) for finding in first_findings]
    second_finding_codes = [cast(str, finding["code"]) for finding in second_findings]
    assert first_finding_codes == second_finding_codes


def _assert_error_envelope(
    *,
    response: Any,
    expected_status: int,
    expected_reason: str,
) -> None:
    error = _extract_error_detail(response)
    assert response.status_code == expected_status
    assert error["error_code"] == expected_reason
    assert error["reason"] == expected_reason
    assert isinstance(error["message"], str) and error["message"]


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
