"""Test governed NHIF legacy health-contribution rule-pack behavior."""

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

TEST_PRINCIPAL_ID = UUID("45454545-4545-4545-4545-454545454545")
TEST_COMPUTATION_ID = UUID("56565656-5656-5656-5656-565656565656")
TEST_AUDIT_EVENT_ID = UUID("67676767-6767-6767-6767-676767676767")
TEST_IDEMPOTENCY_KEY = "idem-health-nhif"
TEST_CORRELATION_ID = "corr-health-nhif"
RESULT_SCHEMA_PATH = Path("contracts/tools/schemas/health_contribution_result_payload.schema.json")


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "expected_binding_id"),
    [
        (
            2012,
            date(2012, 1, 31),
            "HCH-VER-20100716-A",
            "health_contribution_nhif_legacy_v1_2010_07_16",
        ),
        (
            2019,
            date(2019, 7, 31),
            "HCH-VER-20150401-A",
            "health_contribution_nhif_legacy_v1_2015_04_01",
        ),
        (
            2022,
            date(2022, 6, 30),
            "HCH-VER-20210528-A",
            "health_contribution_nhif_legacy_v1_2021_05_28",
        ),
        (
            2023,
            date(2023, 5, 31),
            "HCH-VER-20221231-REG",
            "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        ),
    ],
)
def test_bind_rule_selection_resolves_supported_nhif_windows(
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_binding_id: str,
) -> None:
    """Verify each implementation-ready NHIF window binds deterministically."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="nhif_legacy",
            tax_year=tax_year,
            rule_version="v1",
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
        )
    )

    assert bound_rule.binding_id == expected_binding_id


def test_bind_rule_selection_rejects_pre_2010_nhif_window() -> None:
    """Verify unresolved pre-2010 NHIF window remains unbound."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier="nhif_legacy",
                tax_year=2009,
                rule_version="v1",
                primary_effective_date=date(2009, 12, 31),
                historical_version_id="HCH-VER-20031205-A",
            )
        )

    assert error.value.reason == "unsupported_partially_specified_window"


@pytest.mark.parametrize(
    (
        "tax_year",
        "primary_effective_date",
        "historical_version_id",
        "expected_binding_id",
    ),
    [
        (
            2010,
            date(2010, 7, 16),
            "HCH-VER-20100716-A",
            "health_contribution_nhif_legacy_v1_2010_07_16",
        ),
        (
            2014,
            date(2014, 12, 7),
            "HCH-VER-20100716-A",
            "health_contribution_nhif_legacy_v1_2010_07_16",
        ),
        (
            2015,
            date(2015, 4, 1),
            "HCH-VER-20150401-A",
            "health_contribution_nhif_legacy_v1_2015_04_01",
        ),
        (
            2021,
            date(2021, 3, 29),
            "HCH-VER-20150401-A",
            "health_contribution_nhif_legacy_v1_2015_04_01",
        ),
        (
            2021,
            date(2021, 5, 28),
            "HCH-VER-20210528-A",
            "health_contribution_nhif_legacy_v1_2021_05_28",
        ),
        (
            2022,
            date(2022, 12, 30),
            "HCH-VER-20210528-A",
            "health_contribution_nhif_legacy_v1_2021_05_28",
        ),
        (
            2022,
            date(2022, 12, 31),
            "HCH-VER-20221231-REG",
            "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        ),
        (
            2023,
            date(2023, 11, 21),
            "HCH-VER-20221231-REG",
            "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        ),
    ],
)
def test_bind_rule_selection_accepts_exact_nhif_window_edges(
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_binding_id: str,
) -> None:
    """Verify exact implementation-ready NHIF start and end dates remain bindable."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="nhif_legacy",
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
        "expected_total_contribution_kes",
        "expected_decision_refs",
    ),
    [
        (
            2012,
            "2012-01-31",
            "HCH-VER-20100716-A",
            "2010-07-16",
            "2014-12-07",
            "1000.00",
            ["HC-NHIF-NPOL-2010-001", "HC-NHIF-NPOL-2010-002"],
        ),
        (
            2019,
            "2019-07-31",
            "HCH-VER-20150401-A",
            "2015-04-01",
            "2021-03-29",
            "1100.00",
            ["HC-NHIF-NPOL-2015-001", "HC-NHIF-NPOL-2015-002"],
        ),
        (
            2022,
            "2022-06-30",
            "HCH-VER-20210528-A",
            "2021-05-28",
            "2022-12-30",
            "1100.00",
            ["HC-NHIF-NPOL-2021-001", "HC-NHIF-NPOL-2021-002"],
        ),
        (
            2023,
            "2023-05-31",
            "HCH-VER-20221231-REG",
            "2022-12-31",
            "2023-11-21",
            "1100.00",
            ["HC-NHIF-NPOL-2022-001", "HC-NHIF-NPOL-2022-002"],
        ),
    ],
)
def test_execute_computation_returns_governed_standard_member_payload(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    expected_effective_start: str,
    expected_effective_end: str,
    expected_total_contribution_kes: str,
    expected_decision_refs: list[str],
) -> None:
    """Verify each implementation-ready NHIF window computes exact governed output."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
            amount_kes="45000.00",
        )
    )

    result = execute_computation(request)
    result_payload = result.result_payload
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    nhif_domain = cast(
        dict[str, object],
        cast(dict[str, object], result_payload["domain_outcomes"])["nhif_legacy"],
    )

    assert result.status == "ok"
    assert version_identity["historical_version_id"] == historical_version_id
    assert version_identity["effective_start"] == expected_effective_start
    assert version_identity["effective_end"] == expected_effective_end
    assert contribution_summary["regime_family"] == "nhif_legacy"
    assert contribution_summary["coverage_status"] == "implementation_ready"
    assert contribution_summary["summary_status"] == "computed"
    assert contribution_summary["contribution_basis_kes"] == "45000.00"
    assert contribution_summary["employee_contribution_kes"] == expected_total_contribution_kes
    assert contribution_summary["employer_contribution_kes"] == "0.00"
    assert contribution_summary["household_contribution_kes"] == "0.00"
    assert contribution_summary["total_contribution_kes"] == expected_total_contribution_kes
    assert contribution_summary["currency"] == "KES"
    assert nhif_domain["decision_refs"] == expected_decision_refs
    _validate_result_payload_schema(result_payload)


@pytest.mark.parametrize(
    (
        "tax_year",
        "primary_effective_date",
        "historical_version_id",
        "amount_kes",
        "expected_total_contribution_kes",
    ),
    [
        (2010, "2010-07-16", "HCH-VER-20100716-A", "6000.00", "300.00"),
        (2014, "2014-12-07", "HCH-VER-20100716-A", "49999.00", "1000.00"),
        (2015, "2015-04-01", "HCH-VER-20150401-A", "45000.00", "1100.00"),
        (2021, "2021-03-29", "HCH-VER-20150401-A", "100000.00", "1700.00"),
        (2021, "2021-05-28", "HCH-VER-20210528-A", "44999.00", "1000.00"),
        (2022, "2022-12-30", "HCH-VER-20210528-A", "45000.00", "1100.00"),
        (2022, "2022-12-31", "HCH-VER-20221231-REG", "44999.00", "1000.00"),
        (2023, "2023-11-21", "HCH-VER-20221231-REG", "45000.00", "1100.00"),
    ],
)
def test_execute_computation_supports_exact_nhif_window_edge_dates(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    amount_kes: str,
    expected_total_contribution_kes: str,
) -> None:
    """Verify exact implementation-ready NHIF edge dates execute with governed output."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
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
    assert contribution_summary["total_contribution_kes"] == expected_total_contribution_kes


@pytest.mark.parametrize(
    ("historical_version_id", "primary_effective_date", "amount_kes", "expected_total"),
    [
        ("HCH-VER-20100716-A", "2012-01-31", "6000.00", "300.00"),
        ("HCH-VER-20100716-A", "2012-01-31", "7999.00", "300.00"),
        ("HCH-VER-20100716-A", "2012-01-31", "8000.00", "400.00"),
        ("HCH-VER-20221231-REG", "2023-05-31", "44999.00", "1000.00"),
        ("HCH-VER-20221231-REG", "2023-05-31", "45000.00", "1100.00"),
        ("HCH-VER-20221231-REG", "2023-05-31", "100000.00", "1700.00"),
    ],
)
def test_execute_computation_applies_exact_nhif_threshold_edges(
    historical_version_id: str,
    primary_effective_date: str,
    amount_kes: str,
    expected_total: str,
) -> None:
    """Verify governed NHIF threshold edges compute the exact published band amount."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=int(primary_effective_date[:4]),
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
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


def test_execute_computation_returns_governed_special_member_payload() -> None:
    """Verify supported NHIF special-member request computes fixed governed output."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=2022,
            primary_effective_date="2022-06-30",
            historical_version_id="HCH-VER-20210528-A",
            contributor_kind="self_employed",
            member_class="special_member",
            income_basis_type="special_contributor_basis",
            amount_kes="500.00",
        )
    )

    result = execute_computation(request)
    contribution_summary = cast(
        dict[str, object],
        result.result_payload["contribution_summary"],
    )

    assert contribution_summary["contribution_basis_kes"] == "500.00"
    assert contribution_summary["employee_contribution_kes"] == "500.00"
    assert contribution_summary["total_contribution_kes"] == "500.00"


def test_execute_computation_rejects_uncovered_2015_gap_amount() -> None:
    """Verify uncovered published 2015 amount gaps fail closed."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=2019,
            primary_effective_date="2019-07-31",
            historical_version_id="HCH-VER-20150401-A",
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
            amount_kes="69001.00",
        )
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(request)

    assert error.value.reason == "unsupported_nhif_legacy_amount_gap"


def test_execute_computation_rejects_uncovered_2010_endpoint_gap() -> None:
    """Verify uncovered strict endpoints in 2010 fail closed."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=2012,
            primary_effective_date="2012-01-31",
            historical_version_id="HCH-VER-20100716-A",
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
            amount_kes="5999.00",
        )
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(request)

    assert error.value.reason == "unsupported_nhif_legacy_amount_gap"


def test_execute_computation_rejects_exact_2010_upper_endpoint_gap() -> None:
    """Verify the unresolved exact 2010 upper endpoint remains fail-closed."""

    request = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=2012,
            primary_effective_date="2012-01-31",
            historical_version_id="HCH-VER-20100716-A",
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
            amount_kes="100000.00",
        )
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(request)

    assert error.value.reason == "unsupported_nhif_legacy_amount_gap"


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "expected_reason"),
    [
        (2009, "2009-12-31", "HCH-VER-20031205-A", "unsupported_partially_specified_window"),
        (2014, "2014-12-08", "HCH-VER-20141208-A", "unsupported_governed_boundary_only_window"),
        (2021, "2021-03-30", "HCH-VER-20210330-A", "unsupported_governed_boundary_only_window"),
        (2023, "2023-11-21", "HCH-VER-20221231-ACT", "unsupported_governed_boundary_only_window"),
    ],
)
def test_bind_rule_selection_rejects_nhif_dates_just_outside_supported_windows(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    expected_reason: str,
) -> None:
    """Verify NHIF dates immediately outside supported windows fail closed deterministically."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier="nhif_legacy",
                tax_year=tax_year,
                rule_version="v1",
                primary_effective_date=date.fromisoformat(primary_effective_date),
                historical_version_id=historical_version_id,
            )
        )

    assert error.value.reason == expected_reason


def test_execute_computation_rejects_malformed_nhif_version_context_shape() -> None:
    """Verify malformed NHIF governed sections fail closed without normalization."""

    request_payload = _supported_request_payload(
        tax_year=2023,
        primary_effective_date="2023-05-31",
        historical_version_id="HCH-VER-20221231-REG",
        contributor_kind="employee",
        member_class="standard_member",
        income_basis_type="salary_band_basis",
        amount_kes="45000.00",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    version_context = cast(dict[str, object], input_payload["version_context"])
    version_context["unexpected_governed_field"] = "not_allowed"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_nhif_legacy_request_shape"
    assert error.value.path == "$.input_payload"


def test_execution_endpoint_rejects_mixed_context_health_request() -> None:
    """Verify endpoint maps mixed-context NHIF rejection to shared deterministic envelope."""

    client = _build_rule_pack_test_client()
    payload = _supported_request_payload(
        tax_year=2023,
        primary_effective_date="2023-05-31",
        historical_version_id="HCH-VER-20221231-REG",
        contributor_kind="employee",
        member_class="standard_member",
        income_basis_type="salary_band_basis",
        amount_kes="45000.00",
    )
    mixed_context_inputs = cast(
        dict[str, object],
        cast(dict[str, object], payload["input_payload"])["mixed_context_inputs"],
    )
    mixed_context_inputs["context_items"] = [
        {
            "mixed_context_type": "legacy_and_active_overlap",
            "affected_domain_ids": ["HCD-CORE-NHIF-LEGACY", "HCD-TRANS-REGIME-SELECTION"],
            "reference_id": "MIX-001",
        }
    ]

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


def test_nhif_execution_is_deterministic_for_logical_equivalent_requests() -> None:
    """Verify supported NHIF requests remain deterministic under key reordering."""

    request_one = ComputationExecutionRequest.model_validate(
        _supported_request_payload(
            tax_year=2023,
            primary_effective_date="2023-05-31",
            historical_version_id="HCH-VER-20221231-REG",
            contributor_kind="employee",
            member_class="standard_member",
            income_basis_type="salary_band_basis",
            amount_kes="45000.00",
        )
    )
    request_two = ComputationExecutionRequest.model_validate(
        {
            "rule_version": "v1",
            "tax_year": 2023,
            "regime_identifier": "nhif_legacy",
            "regime_type": "health_contribution",
            "tax_type": "health_contribution",
            "input_payload": {
                "traceability_context": {
                    "source_record_ids": ["SRC-NHIF-001"],
                    "preparation_profile": "manual_structured_entry",
                    "completeness_assertion": "complete",
                    "evidence_reference_ids": [],
                },
                "operational_context": {
                    "workflow_flags": ["employer_remittance_workflow_present"],
                    "registration_status": "active",
                    "remittance_channel": "employer_payroll_remittance",
                    "reference_ids": ["OPS-NHIF-001"],
                },
                "mixed_context_inputs": {"context_items": []},
                "special_case_assertions": {"assertion_items": []},
                "sha_shif_non_salaried_inputs": {
                    "household_member_reference_ids": [],
                    "means_testing_assertions": [],
                    "household_income_items": [],
                },
                "sha_shif_salaried_inputs": {
                    "remittance_reference_ids": [],
                    "employer_assertions": [],
                    "payroll_items": [],
                },
                "nhif_legacy_inputs": {
                    "member_class_assertions": [
                        {
                            "assertion_status": "confirmed_by_evidence",
                            "assertion_type": "standard_member",
                            "source_reference_id": "EVI-NHIF-001",
                        }
                    ],
                    "earning_items": [
                        {
                            "event_date": "2023-05-31",
                            "amount_kes": "45000.00",
                            "income_basis_type": "salary_band_basis",
                            "reference_id": "PAY-NHIF-001",
                        }
                    ],
                    "deduction_reference_ids": ["DED-NHIF-001"],
                },
                "contributor_context": {
                    "asserted_domain_path": "nhif_legacy",
                    "contributor_kind": "employee",
                    "payroll_reference_id": "PAYROLL-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "contribution_subject_reference_id": "SUBJECT-001",
                },
                "version_context": {
                    "source_anchor_ids": ["HC-NHIF-CONTRIB-REG-2022-12-31"],
                    "historical_version_id": "HCH-VER-20221231-REG",
                    "version_selection_basis": "payroll_period_end",
                    "primary_effective_date": "2023-05-31",
                    "governing_change_ids": ["HC-CHG-2022-12-31-B"],
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


def _supported_request_payload(
    *,
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
    contributor_kind: str,
    member_class: str,
    income_basis_type: str,
    amount_kes: str,
) -> dict[str, object]:
    return {
        "tax_type": "health_contribution",
        "regime_type": "health_contribution",
        "regime_identifier": "nhif_legacy",
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
                "contributor_kind": contributor_kind,
                "asserted_domain_path": "nhif_legacy",
                "contribution_subject_reference_id": "SUBJECT-001",
                "employer_reference_id": "EMPLOYER-001",
                "payroll_reference_id": "PAYROLL-001",
            },
            "nhif_legacy_inputs": {
                "earning_items": [
                    {
                        "income_basis_type": income_basis_type,
                        "amount_kes": amount_kes,
                        "event_date": primary_effective_date,
                        "reference_id": "PAY-NHIF-001",
                    }
                ],
                "member_class_assertions": [
                    {
                        "assertion_type": member_class,
                        "assertion_status": "confirmed_by_evidence",
                        "source_reference_id": "EVI-NHIF-001",
                    }
                ],
                "deduction_reference_ids": ["DED-NHIF-001"],
            },
            "sha_shif_salaried_inputs": {
                "payroll_items": [],
                "employer_assertions": [],
                "remittance_reference_ids": [],
            },
            "sha_shif_non_salaried_inputs": {
                "household_income_items": [],
                "means_testing_assertions": [],
                "household_member_reference_ids": [],
            },
            "special_case_assertions": {"assertion_items": []},
            "mixed_context_inputs": {"context_items": []},
            "operational_context": {
                "workflow_flags": ["employer_remittance_workflow_present"],
                "registration_status": "active",
                "remittance_channel": "employer_payroll_remittance",
                "reference_ids": ["OPS-NHIF-001"],
            },
            "traceability_context": {
                "source_record_ids": ["SRC-NHIF-001"],
                "preparation_profile": "manual_structured_entry",
                "completeness_assertion": "complete",
                "evidence_reference_ids": [],
            },
        },
    }


def _window_change_id(historical_version_id: str) -> str:
    mapping = {
        "HCH-VER-20100716-A": "HC-CHG-2010-07-16-A",
        "HCH-VER-20150401-A": "HC-CHG-2015-04-01-A",
        "HCH-VER-20210528-A": "HC-CHG-2021-05-28-A",
        "HCH-VER-20221231-REG": "HC-CHG-2022-12-31-B",
    }
    return mapping[historical_version_id]


def _window_source_anchor(historical_version_id: str) -> str:
    mapping = {
        "HCH-VER-20100716-A": "HC-NHIF-CONTRIB-REG-2010-07-16",
        "HCH-VER-20150401-A": "HC-NHIF-CONTRIB-REG-2015-04-01",
        "HCH-VER-20210528-A": "HC-NHIF-CONTRIB-REG-2021-05-28",
        "HCH-VER-20221231-REG": "HC-NHIF-CONTRIB-REG-2022-12-31",
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
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))  # type: ignore
    assert errors == []
