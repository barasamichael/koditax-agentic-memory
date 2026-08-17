"""Runtime checks for forms service app factory and baseline routes."""

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


def test_forms_app_factory_boots_and_exposes_health_endpoint() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Correlation-ID": "forms-health-corr"})

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["service"] == "forms"
    traceability = cast(dict[str, object], payload["traceability"])
    assert traceability["correlation_id"] == "forms-health-corr"
    assert isinstance(traceability["trace_id"], str)
    assert traceability["trace_id"]


def test_forms_baseline_routes_exist_and_fail_closed_as_not_implemented() -> None:
    app = create_app()
    with TestClient(app) as client:
        for (
            method,
            path,
            json_payload,
            expected_status,
            expected_reason,
        ) in _baseline_route_matrix():
            response = _request(
                client=client,
                method=method,
                path=path,
                json_payload=json_payload,
                correlation_id="forms-baseline-routes-corr",
            )
            assert response.status_code == expected_status
            if expected_reason is None:
                payload = _response_json(response)
                assert payload["status"] == "ok"
                if path == "/v1/forms/income-tax/mappings":
                    assert payload["mapping_status"] == "ok"
                    governed_validation = cast(dict[str, object], payload["governed_validation"])
                    assert governed_validation["validation_status"] == "accepted"
                if path == "/v1/forms/income-tax/validations":
                    assert payload["validation_status"] == "invalid"
                    assert payload["is_valid"] is False
                    assert isinstance(payload["findings"], list)
                continue
            error = _extract_error_detail(response)
            assert error["error_code"] == expected_reason
            assert error["reason"] == expected_reason


def test_forms_unimplemented_error_envelope_is_deterministic_for_same_request() -> None:
    app = create_app()
    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": {"status": "ok"}},
            headers={"X-Correlation-ID": "forms-unimplemented-determinism-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": {"status": "ok"}},
            headers={"X-Correlation-ID": "forms-unimplemented-determinism-corr"},
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_forms_health_domain_mapping_executes_supported_governed_output() -> None:
    app = create_app()
    finalized_output = _build_finalized_output("health_contribution_sha_shif_case_001.json")

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-health-scope-corr"},
        )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["form_type"] == "health_contribution_summary"
    governed_validation = cast(dict[str, object], payload["governed_validation"])
    assert governed_validation["validation_status"] == "accepted"
    mapping_output = cast(dict[str, object], payload["mapping_output"])
    assert mapping_output["supported_lane_id"] == "health_contribution_sha_shif_v1_2024_10_01"


def test_forms_unknown_tax_domain_fails_closed_with_invalid_domain_reason() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/mystery-tax/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-invalid-domain-corr"},
        )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "invalid_tax_domain"
    assert error["reason"] == "invalid_tax_domain"


def test_income_tax_artifact_generation_fails_closed_when_governed_validation_rejects() -> None:
    app = create_app()
    valid_finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        mapping_response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": valid_finalized_output},
            headers={"X-Correlation-ID": "forms-artifact-governed-validation-map"},
        )
        assert mapping_response.status_code == 200
        mapping_payload = _response_json(mapping_response)

        version_binding_response = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-artifact-governed-validation-bind"},
        )
        assert version_binding_response.status_code == 200
        binding_payload = _response_json(version_binding_response)

        invalid_finalized_output = copy.deepcopy(valid_finalized_output)
        invalid_finalized_output["finalization_status"] = "draft"
        response = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": invalid_finalized_output,
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-Correlation-ID": "forms-artifact-governed-validation-block"},
        )

    payload = _response_json(response)
    assert response.status_code == 409
    assert payload["status"] == "blocked"
    assert payload["reason"] == "forms_generation_blocked_by_validation"
    governed_validation = cast(dict[str, object], payload["governed_validation"])
    assert governed_validation["validation_status"] == "rejected"
    issues = cast(list[dict[str, object]], governed_validation["issues"])
    assert issues[0]["code"] == "forms_income_tax_finalization_incomplete"


def _request(
    *,
    client: TestClient,
    method: str,
    path: str,
    json_payload: dict[str, object] | None,
    correlation_id: str,
) -> Any:
    if method == "post":
        return client.post(
            path,
            json=json_payload,
            headers={"X-Correlation-ID": correlation_id},
        )
    if method == "get":
        return client.get(path, headers={"X-Correlation-ID": correlation_id})
    raise AssertionError(f"Unsupported test method: {method}")


def _baseline_route_matrix() -> (
    tuple[tuple[str, str, dict[str, object] | None, int, str | None], ...]
):
    artifact_id = "a" * 64
    return (
        (
            "post",
            "/v1/forms/income-tax/mappings",
            {
                "finalized_output": {
                    "computation_id": "c63cd26d-6d34-545a-833f-ca7888856670",
                    "finalization_status": "finalized",
                    "finalized_at": "2026-03-15T09:00:00+03:00",
                    "finalized_audit_event_id": "32f8f1af-7f49-58a4-a0ca-a36fed22d09b",
                    "tax_type": "income_tax",
                    "regime_type": "income_tax",
                    "tax_year": 2023,
                    "rule_version": "v1",
                    "input_hash": (
                        "3a8fa6d33c6648cf78f5d7f2688ec6f0737f7193f2bd25688e988bf3cec330f9"
                    ),
                    "result_payload": {
                        "unsupported_or_unresolved": [],
                        "version_identity": {
                            "historical_version_id": "KIT-VER-20230701-A",
                            "effective_start": "2023-07-01",
                            "effective_end": "9999-12-31",
                            "version_selection_basis": "governed",
                            "source_anchor_ids": ["SRC-001"],
                        },
                        "taxpayer_outcome": {
                            "taxpayer_kind": "individual",
                            "resident_status": "resident",
                            "classification_outcome": "employment_only",
                        },
                        "liability_summary": {
                            "assessable_income_kes": "1200000.00",
                            "chargeable_income_kes": "1080000.00",
                            "gross_tax_kes": "216000.00",
                            "total_reliefs_kes": "28800.00",
                            "creditable_withholding_kes": "0.00",
                            "final_tax_excluded_income_kes": "0.00",
                            "installment_tax_credit_kes": "0.00",
                            "advance_tax_credit_kes": "0.00",
                            "net_income_tax_due_kes": "187200.00",
                            "refund_due_kes": "0.00",
                        },
                        "impact_summary": {
                            "relief_impacts": [],
                            "deduction_impacts": [],
                            "exemption_impacts": [],
                        },
                        "domain_outcomes": {
                            "employment": {
                                "status": "computed",
                                "taxable_base_kes": "1200000.00",
                                "gross_tax_kes": "216000.00",
                                "final_tax_amount_kes": None,
                                "creditable_amount_kes": "0.00",
                                "decision_refs": ["EMP-001"],
                            },
                            "investment": {
                                "status": "not_applicable",
                                "taxable_base_kes": None,
                                "gross_tax_kes": None,
                                "final_tax_amount_kes": None,
                                "creditable_amount_kes": None,
                                "decision_refs": [],
                            },
                            "deductions_and_exemptions": {
                                "status": "computed",
                                "taxable_base_kes": None,
                                "gross_tax_kes": None,
                                "final_tax_amount_kes": None,
                                "creditable_amount_kes": None,
                                "decision_refs": [],
                            },
                            "reliefs": {
                                "status": "computed",
                                "taxable_base_kes": None,
                                "gross_tax_kes": None,
                                "final_tax_amount_kes": None,
                                "creditable_amount_kes": None,
                                "decision_refs": [],
                            },
                            "withholding": {
                                "status": "not_applicable",
                                "taxable_base_kes": None,
                                "gross_tax_kes": None,
                                "final_tax_amount_kes": None,
                                "creditable_amount_kes": None,
                                "decision_refs": [],
                            },
                            "business": {"status": "not_applicable"},
                            "rental": {"status": "not_applicable"},
                            "advance_tax": {"status": "not_applicable"},
                            "installment_tax": {"status": "not_applicable"},
                            "adjacent_regime_interactions": {"status": "not_applicable"},
                            "prescribed_rate_resolution": {"status": "not_applicable"},
                        },
                        "treatment_decisions": {
                            "withholding_treatments": [],
                            "adjacent_regime_flags": [],
                        },
                        "traceability": {
                            "applied_policy_ids": ["POL-001"],
                            "validation_focus_domains": ["employment"],
                            "replay_safe": True,
                            "computation_status": "finalized",
                        },
                    },
                }
            },
            200,
            None,
        ),
        (
            "post",
            "/v1/forms/income-tax/version-bindings",
            {"mapped_output": {}},
            400,
            "forms_request_invalid",
        ),
        (
            "post",
            "/v1/forms/income-tax/validations",
            {"form_ready_output": {}, "form_version_binding": {}},
            200,
            None,
        ),
        (
            "post",
            "/v1/forms/income-tax/artifacts",
            {"form_ready_output": {}, "form_version_binding": {}},
            400,
            "forms_generation_precondition_missing",
        ),
        (
            "get",
            f"/v1/forms/income-tax/artifacts/{artifact_id}/versions/form-version-v1/metadata",
            None,
            404,
            "forms_history_not_found",
        ),
    )


def _extract_error_detail(response: Any) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "trace_id" in detail
    assert "correlation_id" in detail
    return cast(dict[str, object], detail)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


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
