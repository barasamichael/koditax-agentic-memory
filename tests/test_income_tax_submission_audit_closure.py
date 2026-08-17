"""Verify deterministic submission audit closure and immutability enforcement."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.report_generation import generate_income_tax_report
from services.forms.app.income_tax.submission_workflow import advance_income_tax_submission_workflow
from services.forms.app.income_tax.submission_workflow import (
    initialize_income_tax_submission_workflow,
)
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.report_version_binding import bind_income_tax_report_version
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.forms.app.income_tax.submission_audit_closure import (
    close_income_tax_submission_workflow,
)
from services.forms.app.income_tax.submission_audit_closure import (
    IncomeTaxSubmissionAuditClosureError,
)
from services.forms.app.income_tax.submission_audit_closure import (
    enforce_income_tax_submission_closure_immutability,
)
from services.forms.app.income_tax.submission_payload_construction import (
    construct_income_tax_submission_payload,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T23:00:00+03:00"


@pytest.mark.parametrize(
    ("fixture_name", "expected_lane_id", "expected_historical_version_id"),
    [
        (
            "income_tax_resident_employment_2021_01_01_case_001.json",
            "resident_employment_income_2021_01_01",
            "KIT-VER-20210101-A",
        ),
        (
            "income_tax_non_resident_employment_2021_01_01_case_001.json",
            "non_resident_employment_income_2021_01_01",
            "KIT-VER-20210101-A",
        ),
        (
            "income_tax_resident_employment_2023_07_01_case_001.json",
            "resident_employment_income_2023_07_01",
            "KIT-VER-20230701-A",
        ),
        (
            "income_tax_non_resident_employment_2023_07_01_case_001.json",
            "non_resident_employment_income_2023_07_01",
            "KIT-VER-20230701-A",
        ),
        (
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json",
            "resident_employment_plus_qualifying_interest_2023_07_01",
            "KIT-VER-20230701-A",
        ),
    ],
)
def test_supported_lanes_close_submission_workflow_deterministically(
    fixture_name: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    submitted_workflow = _build_submitted_workflow_record(fixture_name)

    closure = close_income_tax_submission_workflow(workflow_record=submitted_workflow)

    immutable_fields = _as_object(closure["immutable_identity_fields"])
    closure_lineage = _as_object(closure["lineage"])
    closure_audit = _as_object(closure["closure_audit_evidence"])
    workflow_lineage = _as_object(submitted_workflow["lineage"])
    workflow_audit = _as_object(submitted_workflow["audit_evidence"])

    assert closure["closure_status"] == "closed_internal"
    assert closure["closure_type"] == "income_tax_submission_audit_closure"
    assert closure["closure_version"] == "income_tax_submission_audit_closure_v1"
    assert closure["workflow_record_id"] == submitted_workflow["workflow_record_id"]
    assert closure["submission_payload_id"] == submitted_workflow["submission_payload_id"]
    assert closure["report_id"] == submitted_workflow["report_id"]
    assert closure["form_artifact_id"] == submitted_workflow["form_artifact_id"]
    assert closure["computation_id"] == submitted_workflow["computation_id"]
    assert closure["supported_lane_id"] == expected_lane_id
    assert closure["historical_version_id"] == expected_historical_version_id
    assert closure["tax_year"] == submitted_workflow["tax_year"]
    assert closure["final_internal_status"] == "submitted_internal"
    assert closure["external_confirmation_status"] == "not_available_in_scope"

    assert immutable_fields["closure_record_id"] == closure["closure_record_id"]
    assert immutable_fields["workflow_record_id"] == closure["workflow_record_id"]
    assert immutable_fields["submission_payload_id"] == closure["submission_payload_id"]
    assert immutable_fields["report_id"] == closure["report_id"]
    assert immutable_fields["form_artifact_id"] == closure["form_artifact_id"]
    assert immutable_fields["computation_id"] == closure["computation_id"]
    assert immutable_fields["supported_lane_id"] == closure["supported_lane_id"]
    assert immutable_fields["historical_version_id"] == closure["historical_version_id"]
    assert immutable_fields["tax_year"] == closure["tax_year"]
    assert immutable_fields["final_internal_status"] == closure["final_internal_status"]

    assert (
        closure_lineage["artifact_audit_evidence_id"]
        == workflow_lineage["artifact_audit_evidence_id"]
    )
    assert (
        closure_lineage["report_audit_evidence_id"] == workflow_lineage["report_audit_evidence_id"]
    )
    assert (
        closure_lineage["payload_audit_evidence_id"]
        == workflow_lineage["payload_audit_evidence_id"]
    )
    assert closure_lineage["workflow_audit_evidence_id"] == workflow_audit["audit_evidence_id"]

    assert closure_audit["action"] == "submission_workflow_closure"
    assert closure_audit["action_status"] == "closed_internal"
    assert closure_audit["closure_record_id"] == closure["closure_record_id"]
    assert closure_audit["submission_payload_id"] == closure["submission_payload_id"]
    assert closure_audit["report_id"] == closure["report_id"]
    assert closure_audit["form_artifact_id"] == closure["form_artifact_id"]
    assert closure_audit["computation_id"] == closure["computation_id"]


def test_submission_closure_rejects_non_terminal_workflow_status() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    prepared_workflow = initialize_income_tax_submission_workflow(
        submission_payload_output=payload_output
    )

    with pytest.raises(IncomeTaxSubmissionAuditClosureError) as error_info:
        close_income_tax_submission_workflow(workflow_record=prepared_workflow)

    assert error_info.value.reason == "workflow_not_ready_for_closure"


def test_submission_closure_rejects_malformed_workflow_record() -> None:
    submitted_workflow = _build_submitted_workflow_record(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    del submitted_workflow["lineage"]

    with pytest.raises(IncomeTaxSubmissionAuditClosureError) as error_info:
        close_income_tax_submission_workflow(workflow_record=submitted_workflow)

    assert error_info.value.reason == "missing_required_field"
    assert error_info.value.details()["field_name"] == "lineage"


def test_submission_closure_rejects_unsupported_scope() -> None:
    submitted_workflow = _build_submitted_workflow_record(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    submitted_workflow["supported_lane_id"] = "resident_employment_income_2022_01_01"

    with pytest.raises(IncomeTaxSubmissionAuditClosureError) as error_info:
        close_income_tax_submission_workflow(workflow_record=submitted_workflow)

    assert error_info.value.reason == "unsupported_closure_scope"


def test_closure_immutability_rejects_illegal_post_closure_mutation() -> None:
    submitted_workflow = _build_submitted_workflow_record(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )
    baseline = close_income_tax_submission_workflow(workflow_record=submitted_workflow)
    mutated = copy.deepcopy(baseline)
    mutated["submission_payload_id"] = "tampered_submission_payload_id"
    immutable_fields = _as_object(mutated["immutable_identity_fields"])
    immutable_fields["submission_payload_id"] = "tampered_submission_payload_id"
    mutated["immutable_identity_fields"] = immutable_fields

    with pytest.raises(IncomeTaxSubmissionAuditClosureError) as error_info:
        enforce_income_tax_submission_closure_immutability(
            baseline_closure_output=baseline,
            candidate_closure_output=mutated,
        )

    assert error_info.value.reason == "illegal_post_closure_mutation"
    assert "submission_payload_id" in cast(list[str], error_info.value.details()["mutated_fields"])


def test_closure_rejects_unsupported_external_confirmation_assumptions() -> None:
    submitted_workflow = _build_submitted_workflow_record(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    baseline = close_income_tax_submission_workflow(workflow_record=submitted_workflow)
    candidate = copy.deepcopy(baseline)
    candidate["external_confirmation_status"] = "regulator_confirmed"

    with pytest.raises(IncomeTaxSubmissionAuditClosureError) as error_info:
        enforce_income_tax_submission_closure_immutability(
            baseline_closure_output=baseline,
            candidate_closure_output=candidate,
        )

    assert error_info.value.reason == "unsupported_external_confirmation_status"


def test_submission_closure_is_deterministic_for_same_workflow_record() -> None:
    submitted_workflow = _build_submitted_workflow_record(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )

    first = close_income_tax_submission_workflow(workflow_record=copy.deepcopy(submitted_workflow))
    second = close_income_tax_submission_workflow(workflow_record=copy.deepcopy(submitted_workflow))

    assert second == first


def test_closure_immutability_accepts_identical_candidate() -> None:
    submitted_workflow = _build_submitted_workflow_record(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    baseline = close_income_tax_submission_workflow(workflow_record=submitted_workflow)
    candidate = copy.deepcopy(baseline)

    validated = enforce_income_tax_submission_closure_immutability(
        baseline_closure_output=baseline,
        candidate_closure_output=candidate,
    )

    assert validated == candidate


def _build_submitted_workflow_record(fixture_name: str) -> dict[str, object]:
    payload_output = _build_submission_payload_output(fixture_name)
    prepared = initialize_income_tax_submission_workflow(submission_payload_output=payload_output)
    ready = advance_income_tax_submission_workflow(
        workflow_record=prepared,
        target_status="ready_for_submission",
    )
    return advance_income_tax_submission_workflow(
        workflow_record=ready,
        target_status="submitted_internal",
    )


def _build_submission_payload_output(fixture_name: str) -> dict[str, object]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    form_artifact_output = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )
    report_output = generate_income_tax_report(form_artifact_output=form_artifact_output)
    report_binding = bind_income_tax_report_version(report_output)
    return construct_income_tax_submission_payload(
        report_output=report_output,
        report_version_binding=report_binding,
    )


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


def _as_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)
