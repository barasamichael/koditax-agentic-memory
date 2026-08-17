"""Test governed health-contribution exemptions and special-case handling."""

from __future__ import annotations

import json
from typing import cast
from pathlib import Path
from datetime import date

import pytest
from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest

RESULT_SCHEMA_PATH = Path("contracts/tools/schemas/health_contribution_result_payload.schema.json")


def test_execute_computation_returns_governed_special_member_special_case_payload() -> None:
    """Verify the source-proven NHIF special-member path is centralized and computed."""

    request = ComputationExecutionRequest.model_validate(
        _supported_nhif_request_payload(
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
    result_payload = result.result_payload
    domain_outcomes = cast(dict[str, object], result_payload["domain_outcomes"])
    exemptions_domain = cast(
        dict[str, object],
        domain_outcomes["exemptions_and_special_cases"],
    )
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])

    assert result.status == "ok"
    assert exemptions_domain["status"] == "computed"
    assert exemptions_domain["decision_refs"] == ["HC-NHIF-NPOL-0002", "HC-NHIF-NPOL-2021-001"]
    assert exemptions_domain["applied_policy_ids"] == [
        "HCP-POL-003",
        "HCP-POL-110",
        "HCP-POL-108",
    ]
    assert exemptions_domain["total_contribution_kes"] == "500.00"
    assert contribution_summary["total_contribution_kes"] == "500.00"
    _validate_result_payload_schema(result_payload)


@pytest.mark.parametrize(
    ("tax_year", "primary_effective_date", "historical_version_id"),
    [
        (2021, "2021-05-28", "HCH-VER-20210528-A"),
        (2022, "2022-12-30", "HCH-VER-20210528-A"),
    ],
)
def test_execute_computation_supports_special_member_on_exact_supported_edges(
    tax_year: int,
    primary_effective_date: str,
    historical_version_id: str,
) -> None:
    """Verify the governed special-member lane remains valid on exact supported edges."""

    request = ComputationExecutionRequest.model_validate(
        _supported_nhif_request_payload(
            tax_year=tax_year,
            primary_effective_date=primary_effective_date,
            historical_version_id=historical_version_id,
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

    assert contribution_summary["total_contribution_kes"] == "500.00"


def test_execute_computation_rejects_unresolved_exemption_assertion() -> None:
    """Verify unresolved exemption assertions remain fail-closed for NHIF requests."""

    request_payload = _supported_nhif_request_payload(
        tax_year=2023,
        primary_effective_date="2023-05-31",
        historical_version_id="HCH-VER-20221231-REG",
        contributor_kind="employee",
        member_class="standard_member",
        income_basis_type="salary_band_basis",
        amount_kes="45000.00",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    special_case_assertions = cast(dict[str, object], input_payload["special_case_assertions"])
    special_case_assertions["assertion_items"] = [
        {
            "assertion_type": "exemption_pending_policy",
            "assertion_status": "asserted",
            "affected_domain_id": "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
            "source_reference_id": "EVI-SPCASE-001",
        }
    ]

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_special_case_assertions"


def test_execute_computation_rejects_unresolved_sha_special_status_claim() -> None:
    """Verify SHA special-case assertions remain fail-closed for implementation-ready windows."""

    request_payload = _supported_sha_request_payload(
        tax_year=2024,
        primary_effective_date="2024-10-31",
        historical_version_id="HCH-VER-20241001-A",
        amount_kes="40000.00",
    )
    input_payload = cast(dict[str, object], request_payload["input_payload"])
    special_case_assertions = cast(dict[str, object], input_payload["special_case_assertions"])
    special_case_assertions["assertion_items"] = [
        {
            "assertion_type": "special_case_pending_policy",
            "assertion_status": "asserted",
            "affected_domain_id": "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
            "source_reference_id": "EVI-SPCASE-SHA-001",
        }
    ]

    with pytest.raises(InputHashError) as error:
        execute_computation(ComputationExecutionRequest.model_validate(request_payload))

    assert error.value.reason == "unsupported_special_case_assertions"


def test_bind_rule_selection_rejects_special_member_request_outside_supported_window() -> None:
    """Verify special-member handling does not widen historical NHIF coverage."""

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


def test_special_case_execution_is_deterministic_for_logical_equivalent_requests() -> None:
    """Verify the governed NHIF special-member path is canonical under key reordering."""

    request_one = ComputationExecutionRequest.model_validate(
        _supported_nhif_request_payload(
            tax_year=2022,
            primary_effective_date="2022-06-30",
            historical_version_id="HCH-VER-20210528-A",
            contributor_kind="self_employed",
            member_class="special_member",
            income_basis_type="special_contributor_basis",
            amount_kes="500.00",
        )
    )
    request_two = ComputationExecutionRequest.model_validate(
        {
            "rule_version": "v1",
            "tax_year": 2022,
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
                            "assertion_type": "special_member",
                            "source_reference_id": "EVI-NHIF-001",
                        }
                    ],
                    "earning_items": [
                        {
                            "event_date": "2022-06-30",
                            "amount_kes": "500.00",
                            "income_basis_type": "special_contributor_basis",
                            "reference_id": "PAY-NHIF-001",
                        }
                    ],
                    "deduction_reference_ids": ["DED-NHIF-001"],
                },
                "contributor_context": {
                    "asserted_domain_path": "nhif_legacy",
                    "contributor_kind": "self_employed",
                    "payroll_reference_id": "PAYROLL-001",
                    "employer_reference_id": "EMPLOYER-001",
                    "contribution_subject_reference_id": "SUBJECT-001",
                },
                "version_context": {
                    "source_anchor_ids": ["HC-NHIF-CONTRIB-REG-2021-05-28"],
                    "historical_version_id": "HCH-VER-20210528-A",
                    "version_selection_basis": "specific_event_date",
                    "primary_effective_date": "2022-06-30",
                    "governing_change_ids": ["HC-CHG-2021-05-28-A"],
                },
            },
        }
    )

    first = execute_computation(request_one).model_dump(mode="json")
    second = execute_computation(request_two).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


def _supported_nhif_request_payload(
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
                "version_selection_basis": "specific_event_date"
                if contributor_kind == "self_employed"
                else "payroll_period_end",
                "historical_version_id": historical_version_id,
                "governing_change_ids": [
                    "HC-CHG-2021-05-28-A"
                    if historical_version_id == "HCH-VER-20210528-A"
                    else "HC-CHG-2022-12-31-B"
                ],
                "source_anchor_ids": [
                    "HC-NHIF-CONTRIB-REG-2021-05-28"
                    if historical_version_id == "HCH-VER-20210528-A"
                    else "HC-NHIF-CONTRIB-REG-2022-12-31"
                ],
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


def _supported_sha_request_payload(
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


def _validate_result_payload_schema(result_payload: dict[str, object]) -> None:
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema, format_checker=FormatChecker())
    validator.validate(result_payload)


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
