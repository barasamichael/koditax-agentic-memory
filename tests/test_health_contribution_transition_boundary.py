"""Test deterministic NHIF to SHA/SHIF transition-boundary behavior."""

from __future__ import annotations

import json
from typing import cast
from datetime import date

import pytest

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.rules.health_contribution.transition_boundary import (
    resolve_transition_selection,
)
from services.tax_core.app.rules.health_contribution.transition_boundary import (
    TransitionBoundaryBindingError,
)


@pytest.mark.parametrize(
    (
        "tax_year",
        "primary_effective_date",
        "expected_binding_id",
        "expected_regime_identifier",
        "expected_historical_version_id",
    ),
    [
        (
            2012,
            date(2012, 1, 31),
            "health_contribution_nhif_legacy_v1_2010_07_16",
            "nhif_legacy",
            "HCH-VER-20100716-A",
        ),
        (
            2019,
            date(2019, 7, 31),
            "health_contribution_nhif_legacy_v1_2015_04_01",
            "nhif_legacy",
            "HCH-VER-20150401-A",
        ),
        (
            2022,
            date(2022, 6, 30),
            "health_contribution_nhif_legacy_v1_2021_05_28",
            "nhif_legacy",
            "HCH-VER-20210528-A",
        ),
        (
            2023,
            date(2023, 5, 31),
            "health_contribution_nhif_legacy_v1_2022_12_31_reg",
            "nhif_legacy",
            "HCH-VER-20221231-REG",
        ),
        (
            2024,
            date(2024, 10, 31),
            "health_contribution_sha_shif_v1_2024_10_01",
            "sha_shif",
            "HCH-VER-20241001-A",
        ),
        (
            2025,
            date(2025, 3, 31),
            "health_contribution_sha_shif_v1_2025_02_28_pit",
            "sha_shif",
            "HCH-VER-20250228-PIT",
        ),
    ],
)
def test_resolve_transition_selection_maps_supported_dates_to_governed_windows(
    tax_year: int,
    primary_effective_date: date,
    expected_binding_id: str,
    expected_regime_identifier: str,
    expected_historical_version_id: str,
) -> None:
    """Verify transition routing resolves across the full implementation-ready set."""

    resolution = resolve_transition_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="transition_boundary",
            tax_year=tax_year,
            rule_version="v1",
            primary_effective_date=primary_effective_date,
        )
    )

    assert resolution is not None
    assert resolution.binding_id == expected_binding_id
    assert resolution.resolved_regime_identifier == expected_regime_identifier
    assert resolution.historical_version_id == expected_historical_version_id


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id", "expected_reason"),
    [
        (2009, date(2009, 12, 31), "HCH-VER-20031205-A", "unsupported_transition_window"),
        (2015, date(2015, 3, 1), "HCH-VER-20141208-A", "unsupported_transition_window"),
        (2021, date(2021, 4, 30), "HCH-VER-20210330-A", "unsupported_transition_window"),
        (2023, date(2023, 5, 31), "HCH-VER-20221231-ACT", "unsupported_transition_window"),
        (2024, date(2024, 7, 15), "HCH-VER-20240701-A", "unresolved_transition_window"),
        (2024, date(2024, 9, 25), "HCH-VER-20240920-PIT", "unresolved_transition_window"),
        (2025, date(2025, 3, 31), "HCH-VER-20250228-AMD", "unsupported_transition_window"),
    ],
)
def test_resolve_transition_selection_rejects_non_ready_windows(
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_reason: str,
) -> None:
    """Verify non-ready historical windows remain fail-closed in transition mode."""

    with pytest.raises(TransitionBoundaryBindingError) as error:
        resolve_transition_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier="transition_boundary",
                tax_year=tax_year,
                rule_version="v1",
                primary_effective_date=primary_effective_date,
                historical_version_id=historical_version_id,
            )
        )

    assert error.value.reason == expected_reason


@pytest.mark.parametrize(
    (
        "tax_year",
        "primary_effective_date",
        "expected_regime_identifier",
        "expected_historical_version_id",
    ),
    [
        (2023, date(2023, 11, 21), "nhif_legacy", "HCH-VER-20221231-REG"),
        (2024, date(2024, 10, 1), "sha_shif", "HCH-VER-20241001-A"),
        (2025, date(2025, 2, 27), "sha_shif", "HCH-VER-20241001-A"),
        (2025, date(2025, 2, 28), "sha_shif", "HCH-VER-20250228-PIT"),
    ],
)
def test_resolve_transition_selection_maps_exact_cutover_edges(
    tax_year: int,
    primary_effective_date: date,
    expected_regime_identifier: str,
    expected_historical_version_id: str,
) -> None:
    """Verify exact transition cutover dates resolve to one governed supported window."""

    resolution = resolve_transition_selection(
        RuleSelectionKey(
            tax_type="health_contribution",
            regime_type="health_contribution",
            regime_identifier="transition_boundary",
            tax_year=tax_year,
            rule_version="v1",
            primary_effective_date=primary_effective_date,
        )
    )

    assert resolution is not None
    assert resolution.resolved_regime_identifier == expected_regime_identifier
    assert resolution.historical_version_id == expected_historical_version_id


def test_execute_computation_transition_boundary_routes_to_nhif_rule_pack() -> None:
    """Verify transition-boundary execution selects the governed NHIF pack."""

    result = execute_computation(
        ComputationExecutionRequest.model_validate(
            _transition_nhif_request_payload(primary_effective_date="2023-05-31")
        )
    )
    result_payload = result.result_payload
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])

    assert version_identity["regime_identifier"] == "nhif_legacy"
    assert version_identity["historical_version_id"] == "HCH-VER-20221231-REG"
    assert contribution_summary["total_contribution_kes"] == "1100.00"


def test_execute_computation_transition_boundary_routes_to_sha_rule_pack() -> None:
    """Verify transition-boundary execution selects the governed SHA pack."""

    result = execute_computation(
        ComputationExecutionRequest.model_validate(
            _transition_sha_request_payload(primary_effective_date="2024-10-31")
        )
    )
    result_payload = result.result_payload
    version_identity = cast(dict[str, object], result_payload["version_identity"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])

    assert version_identity["regime_identifier"] == "sha_shif"
    assert version_identity["historical_version_id"] == "HCH-VER-20241001-A"
    assert contribution_summary["total_contribution_kes"] == "1100.00"


def test_execute_computation_transition_boundary_rejects_ambiguous_payload_context() -> None:
    """Verify transition-boundary execution fails closed for ambiguous lane inputs."""

    payload = _transition_sha_request_payload(primary_effective_date="2024-10-31")
    input_payload = cast(dict[str, object], payload["input_payload"])
    input_payload["nhif_legacy_inputs"] = {
        "earning_items": [
            {
                "income_basis_type": "salary_band_basis",
                "amount_kes": "45000.00",
                "event_date": "2024-10-31",
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
    }

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "ambiguous_transition_regime_selection"


def test_execute_computation_transition_boundary_rejects_special_case_assertions_as_ambiguous() -> (
    None
):
    """Verify unresolved special-case assertions remain outside governed transition routing."""

    payload = _transition_nhif_request_payload(primary_effective_date="2023-05-31")
    input_payload = cast(dict[str, object], payload["input_payload"])
    input_payload["special_case_assertions"] = {
        "assertion_items": [
            {
                "assertion_type": "exemption_pending_policy",
                "assertion_status": "asserted",
                "affected_domain_id": "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
                "source_reference_id": "EVI-SPCASE-001",
            }
        ]
    }

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "ambiguous_transition_regime_selection"
    assert error.value.path == "$.input_payload.special_case_assertions.assertion_items"


def test_execute_computation_transition_boundary_rejects_malformed_section_shape() -> None:
    """Verify malformed transition-boundary sections fail closed with a stable shape error."""

    payload = _transition_sha_request_payload(primary_effective_date="2024-10-31")
    input_payload = cast(dict[str, object], payload["input_payload"])
    input_payload["mixed_context_inputs"] = "not_a_section"

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(payload))

    assert error.value.reason == "unsupported_transition_request_shape"
    assert error.value.path == "$.input_payload.mixed_context_inputs"


def test_transition_boundary_execution_is_deterministic_for_equivalent_requests() -> None:
    """Verify equivalent transition-boundary requests remain byte-equivalent."""

    request_one = ComputationExecutionRequest.model_validate(
        _transition_nhif_request_payload(primary_effective_date="2023-05-31")
    )
    request_two = ComputationExecutionRequest.model_validate(
        {
            "rule_version": "v1",
            "tax_year": 2023,
            "regime_identifier": "transition_boundary",
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
                    "asserted_domain_path": "transition_boundary",
                    "contributor_kind": "employee",
                    "payroll_reference_id": "PAYROLL-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "contribution_subject_reference_id": "SUBJECT-001",
                },
                "version_context": {
                    "version_selection_basis": "payroll_period_end",
                    "primary_effective_date": "2023-05-31",
                },
            },
        }
    )

    first = execute_computation(request_one).model_dump(mode="json")
    second = execute_computation(request_two).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


def _transition_nhif_request_payload(*, primary_effective_date: str) -> dict[str, object]:
    return {
        "tax_type": "health_contribution",
        "regime_type": "health_contribution",
        "regime_identifier": "transition_boundary",
        "tax_year": 2023,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": primary_effective_date,
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
                        "event_date": primary_effective_date,
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
    }


def _transition_sha_request_payload(*, primary_effective_date: str) -> dict[str, object]:
    return {
        "tax_type": "health_contribution",
        "regime_type": "health_contribution",
        "regime_identifier": "transition_boundary",
        "tax_year": 2024,
        "rule_version": "v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": primary_effective_date,
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


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
