"""Integrity tests for governed health-contribution IO contracts."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from jsonschema.validators import Draft202012Validator

from shared.validation import contract_validator
from shared.determinism.input_hash import canonical_json_dumps

DOC_PATH = Path("docs/phase-4/health-contribution/io_contract.md")
REQUEST_SCHEMA_PATH = Path(
    "contracts/tools/schemas/health_contribution_execution_request.schema.json"
)
RESULT_SCHEMA_PATH = Path("contracts/tools/schemas/health_contribution_result_payload.schema.json")

DOC_REQUIRED_SECTIONS = (
    "## 1. Contract Position",
    "## 2. Design Rules",
    "## 3. Request Contract",
    "## 4. Result Contract",
    "## 5. Deliberate Limits",
    "## 6. Immediate Downstream Use",
)

REQUEST_REQUIRED_KEYS = {
    "tax_type",
    "regime_type",
    "regime_identifier",
    "tax_year",
    "rule_version",
    "input_payload",
}

RESULT_REQUIRED_KEYS = {
    "version_identity",
    "contributor_outcome",
    "domain_outcomes",
    "contribution_summary",
    "unsupported_or_unresolved",
    "traceability",
}

EXPECTED_CONTRACT_SNAPSHOT = {
    "request_required": [
        "input_payload",
        "regime_identifier",
        "regime_type",
        "rule_version",
        "tax_type",
        "tax_year",
    ],
    "request_payload_required": [
        "contributor_context",
        "mixed_context_inputs",
        "nhif_legacy_inputs",
        "operational_context",
        "sha_shif_non_salaried_inputs",
        "sha_shif_salaried_inputs",
        "special_case_assertions",
        "traceability_context",
        "version_context",
    ],
    "request_version_selection_basis": [
        "household_income_reference_date",
        "payment_due_date",
        "payroll_period_end",
        "registration_effective_date",
        "specific_event_date",
    ],
    "result_required": [
        "contribution_summary",
        "contributor_outcome",
        "domain_outcomes",
        "traceability",
        "unsupported_or_unresolved",
        "version_identity",
    ],
    "result_domain_outcomes_required": [
        "consolidated_state_binding",
        "contributor_classification",
        "exemptions_and_special_cases",
        "mixed_context_paths",
        "nhif_legacy",
        "operational_interaction",
        "regime_selection",
        "sha_shif_non_salaried",
        "sha_shif_salaried",
        "sha_staged_activation",
        "validation_evidence",
        "version_selection",
    ],
    "result_reason_codes": [
        "governed_boundary_only",
        "insufficient_evidence",
        "mixed_context_requires_separate_path",
        "operational_interaction_separate",
        "transition_window_requires_split",
        "unresolved_policy",
        "unsupported_domain",
        "unsupported_special_case",
        "unsupported_version_window",
    ],
    "result_traceability_required": [
        "applied_policy_ids",
        "computation_status",
        "governing_change_ids",
        "input_hash",
        "replay_safe",
        "source_anchor_ids",
        "validation_focus_domains",
    ],
}


def test_health_contribution_io_contract_files_exist_and_have_required_sections() -> None:
    assert DOC_PATH.exists()
    contents = DOC_PATH.read_text(encoding="utf-8")
    for section in DOC_REQUIRED_SECTIONS:
        assert section in contents


def test_health_contribution_schemas_parse_and_are_valid_json_schema() -> None:
    for schema_path in (REQUEST_SCHEMA_PATH, RESULT_SCHEMA_PATH):
        schema = _load_schema(schema_path)
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        contract_validator.validate_json_schema_file(schema_path)


def test_health_contribution_schemas_include_required_top_level_fields() -> None:
    request_schema = _load_schema(REQUEST_SCHEMA_PATH)
    result_schema = _load_schema(RESULT_SCHEMA_PATH)

    assert set(cast(list[str], request_schema["required"])) == REQUEST_REQUIRED_KEYS
    assert set(cast(list[str], result_schema["required"])) == RESULT_REQUIRED_KEYS
    assert request_schema["additionalProperties"] is False
    assert result_schema["additionalProperties"] is False


def test_health_contribution_contract_structure_snapshot_is_stable() -> None:
    request_schema = _load_schema(REQUEST_SCHEMA_PATH)
    result_schema = _load_schema(RESULT_SCHEMA_PATH)

    snapshot = {
        "request_required": sorted(cast(list[str], request_schema["required"])),
        "request_payload_required": sorted(
            cast(list[str], request_schema["$defs"]["InputPayload"]["required"])
        ),
        "request_version_selection_basis": sorted(
            cast(
                list[str],
                request_schema["$defs"]["VersionContext"]["properties"]["version_selection_basis"][
                    "enum"
                ],
            )
        ),
        "result_required": sorted(cast(list[str], result_schema["required"])),
        "result_domain_outcomes_required": sorted(
            cast(list[str], result_schema["$defs"]["DomainOutcomes"]["required"])
        ),
        "result_reason_codes": sorted(
            cast(
                list[str],
                result_schema["$defs"]["UnsupportedOrUnresolvedItem"]["properties"]["reason_code"][
                    "enum"
                ],
            )
        ),
        "result_traceability_required": sorted(
            cast(list[str], result_schema["$defs"]["Traceability"]["required"])
        ),
    }

    assert canonical_json_dumps(snapshot) == canonical_json_dumps(EXPECTED_CONTRACT_SNAPSHOT)


def test_positive_minimal_health_contribution_payloads_validate() -> None:
    request_errors = sorted(
        _build_validator(REQUEST_SCHEMA_PATH).iter_errors(_valid_request_payload()),
        key=lambda item: item.path,
    )
    result_errors = sorted(
        _build_validator(RESULT_SCHEMA_PATH).iter_errors(_valid_result_payload()),
        key=lambda item: item.path,
    )

    assert request_errors == []
    assert result_errors == []


def test_negative_missing_required_fields_fail_validation() -> None:
    invalid_request = _valid_request_payload()
    invalid_request["input_payload"]["version_context"].pop("primary_effective_date")
    invalid_result = _valid_result_payload()
    invalid_result["traceability"].pop("input_hash")

    request_errors = sorted(
        _build_validator(REQUEST_SCHEMA_PATH).iter_errors(invalid_request),
        key=lambda item: item.path,
    )
    result_errors = sorted(
        _build_validator(RESULT_SCHEMA_PATH).iter_errors(invalid_result),
        key=lambda item: item.path,
    )

    assert request_errors
    assert result_errors
    assert any(
        error.validator == "required" and "primary_effective_date" in error.message
        for error in request_errors
    )
    assert any(
        error.validator == "required" and "input_hash" in error.message for error in result_errors
    )


def test_negative_disallowed_extra_fields_fail_validation() -> None:
    invalid_request = _valid_request_payload()
    invalid_request["unexpected_field"] = "not-allowed"
    invalid_result = _valid_result_payload()
    invalid_result["domain_outcomes"]["sha_shif_salaried"]["unexpected_field"] = "not-allowed"

    request_errors = sorted(
        _build_validator(REQUEST_SCHEMA_PATH).iter_errors(invalid_request),
        key=lambda item: item.path,
    )
    result_errors = sorted(
        _build_validator(RESULT_SCHEMA_PATH).iter_errors(invalid_result),
        key=lambda item: item.path,
    )

    assert request_errors
    assert result_errors
    assert any(error.validator == "additionalProperties" for error in request_errors)
    assert any(error.validator == "additionalProperties" for error in result_errors)


def _build_validator(schema_path: Path) -> Draft202012Validator:
    schema = _load_schema(schema_path)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return cast(
        Draft202012Validator,
        validator_class(schema, format_checker=FormatChecker()),
    )


def _load_schema(schema_path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(schema_path.read_text(encoding="utf-8")))


def _valid_request_payload() -> dict[str, Any]:
    return {
        "tax_type": "health_contribution",
        "regime_type": "health_contribution",
        "regime_identifier": "sha_shif",
        "tax_year": 2024,
        "rule_version": "phase4b-governed-v1",
        "input_payload": {
            "version_context": {
                "primary_effective_date": "2024-10-01",
                "contribution_period_start": "2024-10-01",
                "contribution_period_end": "2024-10-31",
                "version_selection_basis": "payroll_period_end",
                "historical_version_id": "HCH-VER-20241001-A",
                "governing_change_ids": ["HC-CHG-2024-10-01-A"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "contributor_context": {
                "contributor_kind": "employee",
                "asserted_domain_path": "sha_shif_salaried",
                "contribution_subject_reference_id": "SUBJECT-001",
                "employer_reference_id": "EMPLOYER-001",
                "payroll_reference_id": "PAYROLL-2024-10",
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
                        "amount_kes": "100000.00",
                        "event_date": "2024-10-31",
                        "reference_id": "PAY-ITEM-001",
                    }
                ],
                "employer_assertions": [
                    {
                        "assertion_type": "deduction_path_asserted",
                        "assertion_status": "confirmed_by_evidence",
                        "source_reference_id": "EVI-EMPLOYER-001",
                    }
                ],
                "remittance_reference_ids": ["REMIT-001"],
            },
            "sha_shif_non_salaried_inputs": {
                "household_income_items": [],
                "means_testing_assertions": [],
                "household_member_reference_ids": [],
            },
            "special_case_assertions": {"assertion_items": []},
            "mixed_context_inputs": {"context_items": []},
            "operational_context": {
                "workflow_flags": ["payment_and_access_live"],
                "registration_status": "active",
                "remittance_channel": "sha_portal",
                "reference_ids": ["OPS-REF-001"],
            },
            "traceability_context": {
                "source_record_ids": ["SRC-REC-001"],
                "evidence_reference_ids": ["EVI-001"],
                "preparation_profile": "payroll_import_normalized",
                "completeness_assertion": "partial_but_governed",
            },
        },
    }


def _valid_result_payload() -> dict[str, Any]:
    computed_domain_outcome = {
        "status": "computed",
        "contribution_basis_kes": "100000.00",
        "employee_contribution_kes": "2750.00",
        "employer_contribution_kes": "2750.00",
        "household_contribution_kes": None,
        "total_contribution_kes": "5500.00",
        "decision_refs": ["DEC-SALARIED-001"],
        "applied_policy_ids": ["HCP-POL-204"],
        "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
    }
    not_applicable_domain_outcome = {
        "status": "not_applicable",
        "contribution_basis_kes": None,
        "employee_contribution_kes": None,
        "employer_contribution_kes": None,
        "household_contribution_kes": None,
        "total_contribution_kes": None,
        "decision_refs": ["DEC-NA-001"],
        "applied_policy_ids": ["HCP-POL-001"],
        "source_anchor_ids": ["HC-SHI-ACT-2023-11-24"],
    }

    return {
        "version_identity": {
            "historical_version_id": "HCH-VER-20241001-A",
            "tax_year": 2024,
            "rule_version": "phase4b-governed-v1",
            "regime_identifier": "sha_shif",
            "effective_start": "2024-10-01",
            "effective_end": "2025-02-27",
            "version_selection_basis": "payroll_period_end",
            "governing_change_ids": ["HC-CHG-2024-10-01-A"],
            "source_anchor_ids": ["HC-SHI-REG-2024-03-08", "HC-CHG-2024-10-01-A"],
        },
        "contributor_outcome": {
            "contributor_kind": "employee",
            "resolved_domain_path": "sha_shif_salaried",
            "regime_family": "sha_shif",
            "classification_outcome": "fully_classified",
        },
        "domain_outcomes": {
            "contributor_classification": {
                "status": "computed",
                "contribution_basis_kes": "100000.00",
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-CLASS-001"],
                "applied_policy_ids": ["HCP-POL-003"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "nhif_legacy": not_applicable_domain_outcome,
            "sha_shif_salaried": computed_domain_outcome,
            "sha_shif_non_salaried": not_applicable_domain_outcome,
            "regime_selection": {
                "status": "computed",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-REGIME-001"],
                "applied_policy_ids": ["HCP-POL-002"],
                "source_anchor_ids": [
                    "HC-NHIF-REPEAL-2023-SHI-ACT",
                    "HC-SHI-ACT-2023-11-24",
                ],
            },
            "sha_staged_activation": {
                "status": "computed",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-STAGE-001"],
                "applied_policy_ids": ["HCP-POL-202"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "consolidated_state_binding": {
                "status": "not_applicable",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-CSB-001"],
                "applied_policy_ids": ["HCP-POL-004"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "exemptions_and_special_cases": {
                "status": "unresolved",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-SPECIAL-001"],
                "applied_policy_ids": ["HCP-POL-007"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "mixed_context_paths": {
                "status": "not_applicable",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-MIXED-001"],
                "applied_policy_ids": ["HCP-POL-008"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "operational_interaction": {
                "status": "computed",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-OPS-001"],
                "applied_policy_ids": ["HCP-POL-006"],
                "source_anchor_ids": ["HC-SHA-OPS-EMPLOYER-PORTAL-2024-10-01"],
            },
            "validation_evidence": {
                "status": "computed",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-VAL-001"],
                "applied_policy_ids": ["HCP-POL-005"],
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
            },
            "version_selection": {
                "status": "computed",
                "contribution_basis_kes": None,
                "employee_contribution_kes": None,
                "employer_contribution_kes": None,
                "household_contribution_kes": None,
                "total_contribution_kes": None,
                "decision_refs": ["DEC-VERSION-001"],
                "applied_policy_ids": ["HCP-POL-001"],
                "source_anchor_ids": ["HC-CHG-2024-10-01-A"],
            },
        },
        "contribution_summary": {
            "regime_family": "sha_shif",
            "coverage_status": "partially_specified",
            "summary_status": "partial",
            "contribution_basis_kes": "100000.00",
            "employee_contribution_kes": "2750.00",
            "employer_contribution_kes": "2750.00",
            "household_contribution_kes": None,
            "total_contribution_kes": "5500.00",
            "currency": "KES",
        },
        "unsupported_or_unresolved": [
            {
                "domain_id": "HCD-XCUT-EXEMPTIONS-SPECIAL-CASES",
                "reason_code": "unresolved_policy",
                "decision_ref": "DEC-SPECIAL-001",
                "source_anchor_ids": ["HC-SHI-REG-2024-03-08"],
                "applied_policy_ids": ["HCP-POL-007"],
            }
        ],
        "traceability": {
            "input_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "applied_policy_ids": ["HCP-POL-001", "HCP-POL-202", "HCP-POL-204"],
            "source_anchor_ids": ["HC-SHI-REG-2024-03-08", "HC-CHG-2024-10-01-A"],
            "governing_change_ids": ["HC-CHG-2024-10-01-A"],
            "validation_focus_domains": [
                "HCD-CORE-SHI-SALARIED",
                "HCD-XCUT-VALIDATION-EVIDENCE",
            ],
            "computation_status": "partial",
            "replay_safe": True,
        },
    }
