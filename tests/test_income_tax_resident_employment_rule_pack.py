"""Test governed resident employment income rule-pack behavior."""

from __future__ import annotations

import copy
import json
from uuid import UUID
from typing import Any
from typing import cast
from pathlib import Path
from datetime import date

import pytest
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import MaterializedComputationExecutionResult

TEST_PRINCIPAL_ID = UUID("44444444-4444-4444-4444-444444444444")
TEST_COMPUTATION_ID = UUID("12121212-1212-1212-1212-121212121212")
TEST_AUDIT_EVENT_ID = UUID("34343434-3434-3434-3434-343434343434")
TEST_IDEMPOTENCY_KEY = "idem-resident-employment"
TEST_CORRELATION_ID = "corr-resident-employment"
GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_resident_employment_2023_07_01_case_001.json"
)


def test_bind_rule_selection_resolves_resident_employment_window() -> None:
    """Verify supported effective-date window binds to resident employment pack."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2023,
            rule_version="v1",
            primary_effective_date=date(2023, 7, 1),
            historical_version_id="KIT-VER-20230701-A",
            resident_status_assertion="resident",
            income_category_signature="employment",
        )
    )

    assert bound_rule.binding_id == "income_tax_resident_employment_v1_2023_07_01"


def test_bind_rule_selection_rejects_effective_date_outside_supported_window() -> None:
    """Verify resident employment binding fails outside supported window."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="income_tax",
                regime_type="income_tax",
                regime_identifier=None,
                tax_year=2023,
                rule_version="v1",
                primary_effective_date=date(2023, 9, 1),
                historical_version_id="KIT-VER-20230701-A",
                resident_status_assertion="resident",
                income_category_signature="employment",
            )
        )

    assert error.value.reason == "unknown_rule_binding"


def test_execute_computation_returns_governed_resident_employment_payload() -> None:
    """Verify supported request computes governed resident-employment result."""

    request = _build_supported_request()

    result = execute_computation(request)
    result_payload = result.result_payload
    liability_summary = cast(dict[str, object], result_payload["liability_summary"])
    domain_outcomes = cast(dict[str, object], result_payload["domain_outcomes"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    traceability = cast(dict[str, object], result_payload["traceability"])

    assert result.status == "ok"
    assert result.tax_year == 2023
    assert result.rule_version == "v1"
    assert version_identity["historical_version_id"] == "KIT-VER-20230701-A"
    assert version_identity["effective_start"] == "2023-07-01"
    assert version_identity["effective_end"] == "2023-08-31"
    assert domain_outcomes["deductions_and_exemptions"] == {
        "status": "computed",
        "taxable_base_kes": "960000.00",
        "gross_tax_kes": None,
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": None,
        "decision_refs": ["ITC-POL-803", "ITC-POL-804"],
    }
    assert liability_summary == {
        "assessable_income_kes": "960000.00",
        "chargeable_income_kes": "960000.00",
        "gross_tax_kes": "225400.00",
        "total_reliefs_kes": "28800.00",
        "creditable_withholding_kes": "0.00",
        "installment_tax_credit_kes": "0.00",
        "advance_tax_credit_kes": "0.00",
        "net_income_tax_due_kes": "196600.00",
        "refund_due_kes": "0.00",
        "final_tax_excluded_income_kes": "0.00",
    }
    assert traceability["validation_focus_domains"] == [
        "ITD-CORE-EMPLOYMENT",
        "ITD-CORE-DEDUCTIONS",
        "ITD-CORE-RELIEFS",
    ]
    assert traceability["input_hash"] == result.input_hash


def test_execute_computation_rejects_unsupported_non_cash_benefit() -> None:
    """Verify unsupported employment benefit shapes fail deterministically."""

    request = _build_supported_request()
    payload = request.input_payload
    income_sections = cast(dict[str, object], payload["income_sections"])
    employment = cast(dict[str, object], income_sections["employment"])
    employment_items = cast(list[dict[str, object]], employment["employment_items"])
    employment_items[0]["income_subtype"] = "benefit_in_kind"

    with pytest.raises(InputHashError) as error:
        execute_computation(request)

    assert error.value.reason == "unsupported_employment_income_subtype"


def test_execution_endpoint_rejects_unsupported_scope_with_deterministic_error() -> None:
    """Verify endpoint maps unsupported resident-employment scope to stable 400 envelope."""

    client = _build_rule_pack_test_client()
    payload = _supported_request_payload()
    input_payload = cast(dict[str, object], payload["input_payload"])
    income_sections = input_payload["income_sections"]
    employment = cast(dict[str, object], cast(dict[str, object], income_sections)["employment"])
    employment_items = cast(list[dict[str, object]], employment["employment_items"])
    employment_items[0]["income_subtype"] = "benefit_in_kind"

    response = client.post(
        "/computations/execute",
        json=payload,
        headers=_execution_headers(),
    )
    body = _response_json(response)
    detail = cast(dict[str, object], body["detail"])
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_employment_income_subtype"


def test_resident_employment_execution_is_deterministic_for_logical_equivalent_requests() -> None:
    """Verify supported resident-employment requests stay deterministic under key reordering."""

    request_one = _build_supported_request()
    request_two = ComputationExecutionRequest.model_validate(
        {
            "tax_type": "income_tax",
            "regime_type": "income_tax",
            "regime_identifier": None,
            "tax_year": 2023,
            "rule_version": "v1",
            "input_payload": {
                "traceability_context": {
                    "preparation_profile": "manual_structured_entry",
                    "source_record_ids": ["src-payroll-001"],
                    "completeness_assertion": "complete",
                    "evidence_reference_ids": [],
                },
                "payment_pathways": {
                    "advance_tax_events": [],
                    "installment_tax_events": [],
                    "withholding_events": [],
                },
                "claims": {
                    "exemption_claims": [],
                    "deduction_claims": [],
                    "relief_claims": [
                        {
                            "asserted_amount_kes": "28800.00",
                            "claim_reference_id": "claim-personal-relief-001",
                            "relief_type": "personal_relief",
                        }
                    ],
                },
                "income_sections": {
                    "employment": {
                        "employment_items": [
                            {
                                "event_date": "2023-07-15",
                                "income_subtype": "cash_emolument",
                                "amount_kes": "960000.00",
                                "employer_reference_id": "employer-001",
                            }
                        ]
                    }
                },
                "taxpayer_context": {
                    "resident_status_assertion": "resident",
                    "taxpayer_kind": "individual",
                    "residence_reference_period_start": "2023-01-01",
                    "residence_reference_period_end": "2023-12-31",
                },
                "version_context": {
                    "historical_version_id": "KIT-VER-20230701-A",
                    "version_selection_basis": "specific_event_date",
                    "primary_effective_date": "2023-07-01",
                    "source_anchor_ids": ["ITA-2023-07-01-A"],
                },
            },
        }
    )

    first = execute_computation(request_one).model_dump(mode="json")
    second = execute_computation(request_two).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


def test_resident_employment_golden_fixture_matches_exact_output() -> None:
    """Verify the resident-employment golden case locks exact governed output."""

    fixture = _load_golden_fixture()
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(fixture["request"])
    ).model_dump(mode="json")

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def test_resident_employment_golden_fixture_detects_output_drift() -> None:
    """Verify resident-employment golden harness fails on drift."""

    fixture = _load_golden_fixture()
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(cast(dict[str, object], fixture["request"]))
    ).model_dump(mode="json")
    drifted_expected_output = copy.deepcopy(cast(dict[str, object], fixture["expected_output"]))
    drifted_payload = cast(dict[str, object], drifted_expected_output["result_payload"])
    liability_summary = cast(dict[str, object], drifted_payload["liability_summary"])
    liability_summary["net_income_tax_due_kes"] = "196601.00"

    with pytest.raises(AssertionError):
        assert _canonical_json(actual_output) == _canonical_json(drifted_expected_output)


def _build_supported_request() -> ComputationExecutionRequest:
    return ComputationExecutionRequest.model_validate(_supported_request_payload())


def _supported_request_payload() -> dict[str, object]:
    return {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2023,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": "2023-07-01",
                "version_selection_basis": "specific_event_date",
                "historical_version_id": "KIT-VER-20230701-A",
                "source_anchor_ids": ["ITA-2023-07-01-A"],
            },
            "taxpayer_context": {
                "taxpayer_kind": "individual",
                "resident_status_assertion": "resident",
                "residence_reference_period_start": "2023-01-01",
                "residence_reference_period_end": "2023-12-31",
            },
            "income_sections": {
                "employment": {
                    "employment_items": [
                        {
                            "income_subtype": "cash_emolument",
                            "amount_kes": "960000.00",
                            "event_date": "2023-07-15",
                            "employer_reference_id": "employer-001",
                        }
                    ]
                }
            },
            "claims": {
                "relief_claims": [
                    {
                        "relief_type": "personal_relief",
                        "claim_reference_id": "claim-personal-relief-001",
                        "asserted_amount_kes": "28800.00",
                    }
                ],
                "deduction_claims": [],
                "exemption_claims": [],
            },
            "payment_pathways": {
                "withholding_events": [],
                "installment_tax_events": [],
                "advance_tax_events": [],
            },
            "traceability_context": {
                "source_record_ids": ["src-payroll-001"],
                "preparation_profile": "manual_structured_entry",
                "completeness_assertion": "complete",
                "evidence_reference_ids": [],
            },
        },
    }


def _load_golden_fixture() -> dict[str, object]:
    with GOLDEN_CASE_PATH.open("r", encoding="utf-8") as fixture_file:
        fixture = json.load(fixture_file)
    return cast(dict[str, object], fixture)


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _build_rule_pack_test_client() -> TestClient:
    app = create_app()
    app.state.materializer = _stub_materializer
    return TestClient(app)


def _stub_materializer(
    execution_request: ComputationExecutionRequest,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
) -> MaterializedComputationExecutionResult:
    return MaterializedComputationExecutionResult(
        status="ok",
        computation_id=TEST_COMPUTATION_ID,
        computation_result_id=TEST_COMPUTATION_ID,
        audit_event_id=TEST_AUDIT_EVENT_ID,
        idempotency_key=context.idempotency_key,
        correlation_id=context.correlation_id,
        tax_type=execution_request.tax_type,
        regime_type=execution_request.regime_type,
        tax_year=execution_request.tax_year,
        rule_version=execution_result.rule_version,
        input_hash=execution_result.input_hash,
        result_payload=execution_result.result_payload,
    )


def _execution_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TEST_PRINCIPAL_ID}:IndividualTaxpayer",
        "Idempotency-Key": TEST_IDEMPOTENCY_KEY,
        "X-Correlation-ID": TEST_CORRELATION_ID,
    }


def _response_json(response: object) -> dict[str, object]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
