"""Test governed SHA/SHIF health-contribution rule-pack behavior."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Any
from typing import cast
from pathlib import Path
from datetime import date

import pytest
from jsonschema import FormatChecker
from fastapi.testclient import TestClient
from jsonschema.validators import validator_for

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

TEST_PRINCIPAL_ID = UUID("75757575-7575-7575-7575-757575757575")
TEST_COMPUTATION_ID = UUID("86868686-8686-8686-8686-868686868686")
TEST_AUDIT_EVENT_ID = UUID("97979797-9797-9797-9797-979797979797")
TEST_IDEMPOTENCY_KEY = "idem-health-sha"
TEST_CORRELATION_ID = "corr-health-sha"
RESULT_SCHEMA_PATH = Path("contracts/tools/schemas/health_contribution_result_payload.schema.json")


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "expected_binding_id"),
    [
        (
            2024,
            date(2024, 10, 31),
            "HCH-VER-20241001-A",
            "health_contribution_sha_shif_v1_2024_10_01",
        ),
        (
            2025,
            date(2025, 3, 31),
            "HCH-VER-20250228-PIT",
            "health_contribution_sha_shif_v1_2025_02_28_pit",
        ),
    ],
)
def test_bind_rule_selection_resolves_supported_sha_windows(
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_binding_id: str,
) -> None:
    """Verify each implementation-ready SHA/SHIF window binds deterministically."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="sha_shif",
            tax_year=tax_year,
            rule_version="v1",
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
        )
    )

    assert bound_rule.binding_id == expected_binding_id


def test_bind_rule_selection_rejects_pre_live_sha_window() -> None:
    """Verify pre-payment SHA windows remain unbound."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier="sha_shif",
                tax_year=2024,
                rule_version="v1",
                primary_effective_date=date(2024, 9, 30),
                historical_version_id="HCH-VER-20240920-PIT",
            )
        )

    assert error.value.reason == "unsupported_governed_boundary_only_window"


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "expected_binding_id"),
    [
        (
            2024,
            date(2024, 10, 1),
            "HCH-VER-20241001-A",
            "health_contribution_sha_shif_v1_2024_10_01",
        ),
        (
            2025,
            date(2025, 2, 27),
            "HCH-VER-20241001-A",
            "health_contribution_sha_shif_v1_2024_10_01",
        ),
        (
            2025,
            date(2025, 2, 28),
            "HCH-VER-20250228-PIT",
            "health_contribution_sha_shif_v1_2025_02_28_pit",
        ),
    ],
)
def test_bind_rule_selection_accepts_exact_sha_window_edges(
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_binding_id: str,
) -> None:
    """Verify exact implementation-ready SHA/SHIF cutover dates remain bindable."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="sha_shif",
            tax_year=tax_year,
            rule_version="v1",
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
        )
    )

    assert bound_rule.binding_id == expected_binding_id


@pytest.mark.parametrize(
    (
        "tax_year",
        "primary_effective_date",
        "historical_version_id",
        "expected_effective_start",
        "expected_effective_end",
        "expected_decision_refs",
    ),
    [
        (
            2024,
            "2024-10-31",
            "HCH-VER-20241001-A",
            "2024-10-01",
            "2025-02-27",
            ["HC-SHI-NPOL-2024-001"],
        ),
        (
            2025,
            "2025-03-31",
            "HCH-VER-20250228-PIT",
            "2025-02-28",
            None,
            ["HC-SHI-NPOL-2025-001"],
        ),
    ],
)
def test_execute_computation_returns_governed_sha_salaried_payload(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    expected_effective_start: str,
    expected_effective_end: str | None,
    expected_decision_refs: list[str],
) -> None:
    """Verify each implementation-ready SHA/SHIF window supports the salaried lane."""

    request = ComputationExecutionRequest.model_validate(
        _supported_salaried_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            amount_kes="40000.00",
        )
    )

    result = execute_computation(request)
    result_payload = result.result_payload
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    salaried_domain = cast(
        dict[str, object],
        cast(dict[str, object], result_payload["domain_outcomes"])["sha_shif_salaried"],
    )

    assert result.status == "ok"
    assert version_identity["historical_version_id"] == historical_version_id
    assert version_identity["effective_start"] == expected_effective_start
    assert version_identity["effective_end"] == expected_effective_end
    assert contribution_summary["regime_family"] == "sha_shif"
    assert contribution_summary["coverage_status"] == "implementation_ready"
    assert contribution_summary["summary_status"] == "computed"
    assert contribution_summary["contribution_basis_kes"] == "40000.00"
    assert contribution_summary["employee_contribution_kes"] == "1100.00"
    assert contribution_summary["employer_contribution_kes"] == "0.00"
    assert contribution_summary["household_contribution_kes"] == "0.00"
    assert contribution_summary["total_contribution_kes"] == "1100.00"
    assert contribution_summary["currency"] == "KES"
    assert salaried_domain["decision_refs"] == expected_decision_refs
    assert salaried_domain["employee_contribution_kes"] == "1100.00"
    _validate_result_payload_schema(result_payload)


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "amount_kes", "expected_total"),
    [
        (2024, "2024-10-01", "HCH-VER-20241001-A", "40000.00", "1100.00"),
        (2025, "2025-02-27", "HCH-VER-20241001-A", "10909.28", "300.01"),
        (2025, "2025-02-28", "HCH-VER-20250228-PIT", "40000.00", "1100.00"),
    ],
)
def test_execute_computation_supports_exact_sha_salaried_window_edge_dates(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    amount_kes: str,
    expected_total: str,
) -> None:
    """Verify exact supported SHA salaried edge dates execute in the governed lane."""

    request = ComputationExecutionRequest.model_validate(
        _supported_salaried_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            amount_kes=amount_kes,
        )
    )

    result = execute_computation(request)
    version_identity = cast(dict[str, object], result.result_payload["version_identity"])
    contribution_summary = cast(
        dict[str, object],
        result.result_payload["contribution_summary"],
    )

    assert version_identity["historical_version_id"] == historical_version_id
    assert contribution_summary["total_contribution_kes"] == expected_total


@pytest.mark.parametrize(
    (
        "tax_year",
        "primary_effective_date",
        "historical_version_id",
        "expected_effective_start",
        "expected_effective_end",
        "expected_decision_refs",
    ),
    [
        (
            2024,
            "2024-10-31",
            "HCH-VER-20241001-A",
            "2024-10-01",
            "2025-02-27",
            ["HC-SHI-NPOL-2024-002", "HC-SHI-NPOL-2024-003"],
        ),
        (
            2025,
            "2025-03-31",
            "HCH-VER-20250228-PIT",
            "2025-02-28",
            None,
            ["HC-SHI-NPOL-2025-002", "HC-SHI-NPOL-2025-003"],
        ),
    ],
)
def test_execute_computation_returns_governed_sha_non_salaried_payload(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    expected_effective_start: str,
    expected_effective_end: str | None,
    expected_decision_refs: list[str],
) -> None:
    """Verify each implementation-ready SHA/SHIF window supports the non-salaried lane."""

    request = ComputationExecutionRequest.model_validate(
        _supported_non_salaried_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            amount_kes="200000.00",
        )
    )

    result = execute_computation(request)
    result_payload = result.result_payload
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    non_salaried_domain = cast(
        dict[str, object],
        cast(dict[str, object], result_payload["domain_outcomes"])["sha_shif_non_salaried"],
    )

    assert version_identity["historical_version_id"] == historical_version_id
    assert version_identity["effective_start"] == expected_effective_start
    assert version_identity["effective_end"] == expected_effective_end
    assert contribution_summary["household_contribution_kes"] == "5500.00"
    assert contribution_summary["total_contribution_kes"] == "5500.00"
    assert non_salaried_domain["decision_refs"] == expected_decision_refs
    assert non_salaried_domain["household_contribution_kes"] == "5500.00"
    _validate_result_payload_schema(result_payload)


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "amount_kes", "expected_total"),
    [
        (2024, "2024-10-01", "HCH-VER-20241001-A", "130909.09", "3600.00"),
        (2025, "2025-02-27", "HCH-VER-20241001-A", "200000.00", "5500.00"),
        (2025, "2025-02-28", "HCH-VER-20250228-PIT", "130909.28", "3600.01"),
    ],
)
def test_execute_computation_supports_exact_sha_non_salaried_window_edge_dates(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    amount_kes: str,
    expected_total: str,
) -> None:
    """Verify exact supported SHA non-salaried edge dates execute in the governed lane."""

    request = ComputationExecutionRequest.model_validate(
        _supported_non_salaried_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            amount_kes=amount_kes,
        )
    )

    result = execute_computation(request)
    version_identity = cast(dict[str, object], result.result_payload["version_identity"])
    contribution_summary = cast(
        dict[str, object],
        result.result_payload["contribution_summary"],
    )

    assert version_identity["historical_version_id"] == historical_version_id
    assert contribution_summary["total_contribution_kes"] == expected_total


@pytest.mark.parametrize(
    ("amount_kes", "expected_total"),
    [("10909.09", "300.00"), ("10909.28", "300.01")],
)
def test_execute_computation_applies_exact_sha_salaried_floor_threshold_edges(
    amount_kes: str,
    expected_total: str,
) -> None:
    """Verify salaried SHA floor and post-floor rounding edges stay exact."""

    request = ComputationExecutionRequest.model_validate(
        _supported_salaried_request_payload(
            tax_year=2025,
            primary_effective_date="2025-03-31",
            historical_version_id="HCH-VER-20250228-PIT",
            amount_kes=amount_kes,
        )
    )

    result = execute_computation(request)
    contribution_summary = cast(
        dict[str, object],
        result.result_payload["contribution_summary"],
    )

    assert contribution_summary["employee_contribution_kes"] == expected_total
    assert contribution_summary["total_contribution_kes"] == expected_total


@pytest.mark.parametrize(
    ("amount_kes", "expected_total"),
    [("130909.09", "3600.00"), ("130909.28", "3600.01")],
)
def test_execute_computation_applies_exact_sha_non_salaried_floor_threshold_edges(
    amount_kes: str,
    expected_total: str,
) -> None:
    """Verify non-salaried SHA floor and post-floor rounding edges stay exact."""

    request = ComputationExecutionRequest.model_validate(
        _supported_non_salaried_request_payload(
            tax_year=2025,
            primary_effective_date="2025-03-31",
            historical_version_id="HCH-VER-20250228-PIT",
            amount_kes=amount_kes,
        )
    )

    result = execute_computation(request)
    contribution_summary = cast(
        dict[str, object],
        result.result_payload["contribution_summary"],
    )

    assert contribution_summary["household_contribution_kes"] == expected_total
    assert contribution_summary["total_contribution_kes"] == expected_total


def test_execute_computation_applies_non_salaried_annual_floor() -> None:
    """Verify the annualized SHA non-salaried floor is applied deterministically."""

    request = ComputationExecutionRequest.model_validate(
        _supported_non_salaried_request_payload(
            tax_year=2025,
            primary_effective_date="2025-03-31",
            historical_version_id="HCH-VER-20250228-PIT",
            amount_kes="100000.00",
        )
    )

    result = execute_computation(request)
    contribution_summary = cast(dict[str, object], result.result_payload["contribution_summary"])

    assert contribution_summary["household_contribution_kes"] == "3600.00"
    assert contribution_summary["total_contribution_kes"] == "3600.00"


def test_execute_computation_rejects_missing_means_testing_completion() -> None:
    """Verify non-salaried requests fail closed without completed means-testing evidence."""

    request_payload = _supported_non_salaried_request_payload(
        tax_year=2025,
        primary_effective_date="2025-03-31",
        historical_version_id="HCH-VER-20250228-PIT",
        amount_kes="200000.00",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    non_salaried_inputs = cast(dict[str, object], input_payload["sha_shif_non_salaried_inputs"])
    non_salaried_inputs["means_testing_assertions"] = [
        {
            "assertion_type": "means_testing_required",
            "assertion_status": "asserted",
            "source_reference_id": "EVI-SHA-HOUSE-001",
        }
    ]

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "missing_means_testing_completion"


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id"),
    [
        (2024, "2024-09-30", "HCH-VER-20240920-PIT"),
        (2025, "2025-02-28", "HCH-VER-20250228-AMD"),
    ],
)
def test_bind_rule_selection_rejects_sha_dates_just_outside_supported_windows(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
) -> None:
    """Verify dates immediately outside supported SHA windows stay fail-closed."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier="sha_shif",
                tax_year=tax_year,
                rule_version="v1",
                primary_effective_date=date.fromisoformat(primary_effective_date),
                historical_version_id=historical_version_id,
            )
        )

    assert error.value.reason == "unsupported_governed_boundary_only_window"


def test_execute_computation_rejects_malformed_sha_lane_shape() -> None:
    """Verify malformed SHA governed sections fail closed without lane normalization."""

    request_payload = _supported_salaried_request_payload(
        tax_year=2024,
        primary_effective_date="2024-10-31",
        historical_version_id="HCH-VER-20241001-A",
        amount_kes="40000.00",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    salaried_inputs = cast(dict[str, object], input_payload["sha_shif_salaried_inputs"])
    salaried_inputs["unexpected_governed_field"] = True

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_sha_shif_request_shape"
    assert error.value.path == "$.input_payload"


def test_execution_endpoint_rejects_nhif_inputs_for_sha_request() -> None:
    """Verify endpoint maps wrong-lane SHA rejection to shared deterministic envelope."""

    client = _build_rule_pack_test_client()
    payload = _supported_salaried_request_payload(
        tax_year=2024,
        primary_effective_date="2024-10-31",
        historical_version_id="HCH-VER-20241001-A",
        amount_kes="40000.00",
    )
    input_payload = cast(dict[str, object], payload["input_payload"])
    nhif_inputs = cast(dict[str, object], input_payload["nhif_legacy_inputs"])
    nhif_inputs["deduction_reference_ids"] = ["DED-NHIF-UNSUPPORTED-001"]

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
    assert details["reason"] == "unsupported_mixed_context_hc_mctx_cmb_0001"


def test_sha_execution_is_deterministic_for_logical_equivalent_requests() -> None:
    """Verify supported SHA requests remain deterministic under key reordering."""

    request_one = ComputationExecutionRequest.model_validate(
        _supported_salaried_request_payload(
            tax_year=2024,
            primary_effective_date="2024-10-31",
            historical_version_id="HCH-VER-20241001-A",
            amount_kes="40000.00",
        )
    )
    request_two = ComputationExecutionRequest.model_validate(
        {
            "rule_version": "v1",
            "tax_year": 2024,
            "regime_identifier": "sha_shif",
            "regime_type": "health_contribution",
            "tax_type": "health_contribution",
            "input_payload": {
                "traceability_context": {
                    "source_record_ids": ["SRC-SHA-001"],
                    "preparation_profile": "payroll_import_normalized",
                    "completeness_assertion": "complete",
                    "evidence_reference_ids": ["EVI-SHA-001"],
                },
                "operational_context": {
                    "workflow_flags": [
                        "employer_remittance_workflow_present",
                        "payment_and_access_live",
                    ],
                    "registration_status": "active",
                    "remittance_channel": "employer_payroll_remittance",
                    "reference_ids": ["OPS-SHA-001"],
                },
                "mixed_context_inputs": {"context_items": []},
                "special_case_assertions": {"assertion_items": []},
                "sha_shif_non_salaried_inputs": {
                    "household_member_reference_ids": [],
                    "means_testing_assertions": [],
                    "household_income_items": [],
                },
                "sha_shif_salaried_inputs": {
                    "remittance_reference_ids": ["SHA-REM-001"],
                    "employer_assertions": [
                        {
                            "assertion_status": "confirmed_by_evidence",
                            "assertion_type": "employer_registered",
                            "source_reference_id": "EVI-SHA-EMP-001",
                        },
                        {
                            "assertion_status": "confirmed_by_evidence",
                            "assertion_type": "remittance_path_asserted",
                            "source_reference_id": "EVI-SHA-EMP-002",
                        },
                    ],
                    "payroll_items": [
                        {
                            "event_date": "2024-10-31",
                            "amount_kes": "40000.00",
                            "income_basis_type": "gross_salary_basis",
                            "reference_id": "PAY-SHA-001",
                        }
                    ],
                },
                "nhif_legacy_inputs": {
                    "member_class_assertions": [],
                    "earning_items": [],
                    "deduction_reference_ids": [],
                },
                "contributor_context": {
                    "asserted_domain_path": "sha_shif_salaried",
                    "contributor_kind": "employee",
                    "payroll_reference_id": "PAYROLL-SHA-001",
                    "employer_reference_id": "EMPLOYER-SHA-001",
                    "contribution_subject_reference_id": "SUBJECT-SHA-001",
                },
                "version_context": {
                    "source_anchor_ids": ["HC-SHI-REG-2024-09-20"],
                    "historical_version_id": "HCH-VER-20241001-A",
                    "version_selection_basis": "payroll_period_end",
                    "primary_effective_date": "2024-10-31",
                    "governing_change_ids": ["HC-CHG-2024-10-01-A"],
                },
            },
        }
    )

    first = execute_computation(request_one).model_dump(mode="json")
    second = execute_computation(request_two).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


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


def _supported_salaried_request_payload(
    *,
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    amount_kes: str,
) -> dict[str, object]:
    return {
        "tax_type": "health_contribution",
        "regime_type": "health_contribution",
        "regime_identifier": "sha_shif",
        "tax_year": tax_year,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": primary_effective_date,
                "version_selection_basis": "payroll_period_end",
                "historical_version_id": historical_version_id,
                "governing_change_ids": [_window_change_id(historical_version_id)],
                "source_anchor_ids": [_window_source_anchor(historical_version_id)],
            },
            "contributor_context": {
                "contributor_kind": "employee",
                "asserted_domain_path": "sha_shif_salaried",
                "contribution_subject_reference_id": "SUBJECT-SHA-001",
                "employer_reference_id": "EMPLOYER-SHA-001",
                "payroll_reference_id": "PAYROLL-SHA-001",
            },
            "nhif_legacy_inputs": {
                "earning_items": [],
                "member_class_assertions": [],
                "deduction_reference_ids": [],
            },
            "sha_shif_salaried_inputs": {
                "payroll_items": [
                    {
                        "income_basis_type": "gross_salary_basis",
                        "amount_kes": amount_kes,
                        "event_date": primary_effective_date,
                        "reference_id": "PAY-SHA-001",
                    }
                ],
                "employer_assertions": [
                    {
                        "assertion_type": "employer_registered",
                        "assertion_status": "confirmed_by_evidence",
                        "source_reference_id": "EVI-SHA-EMP-001",
                    },
                    {
                        "assertion_type": "remittance_path_asserted",
                        "assertion_status": "confirmed_by_evidence",
                        "source_reference_id": "EVI-SHA-EMP-002",
                    },
                ],
                "remittance_reference_ids": ["SHA-REM-001"],
            },
            "sha_shif_non_salaried_inputs": {
                "household_income_items": [],
                "means_testing_assertions": [],
                "household_member_reference_ids": [],
            },
            "special_case_assertions": {"assertion_items": []},
            "mixed_context_inputs": {"context_items": []},
            "operational_context": {
                "workflow_flags": [
                    "employer_remittance_workflow_present",
                    "payment_and_access_live",
                ],
                "registration_status": "active",
                "remittance_channel": "employer_payroll_remittance",
                "reference_ids": ["OPS-SHA-001"],
            },
            "traceability_context": {
                "source_record_ids": ["SRC-SHA-001"],
                "preparation_profile": "payroll_import_normalized",
                "completeness_assertion": "complete",
                "evidence_reference_ids": ["EVI-SHA-001"],
            },
        },
    }


def _supported_non_salaried_request_payload(
    *,
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    amount_kes: str,
) -> dict[str, object]:
    return {
        "tax_type": "health_contribution",
        "regime_type": "health_contribution",
        "regime_identifier": "sha_shif",
        "tax_year": tax_year,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": primary_effective_date,
                "version_selection_basis": "household_income_reference_date",
                "historical_version_id": historical_version_id,
                "governing_change_ids": [_window_change_id(historical_version_id)],
                "source_anchor_ids": [_window_source_anchor(historical_version_id)],
            },
            "contributor_context": {
                "contributor_kind": "household",
                "asserted_domain_path": "sha_shif_non_salaried",
                "contribution_subject_reference_id": "SUBJECT-SHA-HOUSE-001",
                "household_reference_id": "HOUSEHOLD-SHA-001",
            },
            "nhif_legacy_inputs": {
                "earning_items": [],
                "member_class_assertions": [],
                "deduction_reference_ids": [],
            },
            "sha_shif_salaried_inputs": {
                "payroll_items": [],
                "employer_assertions": [],
                "remittance_reference_ids": [],
            },
            "sha_shif_non_salaried_inputs": {
                "household_income_items": [
                    {
                        "income_basis_type": "annual_household_income",
                        "amount_kes": amount_kes,
                        "event_date": primary_effective_date,
                        "reference_id": "HOUSE-INCOME-001",
                    }
                ],
                "means_testing_assertions": [
                    {
                        "assertion_type": "means_testing_completed",
                        "assertion_status": "confirmed_by_evidence",
                        "source_reference_id": "EVI-SHA-HOUSE-001",
                    }
                ],
                "household_member_reference_ids": ["HOUSE-MEMBER-001"],
            },
            "special_case_assertions": {"assertion_items": []},
            "mixed_context_inputs": {"context_items": []},
            "operational_context": {
                "workflow_flags": ["payment_and_access_live"],
                "registration_status": "active",
                "remittance_channel": "household_self_service",
                "reference_ids": ["OPS-SHA-HOUSE-001"],
            },
            "traceability_context": {
                "source_record_ids": ["SRC-SHA-HOUSE-001"],
                "preparation_profile": "household_assessment_normalized",
                "completeness_assertion": "complete",
                "evidence_reference_ids": ["EVI-SHA-HOUSE-002"],
            },
        },
    }


def _window_change_id(historical_version_id: str) -> str:
    mapping = {
        "HCH-VER-20241001-A": "HC-CHG-2024-10-01-A",
        "HCH-VER-20250228-PIT": "HC-CHG-2025-02-28-B",
    }
    return mapping[historical_version_id]


def _window_source_anchor(historical_version_id: str) -> str:
    mapping = {
        "HCH-VER-20241001-A": "HC-SHI-REG-2024-09-20",
        "HCH-VER-20250228-PIT": "HC-SHI-REG-2025-02-28",
    }
    return mapping[historical_version_id]


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


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _validate_result_payload_schema(payload: dict[str, object]) -> None:
    schema = cast(dict[str, object], json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8")))
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    assert errors == []
