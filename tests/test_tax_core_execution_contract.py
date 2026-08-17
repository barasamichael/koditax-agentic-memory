"""Test deterministic tax-core execution contract and endpoint behavior."""

from __future__ import annotations

import copy
import json
from uuid import UUID
from typing import Any
from typing import cast
from typing import TypedDict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from services.tax_core.app.engine.replay import ReplayVerificationError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.executor import execute_prepared_input
from services.tax_core.app.engine.executor import prepare_execution_input
from services.tax_core.app.engine.execution_contract import BoundRule
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ValidationFinding
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import PreparedExecutionInput
from services.tax_core.app.engine.execution_contract import ReplayVerificationResult
from services.tax_core.app.engine.execution_contract import PersistedValidationSource
from services.tax_core.app.engine.execution_contract import ReplayVerificationRequest
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationValidationResult
from services.tax_core.app.engine.execution_contract import ComputationValidationRequest
from services.tax_core.app.engine.execution_contract import MaterializedComputationExecutionResult
from services.tax_core.app.rules.health_contribution.validation_catalog import (
    derive_health_contribution_validation_findings,
)

TEST_PRINCIPAL_ID = UUID("11111111-1111-1111-1111-111111111111")
TEST_COMPUTATION_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_AUDIT_EVENT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TEST_IDEMPOTENCY_KEY = "idem-tax-core-contract"
TEST_CORRELATION_ID = "corr-tax-core-contract"
_GOLDEN_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "eval" / "golden" / "tax_core"
_HEALTH_SUPPORTED_GOLDEN_FIXTURE_NAMES = (
    "health_contribution_nhif_legacy_2010_case_001.json",
    "health_contribution_nhif_legacy_2015_case_001.json",
    "health_contribution_nhif_legacy_2021_case_001.json",
    "health_contribution_nhif_legacy_case_001.json",
    "health_contribution_sha_shif_case_001.json",
    "health_contribution_sha_shif_2024_non_salaried_case_001.json",
    "health_contribution_sha_shif_2025_salaried_case_001.json",
    "health_contribution_transition_boundary_nhif_case_001.json",
    "health_contribution_transition_boundary_sha_case_001.json",
)


class ExecutionResponseBody(TypedDict):
    """Represent /computations/execute success payload."""

    status: str
    computation_id: str
    computation_result_id: str
    audit_event_id: str
    idempotency_key: str
    correlation_id: str
    tax_type: str
    regime_type: str
    tax_year: int
    rule_version: str
    input_hash: str
    result_payload: dict[str, object]


class ErrorEnvelopeBody(TypedDict):
    """Represent shared error envelope payload."""

    error_code: str
    message: str
    correlation_id: str
    details: dict[str, object]


class ErrorResponseBody(TypedDict):
    """Represent endpoint error response payload."""

    detail: ErrorEnvelopeBody


def test_prepare_execution_input_canonicalizes_payload_without_mutation() -> None:
    """Verify preparation canonicalizes nested mappings and does not mutate input."""

    input_payload: dict[str, object] = {
        "z": {"b": 2, "a": 1},
        "a": [{"d": 4, "c": 3}, {"x": 10, "w": 9}],
    }
    original_snapshot = copy.deepcopy(input_payload)
    request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload=input_payload,
    )

    prepared_input = prepare_execution_input(request)

    assert input_payload == original_snapshot
    assert prepared_input.canonical_input_payload == {
        "a": [{"c": 3, "d": 4}, {"w": 9, "x": 10}],
        "z": {"a": 1, "b": 2},
    }


def test_execute_prepared_input_uses_injected_rule_executor() -> None:
    """Verify prepared-input execution is isolated and injectable."""

    prepared_input = PreparedExecutionInput(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        canonical_input_payload={"alpha": 1},
        canonical_input_json='{"alpha":1}',
        input_hash="a" * 64,
    )
    bound_rule = BoundRule(
        binding_id="income_tax_default_v1_2025",
        selection_key=RuleSelectionKey(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2025,
            rule_version="v1",
        ),
    )
    seen_prepared_inputs: list[PreparedExecutionInput] = []

    def fake_rule_executor(
        prepared_input: PreparedExecutionInput,
        bound_rule: BoundRule,
    ) -> dict[str, object]:
        assert bound_rule.binding_id == "income_tax_default_v1_2025"
        seen_prepared_inputs.append(prepared_input)
        return {"z": 2, "a": 1}

    result = execute_prepared_input(
        prepared_input=prepared_input,
        bound_rule=bound_rule,
        rule_executor=fake_rule_executor,
    )

    assert seen_prepared_inputs == [prepared_input]
    assert result.status == "ok"
    assert result.rule_version == "v1"
    assert result.input_hash == "a" * 64
    assert result.result_payload == {"a": 1, "z": 2}


def test_execute_computation_is_deterministic_for_identical_logical_inputs() -> None:
    """Verify logical-equivalent requests produce identical deterministic envelopes."""

    request_one = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"b": {"y": 2, "x": 1}, "a": [3, {"k": 9, "j": 8}]},
    )
    request_two = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"a": [3, {"j": 8, "k": 9}], "b": {"x": 1, "y": 2}},
    )

    result_one = execute_computation(request_one).model_dump(mode="json")
    result_two = execute_computation(request_two).model_dump(mode="json")

    assert _canonical_json(result_one) == _canonical_json(result_two)


def test_execution_endpoint_returns_canonical_result_envelope() -> None:
    """Verify endpoint executes request and returns canonical result envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "income_tax",
            "regime_type": "income_tax",
            "regime_identifier": None,
            "tax_year": 2025,
            "rule_version": "v1",
            "input_payload": {"b": {"y": 2, "x": 1}, "a": [3, {"k": 9, "j": 8}]},
        },
        headers=_execution_headers(),
    )
    payload = cast(ExecutionResponseBody, _response_json(response))

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["computation_id"] == str(TEST_COMPUTATION_ID)
    assert payload["computation_result_id"] == str(TEST_COMPUTATION_ID)
    assert payload["audit_event_id"] == str(TEST_AUDIT_EVENT_ID)
    assert payload["idempotency_key"] == TEST_IDEMPOTENCY_KEY
    assert payload["correlation_id"] == TEST_CORRELATION_ID
    assert payload["tax_type"] == "income_tax"
    assert payload["regime_type"] == "income_tax"
    assert payload["tax_year"] == 2025
    assert payload["rule_version"] == "v1"
    assert len(payload["input_hash"]) == 64
    assert payload["input_hash"] == payload["result_payload"]["input_hash"]
    assert payload["result_payload"]["execution_mode"] == "deterministic_stub"
    assert payload["result_payload"]["normalized_input"] == {
        "a": [3, {"j": 8, "k": 9}],
        "b": {"x": 1, "y": 2},
    }


def test_execution_endpoint_returns_canonical_health_result_envelope() -> None:
    """Verify endpoint executes governed NHIF request and returns canonical health envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "nhif_legacy",
            "tax_year": 2023,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2023-05-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20221231-REG",
                    "governing_change_ids": ["HC-CHG-2022-12-31-B"],
                    "source_anchor_ids": ["HC-NHIF-CONTRIB-REG-2022-12-31"],
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "nhif_legacy",
                    "contribution_subject_reference_id": "SUBJECT-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "payroll_reference_id": "PAYROLL-001",
                },
                "nhif_legacy_inputs": {
                    "earning_items": [
                        {
                            "income_basis_type": "salary_band_basis",
                            "amount_kes": "45000.00",
                            "event_date": "2023-05-31",
                            "reference_id": "PAY-NHIF-001",
                        }
                    ],
                    "member_class_assertions": [
                        {
                            "assertion_type": "standard_member",
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ExecutionResponseBody, _response_json(response))
    result_payload = payload["result_payload"]
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])

    assert response.status_code == 200
    assert payload["tax_type"] == "health_contribution"
    assert payload["regime_type"] == "health_contribution"
    assert payload["tax_year"] == 2023
    assert payload["rule_version"] == "v1"
    assert payload["input_hash"] == result_payload["traceability"]["input_hash"]
    assert version_identity["historical_version_id"] == "HCH-VER-20221231-REG"
    assert contribution_summary["total_contribution_kes"] == "1100.00"
    assert contribution_summary["coverage_status"] == "implementation_ready"


def test_execution_endpoint_returns_canonical_sha_health_result_envelope() -> None:
    """Verify endpoint executes governed SHA request and returns canonical health envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "sha_shif",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-10-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20241001-A",
                    "governing_change_ids": ["HC-CHG-2024-10-01-A"],
                    "source_anchor_ids": ["HC-SHI-REG-2024-09-20"],
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
                            "amount_kes": "40000.00",
                            "event_date": "2024-10-31",
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ExecutionResponseBody, _response_json(response))
    result_payload = payload["result_payload"]
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])

    assert response.status_code == 200
    assert payload["tax_type"] == "health_contribution"
    assert payload["regime_type"] == "health_contribution"
    assert payload["tax_year"] == 2024
    assert payload["rule_version"] == "v1"
    assert payload["input_hash"] == result_payload["traceability"]["input_hash"]
    assert version_identity["historical_version_id"] == "HCH-VER-20241001-A"
    assert contribution_summary["total_contribution_kes"] == "1100.00"
    assert contribution_summary["coverage_status"] == "implementation_ready"


def test_execution_endpoint_returns_canonical_transition_selected_health_envelope() -> None:
    """Verify transition-boundary request resolves to one governed health envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "transition_boundary",
            "tax_year": 2023,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2023-05-31",
                    "version_selection_basis": "payroll_period_end",
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "transition_boundary",
                    "contribution_subject_reference_id": "SUBJECT-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "payroll_reference_id": "PAYROLL-001",
                },
                "nhif_legacy_inputs": {
                    "earning_items": [
                        {
                            "income_basis_type": "salary_band_basis",
                            "amount_kes": "45000.00",
                            "event_date": "2023-05-31",
                            "reference_id": "PAY-NHIF-001",
                        }
                    ],
                    "member_class_assertions": [
                        {
                            "assertion_type": "standard_member",
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ExecutionResponseBody, _response_json(response))
    result_payload = payload["result_payload"]
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])

    assert response.status_code == 200
    assert version_identity["regime_identifier"] == "nhif_legacy"
    assert version_identity["historical_version_id"] == "HCH-VER-20221231-REG"
    assert contribution_summary["total_contribution_kes"] == "1100.00"


@pytest.mark.parametrize("fixture_name", _HEALTH_SUPPORTED_GOLDEN_FIXTURE_NAMES)
def test_execution_endpoint_matches_supported_health_golden_fixture_result_payload(
    fixture_name: str,
) -> None:
    """Verify endpoint envelopes preserve the governed health golden payloads."""

    fixture_payload = _load_golden_fixture_payload(fixture_name)
    request_payload = cast(dict[str, object], copy.deepcopy(fixture_payload["request"]))
    expected_output = cast(dict[str, object], fixture_payload["expected_output"])
    client = _build_contract_test_client()

    response = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )
    payload = cast(ExecutionResponseBody, _response_json(response))
    result_payload = cast(dict[str, object], payload["result_payload"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["computation_id"] == str(TEST_COMPUTATION_ID)
    assert payload["computation_result_id"] == str(TEST_COMPUTATION_ID)
    assert payload["audit_event_id"] == str(TEST_AUDIT_EVENT_ID)
    assert payload["idempotency_key"] == TEST_IDEMPOTENCY_KEY
    assert payload["correlation_id"] == TEST_CORRELATION_ID
    assert payload["tax_type"] == request_payload["tax_type"]
    assert payload["regime_type"] == request_payload["regime_type"]
    assert payload["tax_year"] == request_payload["tax_year"]
    assert payload["rule_version"] == request_payload["rule_version"]
    assert payload["input_hash"] == expected_output["input_hash"]
    assert payload["input_hash"] == result_payload["traceability"]["input_hash"]
    assert payload["result_payload"] == expected_output["result_payload"]


def test_execution_endpoint_matches_2003_health_rejection_golden_fixture() -> None:
    """Verify the unresolved 2003 health window stays locked to the golden rejection."""

    fixture_payload = _load_golden_fixture_payload(
        "health_contribution_historical_rejection_2003_case_001.json"
    )
    request_payload = cast(dict[str, object], copy.deepcopy(fixture_payload["request"]))
    expected_output = cast(dict[str, object], fixture_payload["expected_output"])
    client = _build_contract_test_client()

    response = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )
    actual_output = {
        "status_code": response.status_code,
        "response_json": _response_json(response),
    }

    assert _canonical_json(actual_output) == _canonical_json(expected_output)


def test_replay_endpoint_returns_canonical_health_replay_envelope() -> None:
    """Verify /computations/replay preserves the canonical health replay response shape."""

    fixture_payload = _load_golden_fixture_payload(
        "health_contribution_transition_boundary_sha_case_001.json"
    )
    request_payload = cast(dict[str, object], fixture_payload["request"])
    expected_output = cast(dict[str, object], fixture_payload["expected_output"])
    expected_result_payload = cast(dict[str, object], expected_output["result_payload"])
    expected_version_identity = cast(
        dict[str, object],
        expected_result_payload["version_identity"],
    )
    expected_traceability = cast(dict[str, object], expected_result_payload["traceability"])

    def fake_replay_verifier(
        replay_request: ReplayVerificationRequest,
        _replay_context: object,
    ) -> ReplayVerificationResult:
        assert replay_request.computation_id == TEST_COMPUTATION_ID
        return ReplayVerificationResult(
            status="ok",
            verification_status="matched",
            computation_id=TEST_COMPUTATION_ID,
            replay_audit_event_id=TEST_AUDIT_EVENT_ID,
            correlation_id=TEST_CORRELATION_ID,
            idempotency_key=TEST_IDEMPOTENCY_KEY,
            tax_type=cast(str, request_payload["tax_type"]),
            regime_type=cast(str, request_payload["regime_type"]),
            tax_year=cast(int, request_payload["tax_year"]),
            rule_version=cast(str, request_payload["rule_version"]),
            input_hash=cast(str, expected_output["input_hash"]),
        )

    app = create_app()
    app.state.replay_verifier = fake_replay_verifier
    client = TestClient(app)

    response = client.post(
        "/computations/replay",
        json={"computation_id": str(TEST_COMPUTATION_ID)},
        headers=_execution_headers(),
    )
    payload = cast(dict[str, object], _response_json(response))

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["verification_status"] == "matched"
    assert payload["computation_id"] == str(TEST_COMPUTATION_ID)
    assert payload["replay_audit_event_id"] == str(TEST_AUDIT_EVENT_ID)
    assert payload["correlation_id"] == TEST_CORRELATION_ID
    assert payload["idempotency_key"] == TEST_IDEMPOTENCY_KEY
    assert payload["tax_type"] == "health_contribution"
    assert payload["regime_type"] == "health_contribution"
    assert payload["tax_year"] == request_payload["tax_year"]
    assert payload["rule_version"] == request_payload["rule_version"]
    assert payload["input_hash"] == expected_traceability["input_hash"]
    assert expected_version_identity["historical_version_id"] == "HCH-VER-20241001-A"
    assert expected_version_identity["regime_identifier"] == "sha_shif"


def test_replay_endpoint_rejects_non_ready_health_history_through_canonical_error() -> None:
    """Verify replay endpoint fail-closes non-ready health history through the shared envelope."""

    def fake_replay_verifier(
        replay_request: ReplayVerificationRequest,
        _replay_context: object,
    ) -> ReplayVerificationResult:
        assert replay_request.computation_id == TEST_COMPUTATION_ID
        raise ReplayVerificationError(
            reason="unsupported_partially_specified_window",
            message=(
                "HCH-VER-20031205-A remains partially_specified and is outside the "
                "implementation-ready health-contribution runtime set."
            ),
            status_code=409,
            details={
                "computation_id": str(TEST_COMPUTATION_ID),
                "selection_key": {
                    "tax_type": "health_contribution",
                    "regime_type": "health_contribution",
                    "regime_identifier": "nhif_legacy",
                    "tax_year": 2009,
                    "rule_version": "v1",
                    "historical_version_id": "HCH-VER-20031205-A",
                },
            },
        )

    app = create_app()
    app.state.replay_verifier = fake_replay_verifier
    client = TestClient(app)

    response = client.post(
        "/computations/replay",
        json={"computation_id": str(TEST_COMPUTATION_ID)},
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])
    selection_key = cast(dict[str, object], details["selection_key"])

    assert response.status_code == 409
    assert detail["error_code"] == "unsupported_partially_specified_window"
    assert details["computation_id"] == str(TEST_COMPUTATION_ID)
    assert selection_key["historical_version_id"] == "HCH-VER-20031205-A"
    assert selection_key["regime_identifier"] == "nhif_legacy"


def test_validation_endpoint_returns_canonical_health_validation_envelope() -> None:
    """Verify /computations/validate preserves a canonical health validation response shape."""

    validation_id = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    persisted_source = _build_health_persisted_validation_source(
        "health_contribution_transition_boundary_sha_case_001.json"
    )

    def fake_validator(
        validation_request: ComputationValidationRequest,
        _validation_context: object,
    ) -> ComputationValidationResult:
        assert validation_request.computation_id == persisted_source.computation_id
        findings = [
            ValidationFinding(
                code="computation_lineage_bound",
                severity="info",
                message=(
                    "Validation findings are bound to persisted deterministic computation lineage."
                ),
                details={
                    "tax_type": persisted_source.tax_type,
                    "regime_type": persisted_source.regime_type,
                    "tax_year": persisted_source.tax_year,
                    "rule_version": persisted_source.rule_version,
                    "input_hash": persisted_source.input_hash,
                },
            )
        ]
        findings.extend(derive_health_contribution_validation_findings(persisted_source))
        return ComputationValidationResult(
            status="ok",
            validation_id=validation_id,
            computation_id=persisted_source.computation_id,
            validation_context="deterministic_post_computation_validation",
            correlation_id=TEST_CORRELATION_ID,
            idempotency_key=TEST_IDEMPOTENCY_KEY,
            tax_year=persisted_source.tax_year,
            rule_version=persisted_source.rule_version,
            findings=findings,
        )

    app = create_app()
    app.state.validator = fake_validator
    client = TestClient(app)

    response = client.post(
        "/computations/validate",
        json={"computation_id": str(persisted_source.computation_id)},
        headers=_execution_headers(),
    )
    payload = cast(dict[str, object], _response_json(response))
    findings = cast(list[dict[str, object]], payload["findings"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["validation_id"] == str(validation_id)
    assert payload["computation_id"] == str(persisted_source.computation_id)
    assert payload["validation_context"] == "deterministic_post_computation_validation"
    assert payload["correlation_id"] == TEST_CORRELATION_ID
    assert payload["idempotency_key"] == TEST_IDEMPOTENCY_KEY
    assert payload["tax_year"] == persisted_source.tax_year
    assert payload["rule_version"] == persisted_source.rule_version
    assert [finding["code"] for finding in findings] == [
        "computation_lineage_bound",
        "health_contribution_supported_lane_detected",
        "health_contribution_version_binding_consistent",
        "health_contribution_effective_window_consistent",
        "health_contribution_summary_consistent",
    ]
    assert findings[1]["details"]["request_regime_identifier"] == "transition_boundary"
    assert findings[1]["details"]["resolved_regime_identifier"] == "sha_shif"
    assert findings[3]["details"]["effective_start"] == "2024-10-01"


def test_execution_endpoint_rejects_partially_specified_health_window() -> None:
    """Verify direct historical binding rejects the unresolved 2003 NHIF window explicitly."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "nhif_legacy",
            "tax_year": 2009,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2009-12-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20031205-A",
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "nhif_legacy",
                    "contribution_subject_reference_id": "SUBJECT-001",
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
                    "household_income_items": [],
                    "means_testing_assertions": [],
                    "household_member_reference_ids": [],
                },
                "special_case_assertions": {"assertion_items": []},
                "mixed_context_inputs": {"context_items": []},
                "operational_context": {
                    "workflow_flags": [],
                    "registration_status": "unresolved",
                    "remittance_channel": "not_provided",
                    "reference_ids": [],
                },
                "traceability_context": {
                    "source_record_ids": ["SRC-HCH-2003-001"],
                    "preparation_profile": "historical_reconstruction_normalized",
                    "completeness_assertion": "partial_but_governed",
                    "evidence_reference_ids": [],
                },
            },
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert details["reason"] == "unsupported_partially_specified_window"


def test_execution_endpoint_rejects_governed_boundary_only_health_window() -> None:
    """Verify direct historical binding rejects boundary-only SHA windows explicitly."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "sha_shif",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-09-30",
                    "version_selection_basis": "specific_event_date",
                    "historical_version_id": "HCH-VER-20240920-PIT",
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "sha_shif_salaried",
                    "contribution_subject_reference_id": "SUBJECT-SHA-001",
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
                    "household_income_items": [],
                    "means_testing_assertions": [],
                    "household_member_reference_ids": [],
                },
                "special_case_assertions": {"assertion_items": []},
                "mixed_context_inputs": {"context_items": []},
                "operational_context": {
                    "workflow_flags": [],
                    "registration_status": "started",
                    "remittance_channel": "not_provided",
                    "reference_ids": [],
                },
                "traceability_context": {
                    "source_record_ids": ["SRC-HCH-SHA-BOUNDARY-001"],
                    "preparation_profile": "historical_reconstruction_normalized",
                    "completeness_assertion": "partial_but_governed",
                    "evidence_reference_ids": [],
                },
            },
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert details["reason"] == "unsupported_governed_boundary_only_window"


def test_execution_endpoint_rejects_malformed_nhif_health_shape() -> None:
    """Verify malformed NHIF governed sections preserve the canonical error envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "nhif_legacy",
            "tax_year": 2023,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2023-05-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20221231-REG",
                    "governing_change_ids": ["HC-CHG-2022-12-31-B"],
                    "source_anchor_ids": ["HC-NHIF-CONTRIB-REG-2022-12-31"],
                    "unexpected_governed_field": "not_allowed",
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "nhif_legacy",
                    "contribution_subject_reference_id": "SUBJECT-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "payroll_reference_id": "PAYROLL-001",
                },
                "nhif_legacy_inputs": {
                    "earning_items": [
                        {
                            "income_basis_type": "salary_band_basis",
                            "amount_kes": "45000.00",
                            "event_date": "2023-05-31",
                            "reference_id": "PAY-NHIF-001",
                        }
                    ],
                    "member_class_assertions": [
                        {
                            "assertion_type": "standard_member",
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_nhif_legacy_request_shape"
    assert details["path"] == "$.input_payload"


def test_execution_endpoint_rejects_malformed_sha_health_shape() -> None:
    """Verify malformed SHA governed sections preserve the canonical error envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "sha_shif",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-10-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20241001-A",
                    "governing_change_ids": ["HC-CHG-2024-10-01-A"],
                    "source_anchor_ids": ["HC-SHI-REG-2024-09-20"],
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
                            "amount_kes": "40000.00",
                            "event_date": "2024-10-31",
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
                    "unexpected_governed_field": True,
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_sha_shif_request_shape"
    assert details["path"] == "$.input_payload"


def test_execution_endpoint_rejects_malformed_transition_boundary_shape() -> None:
    """Verify malformed transition sections preserve the canonical error envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "transition_boundary",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-10-31",
                    "version_selection_basis": "payroll_period_end",
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "transition_boundary",
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
                            "amount_kes": "40000.00",
                            "event_date": "2024-10-31",
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
                "mixed_context_inputs": "not_a_section",
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_transition_request_shape"
    assert details["path"] == "$.input_payload.mixed_context_inputs"


def test_execution_endpoint_returns_canonical_special_member_health_envelope() -> None:
    """Verify endpoint returns the governed NHIF special-member special-case output."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "nhif_legacy",
            "tax_year": 2022,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2022-06-30",
                    "version_selection_basis": "specific_event_date",
                    "historical_version_id": "HCH-VER-20210528-A",
                    "governing_change_ids": ["HC-CHG-2021-05-28-A"],
                    "source_anchor_ids": ["HC-NHIF-CONTRIB-REG-2021-05-28"],
                },
                "contributor_context": {
                    "contributor_kind": "self_employed",
                    "asserted_domain_path": "nhif_legacy",
                    "contribution_subject_reference_id": "SUBJECT-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "payroll_reference_id": "PAYROLL-001",
                },
                "nhif_legacy_inputs": {
                    "earning_items": [
                        {
                            "income_basis_type": "special_contributor_basis",
                            "amount_kes": "500.00",
                            "event_date": "2022-06-30",
                            "reference_id": "PAY-NHIF-001",
                        }
                    ],
                    "member_class_assertions": [
                        {
                            "assertion_type": "special_member",
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ExecutionResponseBody, _response_json(response))
    result_payload = payload["result_payload"]
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    domain_outcomes = cast(dict[str, object], result_payload["domain_outcomes"])
    exemptions_domain = cast(
        dict[str, object],
        domain_outcomes["exemptions_and_special_cases"],
    )

    assert response.status_code == 200
    assert contribution_summary["total_contribution_kes"] == "500.00"
    assert exemptions_domain["status"] == "computed"
    assert exemptions_domain["decision_refs"] == ["HC-NHIF-NPOL-0002", "HC-NHIF-NPOL-2021-001"]


def test_execution_endpoint_rejects_unresolved_health_special_case_assertion() -> None:
    """Verify endpoint maps unresolved health special-case claims to a canonical error."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "sha_shif",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-10-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20241001-A",
                    "governing_change_ids": ["HC-CHG-2024-10-01-A"],
                    "source_anchor_ids": ["HC-SHI-REG-2024-09-20"],
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
                            "amount_kes": "40000.00",
                            "event_date": "2024-10-31",
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
                "special_case_assertions": {
                    "assertion_items": [
                        {
                            "assertion_type": "special_case_pending_policy",
                            "assertion_status": "asserted",
                            "affected_domain_id": "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
                            "source_reference_id": "EVI-SPCASE-001",
                        }
                    ]
                },
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
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_special_case_assertions"
    assert details["path"] == "$.input_payload.special_case_assertions.assertion_items"


def test_execution_endpoint_rejects_explicit_mixed_context_through_canonical_envelope() -> None:
    """Verify explicit mixed_context requests bind and fail through the shared envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "mixed_context",
            "tax_year": 2025,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2025-03-31",
                    "version_selection_basis": "specific_event_date",
                },
                "contributor_context": {
                    "contributor_kind": "mixed_context",
                    "asserted_domain_path": "mixed_context",
                    "contribution_subject_reference_id": "SUBJECT-MIX-001",
                    "employer_reference_id": "EMPLOYER-MIX-001",
                    "payroll_reference_id": "PAYROLL-MIX-001",
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
                    "household_income_items": [],
                    "means_testing_assertions": [],
                    "household_member_reference_ids": [],
                },
                "special_case_assertions": {"assertion_items": []},
                "mixed_context_inputs": {
                    "context_items": [
                        {
                            "mixed_context_type": "legacy_and_active_overlap",
                            "affected_domain_ids": [
                                "HCD-CORE-NHIF-LEGACY",
                                "HCD-TRANS-REGIME-SELECTION",
                            ],
                            "reference_id": "MIX-001",
                        }
                    ]
                },
                "operational_context": {
                    "workflow_flags": [],
                    "registration_status": "unresolved",
                    "remittance_channel": "not_provided",
                    "reference_ids": ["OPS-MIX-001"],
                },
                "traceability_context": {
                    "source_record_ids": ["SRC-MIX-001"],
                    "preparation_profile": "historical_reconstruction_normalized",
                    "completeness_assertion": "partial_but_governed",
                    "evidence_reference_ids": [],
                },
            },
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert detail["message"] == "Invalid computation request payload."
    assert details["reason"] == "unsupported_mixed_context_hc_mctx_cmb_0001"
    assert details["path"] == "$.input_payload.mixed_context_inputs.context_items"


def test_execution_endpoint_rejects_single_lane_mixed_context_through_canonical_envelope() -> None:
    """Verify single-lane requests with mixed facts fail through centralized screening."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "sha_shif",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-10-31",
                    "version_selection_basis": "payroll_period_end",
                    "historical_version_id": "HCH-VER-20241001-A",
                    "governing_change_ids": ["HC-CHG-2024-10-01-A"],
                    "source_anchor_ids": ["HC-SHI-REG-2024-09-20"],
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
                            "amount_kes": "40000.00",
                            "event_date": "2024-10-31",
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
                "mixed_context_inputs": {
                    "context_items": [
                        {
                            "mixed_context_type": "salaried_and_non_salaried_overlap",
                            "affected_domain_ids": [
                                "HCD-CORE-SHI-SALARIED",
                                "HCD-CORE-SHI-NONSALARIED",
                            ],
                            "reference_id": "MIX-SHA-001",
                        }
                    ]
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
                "traceability_context": {
                    "source_record_ids": ["SRC-SHA-001"],
                    "preparation_profile": "payroll_import_normalized",
                    "completeness_assertion": "complete",
                    "evidence_reference_ids": ["EVI-SHA-001"],
                },
            },
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert detail["message"] == "Invalid computation request payload."
    assert details["reason"] == "unsupported_mixed_context_hc_mctx_cmb_0002"
    assert details["path"] == "$.input_payload.mixed_context_inputs.context_items"


def test_execution_endpoint_rejects_unresolved_transition_boundary_date() -> None:
    """Verify unresolved transition-boundary dates return canonical binding errors."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": "transition_boundary",
            "tax_year": 2024,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2024-07-15",
                    "version_selection_basis": "registration_effective_date",
                },
                "contributor_context": {
                    "contributor_kind": "employee",
                    "asserted_domain_path": "transition_boundary",
                    "contribution_subject_reference_id": "SUBJECT-TX-001",
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
                    "household_income_items": [],
                    "means_testing_assertions": [],
                    "household_member_reference_ids": [],
                },
                "special_case_assertions": {"assertion_items": []},
                "mixed_context_inputs": {"context_items": []},
                "operational_context": {
                    "workflow_flags": [],
                    "registration_status": "started",
                    "remittance_channel": "not_provided",
                    "reference_ids": [],
                },
                "traceability_context": {
                    "source_record_ids": ["SRC-TX-001"],
                    "preparation_profile": "historical_reconstruction_normalized",
                    "completeness_assertion": "partial_but_governed",
                    "evidence_reference_ids": [],
                },
            },
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert details["reason"] == "unresolved_transition_window"


def test_execution_endpoint_rejects_invalid_shape_with_deterministic_error() -> None:
    """Verify malformed request payload returns stable 400 shared envelope."""

    client = _build_contract_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "income_tax",
            "regime_type": "income_tax",
            "input_payload": {"income": 100},
        },
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))

    assert response.status_code == 400
    assert payload["detail"]["error_code"] == "invalid_computation_request"
    assert payload["detail"]["message"] == "Invalid computation request payload."
    assert payload["detail"]["correlation_id"]


def test_execution_endpoint_rejects_resident_payroll_withholding_input() -> None:
    """Verify resident lane rejects unsupported payroll withholding normalization input."""

    client = _build_contract_test_client()
    request_payload = _load_golden_execution_request(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    employment_items = _employment_items_from_request(request_payload)
    employment_items[0]["paye_withheld_kes"] = "120000.00"

    response = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = detail["details"]

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_payroll_withholding_input"
    assert (
        details["path"]
        == "$.input_payload.income_sections.employment.employment_items[0].paye_withheld_kes"
    )


def test_execution_endpoint_rejects_non_resident_payroll_withholding_input() -> None:
    """Verify non-resident lane rejects unsupported payroll withholding input."""

    client = _build_contract_test_client()
    request_payload = _load_golden_execution_request(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )
    employment_items = _employment_items_from_request(request_payload)
    employment_items[0]["paye_withheld_kes"] = "110000.00"

    response = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = detail["details"]

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_payroll_withholding_input"
    assert (
        details["path"]
        == "$.input_payload.income_sections.employment.employment_items[0].paye_withheld_kes"
    )


def test_execution_endpoint_rejects_mixed_lane_payroll_withholding_input() -> None:
    """Verify mixed lane rejects unsupported payroll withholding input on employment items."""

    client = _build_contract_test_client()
    request_payload = _load_golden_execution_request(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )
    employment_items = _employment_items_from_request(request_payload)
    employment_items[0]["paye_withheld_kes"] = "120000.00"

    response = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )
    payload = cast(ErrorResponseBody, _response_json(response))
    detail = payload["detail"]
    details = detail["details"]

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"
    assert details["reason"] == "unsupported_payroll_withholding_input"
    assert (
        details["path"]
        == "$.input_payload.income_sections.employment.employment_items[0].paye_withheld_kes"
    )


def test_execution_endpoint_payroll_withholding_rejection_is_deterministic() -> None:
    """Verify repeated unsupported payroll-withholding requests return identical envelopes."""

    client = _build_contract_test_client()
    request_payload = _load_golden_execution_request(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    employment_items = _employment_items_from_request(request_payload)
    employment_items[0]["paye_withheld_kes"] = "120000.00"

    response_one = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )
    response_two = client.post(
        "/computations/execute",
        json=request_payload,
        headers=_execution_headers(),
    )

    assert response_one.status_code == 400
    assert response_two.status_code == 400
    assert _canonical_json(_response_json(response_one)) == _canonical_json(
        _response_json(response_two)
    )


def test_execution_endpoint_is_deterministic_for_identical_logical_requests() -> None:
    """Verify endpoint payload is byte-equivalent for logical-equivalent requests."""

    client = _build_contract_test_client()
    request_one = {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2025,
        "rule_version": "v1",
        "input_payload": {"b": {"y": 2, "x": 1}, "a": [3, {"k": 9, "j": 8}]},
    }
    request_two = {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2025,
        "rule_version": "v1",
        "input_payload": {"a": [3, {"j": 8, "k": 9}], "b": {"x": 1, "y": 2}},
    }

    response_one = client.post(
        "/computations/execute",
        json=request_one,
        headers=_execution_headers(),
    )
    response_two = client.post(
        "/computations/execute",
        json=request_two,
        headers=_execution_headers(),
    )

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    assert _canonical_json(_response_json(response_one)) == _canonical_json(
        _response_json(response_two)
    )


def _build_health_persisted_validation_source(fixture_name: str) -> PersistedValidationSource:
    fixture_payload = _load_golden_fixture_payload(fixture_name)
    request_payload = cast(dict[str, object], copy.deepcopy(fixture_payload["request"]))
    expected_output = cast(dict[str, object], fixture_payload["expected_output"])
    result_payload = cast(dict[str, object], copy.deepcopy(expected_output["result_payload"]))
    result_payload["_kodi_replay_context"] = {"normalized_input": request_payload["input_payload"]}

    return PersistedValidationSource(
        computation_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        user_id=TEST_PRINCIPAL_ID,
        tax_type=cast(str, request_payload["tax_type"]),
        regime_type=cast(str, request_payload["regime_type"]),
        regime_identifier=cast(str | None, request_payload["regime_identifier"]),
        tax_year=cast(int, request_payload["tax_year"]),
        rule_version=cast(str, request_payload["rule_version"]),
        input_hash=cast(str, expected_output["input_hash"]),
        stored_result_payload=result_payload,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _build_contract_test_client() -> TestClient:
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
    return _execution_headers_for_user(TEST_PRINCIPAL_ID)


def _execution_headers_for_user(user_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {user_id}:IndividualTaxpayer",
        "Idempotency-Key": TEST_IDEMPOTENCY_KEY,
        "X-Correlation-ID": TEST_CORRELATION_ID,
    }


def _response_json(response: object) -> dict[str, object]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _load_golden_execution_request(fixture_name: str) -> dict[str, object]:
    fixture_payload = _load_golden_fixture_payload(fixture_name)
    request_payload = cast(dict[str, object], fixture_payload["request"])
    return copy.deepcopy(request_payload)


def _load_golden_fixture_payload(fixture_name: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((_GOLDEN_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")),
    )


def _employment_items_from_request(request_payload: dict[str, object]) -> list[dict[str, object]]:
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    income_sections = cast(dict[str, object], input_payload["income_sections"])
    employment = cast(dict[str, object], income_sections["employment"])
    return cast(list[dict[str, object]], employment["employment_items"])
