"""Test governed fail-closed mixed-context health-contribution behavior."""

from __future__ import annotations

import json
from typing import cast

import pytest

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest


def test_bind_rule_selection_resolves_fail_closed_mixed_context_binding() -> None:
    """Verify explicit mixed_context requests bind deterministically."""

    bound_rule = bind_rule_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="mixed_context",
            tax_year=2025,
            rule_version="v1",
            primary_effective_date=None,
        )
    )

    assert bound_rule.binding_id == "health_contribution_mixed_context_v1_fail_closed"


def test_execute_computation_rejects_legacy_and_active_overlap_as_candidate_0001() -> None:
    """Verify legacy-and-active mixed overlap is classified to HC-MCTX-CMB-0001."""

    with pytest.raises(InputHashError) as error:
        execute_computation(
            ComputationExecutionRequest.model_validate(
                _mixed_context_request_payload(
                    mixed_context_type="legacy_and_active_overlap",
                )
            )
        )

    assert error.value.reason == "unsupported_mixed_context_hc_mctx_cmb_0001"
    assert error.value.path == "$.input_payload.mixed_context_inputs.context_items"
    assert "HC-MCTX-CMB-0001" in error.value.message
    assert "HCP-POL-304" in error.value.message


def test_execute_computation_rejects_salaried_and_non_salaried_overlap_as_candidate_0002() -> None:
    """Verify salaried-plus-non-salaried overlap is classified to HC-MCTX-CMB-0002."""

    request_payload = _mixed_context_request_payload(
        mixed_context_type="salaried_and_non_salaried_overlap",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    input_payload["sha_shif_salaried_inputs"] = {
        "payroll_items": [
            {
                "income_basis_type": "gross_salary_basis",
                "amount_kes": "40000.00",
                "event_date": "2025-03-31",
                "reference_id": "PAY-SHA-001",
            }
        ],
        "employer_assertions": [],
        "remittance_reference_ids": [],
    }
    input_payload["sha_shif_non_salaried_inputs"] = {
        "household_income_items": [
            {
                "income_basis_type": "annual_household_income",
                "amount_kes": "200000.00",
                "event_date": "2025-03-31",
                "reference_id": "HOUSE-001",
            }
        ],
        "means_testing_assertions": [],
        "household_member_reference_ids": [],
    }

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_mixed_context_hc_mctx_cmb_0002"
    assert "HC-MCTX-CMB-0002" in error.value.message
    assert "HCP-POL-008" in error.value.message


def test_execute_computation_rejects_employer_employee_split_as_candidate_0003() -> None:
    """Verify split-style mixed facts are classified to HC-MCTX-CMB-0003."""

    with pytest.raises(InputHashError) as error:
        execute_computation(
            ComputationExecutionRequest.model_validate(
                _mixed_context_request_payload(
                    mixed_context_type="other_governed_mixed_context",
                )
            )
        )

    assert error.value.reason == "unsupported_mixed_context_hc_mctx_cmb_0003"
    assert "HC-MCTX-CMB-0003" in error.value.message
    assert "HCP-POL-003" in error.value.message
    assert "HCP-POL-008" in error.value.message


def test_execute_computation_rejects_special_case_mixed_context_as_candidate_0004() -> None:
    """Verify exemption-dependent mixed facts are classified to HC-MCTX-CMB-0004."""

    request_payload = _mixed_context_request_payload(
        mixed_context_type="legacy_and_active_overlap",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    input_payload["special_case_assertions"] = {
        "assertion_items": [
            {
                "assertion_type": "exemption_pending_policy",
                "assertion_status": "asserted",
                "affected_domain_id": "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
                "source_reference_id": "EVI-SP-001",
            }
        ]
    }

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_mixed_context_hc_mctx_cmb_0004"
    assert error.value.path == "$.input_payload.special_case_assertions.assertion_items"
    assert "HC-MCTX-CMB-0004" in error.value.message
    assert "HCP-POL-U03" in error.value.message


def test_execute_computation_rejects_empty_explicit_mixed_context_shape_deterministically() -> None:
    """Verify empty explicit mixed_context requests fail with a stable shape error."""

    request_payload = _mixed_context_request_payload(mixed_context_type=None)
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    input_payload["mixed_context_inputs"] = {"context_items": []}

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_mixed_context_request_shape"
    assert error.value.path == "$.input_payload"


def test_execute_computation_rejects_malformed_mixed_context_section_shape() -> None:
    """Verify malformed mixed-context sections fail with the canonical shape reason."""

    request_payload = _mixed_context_request_payload(
        mixed_context_type="legacy_and_active_overlap",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    input_payload["mixed_context_inputs"] = "not_a_section"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_mixed_context_request_shape"
    assert error.value.path == "$.input_payload.mixed_context_inputs"


def test_single_lane_nhif_request_with_mixed_context_items_uses_central_screening() -> None:
    """Verify NHIF lane delegates mixed-context rejection to the centralized module."""

    request_payload = _nhif_request_with_mixed_context(
        mixed_context_type="legacy_and_active_overlap",
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_mixed_context_hc_mctx_cmb_0001"
    assert "HC-MCTX-CMB-0001" in error.value.message


def test_single_lane_sha_request_with_mixed_context_items_uses_central_screening() -> None:
    """Verify SHA lane delegates mixed-context rejection to the centralized module."""

    request_payload = _sha_request_with_mixed_context(
        mixed_context_type="salaried_and_non_salaried_overlap",
    )

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_mixed_context_hc_mctx_cmb_0002"
    assert "HC-MCTX-CMB-0002" in error.value.message


def test_mixed_context_rejection_is_deterministic_for_logically_equivalent_requests() -> None:
    """Verify equivalent mixed-context requests reject with canonical-equivalent errors."""

    request_one = ComputationExecutionRequest.model_validate(
        _mixed_context_request_payload(
            mixed_context_type="legacy_and_active_overlap",
        )
    )
    request_two = ComputationExecutionRequest.model_validate(
        {
            "rule_version": "v1",
            "tax_year": 2025,
            "regime_identifier": "mixed_context",
            "regime_type": "health_contribution",
            "tax_type": "health_contribution",
            "input_payload": {
                "traceability_context": {
                    "source_record_ids": ["SRC-MIX-001"],
                    "preparation_profile": "historical_reconstruction_normalized",
                    "completeness_assertion": "partial_but_governed",
                    "evidence_reference_ids": [],
                },
                "operational_context": {
                    "workflow_flags": [],
                    "registration_status": "unresolved",
                    "remittance_channel": "not_provided",
                    "reference_ids": ["OPS-MIX-001"],
                },
                "mixed_context_inputs": {
                    "context_items": [
                        {
                            "reference_id": "MIX-001",
                            "affected_domain_ids": [
                                "HCD-CORE-NHIF-LEGACY",
                                "HCD-TRANS-REGIME-SELECTION",
                            ],
                            "mixed_context_type": "legacy_and_active_overlap",
                        }
                    ]
                },
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
                    "member_class_assertions": [],
                    "earning_items": [],
                    "deduction_reference_ids": [],
                },
                "contributor_context": {
                    "asserted_domain_path": "mixed_context",
                    "contributor_kind": "mixed_context",
                    "payroll_reference_id": "PAYROLL-MIX-001",
                    "employer_reference_id": "EMPLOYER-MIX-001",
                    "contribution_subject_reference_id": "SUBJECT-MIX-001",
                },
                "version_context": {
                    "version_selection_basis": "specific_event_date",
                    "primary_effective_date": "2025-03-31",
                },
            },
        }
    )

    first_error = _error_snapshot(request_one)
    second_error = _error_snapshot(request_two)

    assert _canonical_json(first_error) == _canonical_json(second_error)


def _error_snapshot(request: ComputationExecutionRequest) -> dict[str, object]:
    with pytest.raises(InputHashError) as error:
        execute_computation(request)
    return {
        "reason": error.value.reason,
        "message": error.value.message,
        "path": error.value.path,
    }


def _mixed_context_request_payload(
    *,
    mixed_context_type: str | None,
) -> dict[str, object]:
    context_items = (
        []
        if mixed_context_type is None
        else [
            {
                "mixed_context_type": mixed_context_type,
                "affected_domain_ids": [
                    "HCD-CORE-NHIF-LEGACY",
                    "HCD-TRANS-REGIME-SELECTION",
                ],
                "reference_id": "MIX-001",
            }
        ]
    )

    return {
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
            "mixed_context_inputs": {"context_items": context_items},
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
    }


def _nhif_request_with_mixed_context(*, mixed_context_type: str) -> dict[str, object]:
    return {
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
            "mixed_context_inputs": {
                "context_items": [
                    {
                        "mixed_context_type": mixed_context_type,
                        "affected_domain_ids": [
                            "HCD-CORE-NHIF-LEGACY",
                            "HCD-TRANS-REGIME-SELECTION",
                        ],
                        "reference_id": "MIX-NHIF-001",
                    }
                ]
            },
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


def _sha_request_with_mixed_context(*, mixed_context_type: str) -> dict[str, object]:
    return {
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
                        "mixed_context_type": mixed_context_type,
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
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
