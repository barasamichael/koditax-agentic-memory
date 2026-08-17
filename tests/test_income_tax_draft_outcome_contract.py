"""Verify canonical deterministic draft-outcome response contract mapping."""

from __future__ import annotations

from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.income_tax_draft_outcome_contract import (
    IncomeTaxDraftOutcomeContractError,
)
from services.orchestration.app.income_tax_draft_outcome_contract import (
    build_income_tax_draft_outcome_response,
)


def _sample_computation_output() -> dict[str, object]:
    return {
        "tax_type": "income_tax",
        "input_hash": "input-hash-001",
        "rule_version": "v1",
    }


def _sample_finalized_output() -> dict[str, object]:
    return {
        "computation_id": "comp-001",
        "finalized_audit_event_id": "audit-finalized-001",
        "result_payload": {
            "liability_summary": {
                "chargeable_income_kes": "960000.00",
                "gross_tax_kes": "225400.00",
                "total_reliefs_kes": "28800.00",
                "net_income_tax_due_kes": "196600.00",
                "refund_due_kes": "0.00",
            }
        },
    }


def _sample_form_artifact_output() -> dict[str, object]:
    return {
        "artifact_id": "form-artifact-001",
        "form_version_id": "it1-v2023.07",
        "audit_evidence": {"audit_evidence_id": "audit-form-001"},
    }


def _sample_report_output() -> dict[str, object]:
    return {
        "report_id": "report-001",
        "audit_evidence": {"audit_evidence_id": "audit-report-001"},
    }


def _sample_report_binding() -> dict[str, object]:
    return {"report_version_id": "report-v2023.07"}


def _sample_submission_payload_output() -> dict[str, object]:
    return {
        "payload_id": "payload-001",
        "payload_version": "payload-v1",
        "audit_evidence": {"audit_evidence_id": "audit-payload-001"},
    }


def test_builds_canonical_draft_ready_envelope() -> None:
    actual = build_income_tax_draft_outcome_response(
        prompt_id="prompt-001",
        prompt_text=(
            "Compute income tax for resident employment lane in tax year 2023 "
            "under KIT-VER-20230701-A."
        ),
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        computation_output=_sample_computation_output(),
        finalized_output=_sample_finalized_output(),
        form_artifact_output=_sample_form_artifact_output(),
        report_output=_sample_report_output(),
        report_version_binding=_sample_report_binding(),
        submission_payload_output=_sample_submission_payload_output(),
    )

    expected = {
        "status": "draft_ready",
        "message": (
            "Draft income-tax outcome is ready for review. Confirm to continue, "
            "reject to stop, or revise input."
        ),
        "prompt_id": "prompt-001",
        "prompt_text": (
            "Compute income tax for resident employment lane in tax year 2023 "
            "under KIT-VER-20230701-A."
        ),
        "draft_context": {
            "tax_type": "income_tax",
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
        "review_summary": {
            "chargeable_income_kes": "960000.00",
            "gross_tax_kes": "225400.00",
            "total_reliefs_kes": "28800.00",
            "net_income_tax_due_kes": "196600.00",
            "refund_due_kes": "0.00",
        },
        "artifacts": {
            "form_artifact_id": "form-artifact-001",
            "form_version_id": "it1-v2023.07",
            "report_id": "report-001",
            "report_version_id": "report-v2023.07",
            "submission_preview_payload_id": "payload-001",
            "submission_preview_payload_version": "payload-v1",
        },
        "lineage": {
            "computation_id": "comp-001",
            "input_hash": "input-hash-001",
            "rule_version": "v1",
            "finalized_audit_event_id": "audit-finalized-001",
            "form_audit_evidence_id": "audit-form-001",
            "report_audit_evidence_id": "audit-report-001",
            "payload_audit_evidence_id": "audit-payload-001",
        },
        "next_allowed_actions": ["confirm", "reject", "revise_input"],
    }

    assert canonical_json_dumps(actual) == canonical_json_dumps(expected)


def test_draft_ready_envelope_is_deterministic_for_identical_inputs() -> None:
    first = build_income_tax_draft_outcome_response(
        prompt_id="prompt-001",
        prompt_text="Compute income tax for resident employment lane in tax year 2023.",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        computation_output=_sample_computation_output(),
        finalized_output=_sample_finalized_output(),
        form_artifact_output=_sample_form_artifact_output(),
        report_output=_sample_report_output(),
        report_version_binding=_sample_report_binding(),
        submission_payload_output=_sample_submission_payload_output(),
    )
    second = build_income_tax_draft_outcome_response(
        prompt_id="prompt-001",
        prompt_text="Compute income tax for resident employment lane in tax year 2023.",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        computation_output=_sample_computation_output(),
        finalized_output=_sample_finalized_output(),
        form_artifact_output=_sample_form_artifact_output(),
        report_output=_sample_report_output(),
        report_version_binding=_sample_report_binding(),
        submission_payload_output=_sample_submission_payload_output(),
    )

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_missing_required_liability_field_is_rejected_deterministically() -> None:
    finalized_output = _sample_finalized_output()
    result_payload = cast(dict[str, object], finalized_output["result_payload"])
    liability_summary = cast(dict[str, object], result_payload["liability_summary"])
    del liability_summary["gross_tax_kes"]

    with pytest.raises(IncomeTaxDraftOutcomeContractError) as error_info:
        build_income_tax_draft_outcome_response(
            prompt_id="prompt-001",
            prompt_text="Compute income tax for resident employment lane in tax year 2023.",
            supported_lane_id="resident_employment_income_2023_07_01",
            historical_version_id="KIT-VER-20230701-A",
            tax_year=2023,
            computation_output=_sample_computation_output(),
            finalized_output=finalized_output,
            form_artifact_output=_sample_form_artifact_output(),
            report_output=_sample_report_output(),
            report_version_binding=_sample_report_binding(),
            submission_payload_output=_sample_submission_payload_output(),
        )

    assert canonical_json_dumps(error_info.value.payload()) == canonical_json_dumps(
        {
            "error_code": "draft_outcome_contract_mapping_failed",
            "message": (
                "Required string field 'gross_tax_kes' is missing in draft outcome contract."
            ),
            "reason": "missing_required_field",
            "details": {"field_name": "gross_tax_kes"},
        }
    )
