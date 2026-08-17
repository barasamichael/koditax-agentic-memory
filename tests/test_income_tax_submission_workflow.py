"""Verify deterministic internal submission workflow handling for supported income-tax lanes."""

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
from services.forms.app.income_tax.submission_workflow import IncomeTaxSubmissionWorkflowError
from services.forms.app.income_tax.submission_workflow import advance_income_tax_submission_workflow
from services.forms.app.income_tax.submission_workflow import (
    initialize_income_tax_submission_workflow,
)
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.report_version_binding import bind_income_tax_report_version
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.forms.app.income_tax.submission_payload_construction import (
    construct_income_tax_submission_payload,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T22:00:00+03:00"


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
def test_supported_lanes_initialize_submission_workflow_deterministically(
    fixture_name: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    payload_output = _build_submission_payload_output(fixture_name)

    workflow_record = initialize_income_tax_submission_workflow(
        submission_payload_output=payload_output
    )
    status_history = _as_list_of_objects(workflow_record["status_history"])
    first_transition = _as_object(status_history[0])

    assert workflow_record["workflow_type"] == "income_tax_submission_workflow"
    assert workflow_record["workflow_version"] == "income_tax_submission_workflow_v1"
    assert workflow_record["submission_payload_id"] == payload_output["payload_id"]
    assert workflow_record["computation_id"] == payload_output["computation_id"]
    assert workflow_record["supported_lane_id"] == expected_lane_id
    assert workflow_record["historical_version_id"] == expected_historical_version_id
    assert workflow_record["tax_year"] == payload_output["tax_year"]
    assert workflow_record["current_status"] == "prepared"
    assert len(status_history) == 1
    assert first_transition["from_status"] is None
    assert first_transition["to_status"] == "prepared"
    assert first_transition["transition_reason"] == "workflow_initialized"


def test_workflow_advances_through_supported_internal_states() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    prepared = initialize_income_tax_submission_workflow(submission_payload_output=payload_output)

    ready = advance_income_tax_submission_workflow(
        workflow_record=prepared,
        target_status="ready_for_submission",
    )
    submitted = advance_income_tax_submission_workflow(
        workflow_record=ready,
        target_status="submitted_internal",
    )

    ready_history = _as_list_of_objects(ready["status_history"])
    submitted_history = _as_list_of_objects(submitted["status_history"])

    assert ready["current_status"] == "ready_for_submission"
    assert len(ready_history) == 2
    assert _as_object(ready_history[-1])["transition_reason"] == "lineage_validated"

    assert submitted["current_status"] == "submitted_internal"
    assert len(submitted_history) == 3
    assert _as_object(submitted_history[-1])["transition_reason"] == "internal_submission_recorded"


def test_workflow_rejects_invalid_transition() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    prepared = initialize_income_tax_submission_workflow(submission_payload_output=payload_output)

    with pytest.raises(IncomeTaxSubmissionWorkflowError) as error_info:
        advance_income_tax_submission_workflow(
            workflow_record=prepared,
            target_status="submitted_internal",
        )

    assert error_info.value.reason == "invalid_status_transition"


def test_workflow_rejects_invalid_status_value() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    prepared = initialize_income_tax_submission_workflow(submission_payload_output=payload_output)

    with pytest.raises(IncomeTaxSubmissionWorkflowError) as error_info:
        advance_income_tax_submission_workflow(
            workflow_record=prepared,
            target_status="external_submitted",
        )

    assert error_info.value.reason == "invalid_workflow_status"


def test_workflow_rejects_malformed_payload_input() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    del payload_output["lineage"]

    with pytest.raises(IncomeTaxSubmissionWorkflowError) as error_info:
        initialize_income_tax_submission_workflow(submission_payload_output=payload_output)

    assert error_info.value.reason == "missing_required_field"
    assert error_info.value.details()["field_name"] == "lineage"


def test_workflow_rejects_unsupported_scope() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    payload_output["supported_lane_id"] = "resident_employment_income_2022_01_01"

    with pytest.raises(IncomeTaxSubmissionWorkflowError) as error_info:
        initialize_income_tax_submission_workflow(submission_payload_output=payload_output)

    assert error_info.value.reason == "unsupported_workflow_scope"


def test_workflow_transition_is_idempotent_for_same_target_state() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_non_resident_employment_2023_07_01_case_001.json"
    )
    prepared = initialize_income_tax_submission_workflow(submission_payload_output=payload_output)
    ready_once = advance_income_tax_submission_workflow(
        workflow_record=prepared,
        target_status="ready_for_submission",
    )
    ready_twice = advance_income_tax_submission_workflow(
        workflow_record=copy.deepcopy(ready_once),
        target_status="ready_for_submission",
    )

    assert ready_twice == ready_once


def test_workflow_initialization_is_deterministic_for_same_payload() -> None:
    payload_output = _build_submission_payload_output(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )

    first = initialize_income_tax_submission_workflow(
        submission_payload_output=copy.deepcopy(payload_output)
    )
    second = initialize_income_tax_submission_workflow(
        submission_payload_output=copy.deepcopy(payload_output)
    )

    assert second == first


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


def _as_list_of_objects(value: object) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], value)
