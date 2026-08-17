"""Verify deterministic audit coverage for supported income-tax form generation actions."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import IncomeTaxFormMappingError
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_audit_coverage import (
    build_income_tax_form_failure_audit_evidence,
)
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.form_version_binding import IncomeTaxFormVersionBindingError
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.forms.app.income_tax.form_artifact_generation import (
    IncomeTaxFormArtifactGenerationError,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T16:20:00+03:00"


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
def test_form_generation_actions_emit_lineage_bound_audit_evidence(
    fixture_name: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    form_artifact = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )

    mapping_audit = _as_object(form_ready_output["audit_evidence"])
    binding_audit = _as_object(form_version_binding["audit_evidence"])
    artifact_audit = _as_object(form_artifact["audit_evidence"])

    assert mapping_audit["action"] == "mapping"
    assert mapping_audit["action_status"] == "mapped"
    assert mapping_audit["computation_id"] == finalized_output["computation_id"]
    assert mapping_audit["finalized_audit_event_id"] == finalized_output["finalized_audit_event_id"]
    assert mapping_audit["supported_lane_id"] == expected_lane_id
    assert mapping_audit["historical_version_id"] == expected_historical_version_id
    assert mapping_audit["artifact_id"] is None

    assert binding_audit["action"] == "binding"
    assert binding_audit["action_status"] == "bound"
    assert binding_audit["supported_lane_id"] == expected_lane_id
    assert binding_audit["historical_version_id"] == expected_historical_version_id
    assert binding_audit["form_version_id"] == form_version_binding["form_version_id"]
    assert binding_audit["artifact_id"] is None

    assert artifact_audit["action"] == "artifact_generation"
    assert artifact_audit["action_status"] == "generated"
    assert artifact_audit["supported_lane_id"] == expected_lane_id
    assert artifact_audit["historical_version_id"] == expected_historical_version_id
    assert artifact_audit["form_version_id"] == form_version_binding["form_version_id"]
    assert artifact_audit["artifact_id"] == form_artifact["artifact_id"]


def test_form_generation_audit_evidence_is_deterministic_for_same_input() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    first_form_ready = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    first_binding = bind_income_tax_form_version(copy.deepcopy(first_form_ready))
    first_artifact = generate_income_tax_form_artifact(
        finalized_output=copy.deepcopy(finalized_output),
        form_ready_output=copy.deepcopy(first_form_ready),
        form_version_binding=copy.deepcopy(first_binding),
    )

    second_form_ready = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    second_binding = bind_income_tax_form_version(copy.deepcopy(second_form_ready))
    second_artifact = generate_income_tax_form_artifact(
        finalized_output=copy.deepcopy(finalized_output),
        form_ready_output=copy.deepcopy(second_form_ready),
        form_version_binding=copy.deepcopy(second_binding),
    )

    assert second_form_ready["audit_evidence"] == first_form_ready["audit_evidence"]
    assert second_binding["audit_evidence"] == first_binding["audit_evidence"]
    assert second_artifact["audit_evidence"] == first_artifact["audit_evidence"]


def test_non_finalized_mapping_failure_produces_deterministic_failed_audit_evidence() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    finalized_output["finalization_status"] = "draft"

    with pytest.raises(IncomeTaxFormMappingError) as error_info:
        map_finalized_income_tax_output_to_form_ready(finalized_output)

    first_failure_audit = build_income_tax_form_failure_audit_evidence(
        action="mapping",
        error_reason=error_info.value.reason,
        error_message=error_info.value.message,
        error_details=error_info.value.details(),
        finalized_output=finalized_output,
    )
    second_failure_audit = build_income_tax_form_failure_audit_evidence(
        action="mapping",
        error_reason=error_info.value.reason,
        error_message=error_info.value.message,
        error_details=error_info.value.details(),
        finalized_output=finalized_output,
    )

    assert first_failure_audit["action"] == "mapping"
    assert first_failure_audit["action_status"] == "failed"
    assert first_failure_audit["computation_id"] == finalized_output["computation_id"]
    assert first_failure_audit["error"] == {
        "reason": "computation_not_finalized",
        "message": "Form mapping requires a finalized computation output.",
        "details": {
            "reason": "computation_not_finalized",
            "finalization_status": "draft",
        },
    }
    assert second_failure_audit == first_failure_audit


def test_unsupported_binding_failure_produces_deterministic_failed_audit_evidence() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_ready_version_identity = _as_object(form_ready_output["version_identity"])
    form_ready_version_identity["historical_version_id"] = "KIT-VER-20200901-A"
    form_ready_output["version_identity"] = form_ready_version_identity

    with pytest.raises(IncomeTaxFormVersionBindingError) as error_info:
        bind_income_tax_form_version(form_ready_output)

    first_failure_audit = build_income_tax_form_failure_audit_evidence(
        action="binding",
        error_reason=error_info.value.reason,
        error_message=error_info.value.message,
        error_details=error_info.value.details(),
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
    )
    second_failure_audit = build_income_tax_form_failure_audit_evidence(
        action="binding",
        error_reason=error_info.value.reason,
        error_message=error_info.value.message,
        error_details=error_info.value.details(),
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
    )

    assert first_failure_audit["action"] == "binding"
    assert first_failure_audit["action_status"] == "failed"
    assert first_failure_audit["supported_lane_id"] == "resident_employment_income_2023_07_01"
    assert first_failure_audit["historical_version_id"] == "KIT-VER-20200901-A"
    assert first_failure_audit["error"] == {
        "reason": "unsupported_form_version_binding",
        "message": "No governed form-version binding exists for this supported lane context.",
        "details": {
            "reason": "unsupported_form_version_binding",
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20200901-A",
            "tax_year": 2023,
        },
    }
    assert second_failure_audit == first_failure_audit


def test_non_finalized_artifact_failure_produces_deterministic_failed_audit_evidence() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    finalized_output["finalization_status"] = "draft"

    with pytest.raises(IncomeTaxFormArtifactGenerationError) as error_info:
        generate_income_tax_form_artifact(
            finalized_output=finalized_output,
            form_ready_output=form_ready_output,
            form_version_binding=form_version_binding,
        )

    failure_audit = build_income_tax_form_failure_audit_evidence(
        action="artifact_generation",
        error_reason=error_info.value.reason,
        error_message=error_info.value.message,
        error_details=error_info.value.details(),
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )

    assert failure_audit["action"] == "artifact_generation"
    assert failure_audit["action_status"] == "failed"
    assert failure_audit["supported_lane_id"] == "non_resident_employment_income_2021_01_01"
    assert failure_audit["historical_version_id"] == "KIT-VER-20210101-A"
    assert failure_audit["error"] == {
        "reason": "computation_not_finalized",
        "message": "Form artifacts may only be generated from finalized computations.",
        "details": {
            "reason": "computation_not_finalized",
            "finalization_status": "draft",
        },
    }


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
