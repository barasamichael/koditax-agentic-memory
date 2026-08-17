"""Verify deterministic immutable form-artifact generation for supported income-tax lanes."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.forms.app.income_tax.form_artifact_generation import (
    IncomeTaxFormArtifactGenerationError,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T10:00:00+03:00"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "income_tax_resident_employment_2021_01_01_case_001.json",
        "income_tax_non_resident_employment_2021_01_01_case_001.json",
        "income_tax_resident_employment_2023_07_01_case_001.json",
        "income_tax_non_resident_employment_2023_07_01_case_001.json",
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json",
    ],
)
def test_supported_lanes_generate_deterministic_form_artifact(
    fixture_name: str,
) -> None:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)

    artifact = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )

    assert artifact["generation_status"] == "generated"
    assert artifact["form_type"] == "income_tax_return"
    assert artifact["computation_id"] == finalized_output["computation_id"]
    assert artifact["tax_year"] == finalized_output["tax_year"]
    assert artifact["supported_lane_id"] == form_version_binding["supported_lane_id"]
    assert artifact["historical_version_id"] == form_version_binding["historical_version_id"]
    assert artifact["content_sha256"]


def test_mixed_income_lane_generates_expected_artifact_payload_shape() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)

    artifact = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )
    generated_content = _as_object(artifact["generated_content_payload"])
    header = _as_object(generated_content["header"])
    form_fields = _as_object(generated_content["form_fields"])

    assert header["form_version_id"] == "ITX-FORM-20230701-RES-EMP-QINT-V1"
    assert form_fields["investment_income_kes"] == "120000.00"
    assert form_fields["investment_final_tax_amount_kes"] == "18000.00"


def test_non_finalized_input_fails_deterministically() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
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

    assert error_info.value.reason == "computation_not_finalized"


def test_unsupported_binding_mismatch_fails_deterministically() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    form_version_binding["supported_lane_id"] = "resident_employment_income_2021_01_01"

    with pytest.raises(IncomeTaxFormArtifactGenerationError) as error_info:
        generate_income_tax_form_artifact(
            finalized_output=finalized_output,
            form_ready_output=form_ready_output,
            form_version_binding=form_version_binding,
        )

    assert error_info.value.reason == "lineage_mismatch"


def test_artifact_generation_is_deterministic_for_same_inputs() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)

    first = generate_income_tax_form_artifact(
        finalized_output=copy.deepcopy(finalized_output),
        form_ready_output=copy.deepcopy(form_ready_output),
        form_version_binding=copy.deepcopy(form_version_binding),
    )
    second = generate_income_tax_form_artifact(
        finalized_output=copy.deepcopy(finalized_output),
        form_ready_output=copy.deepcopy(form_ready_output),
        form_version_binding=copy.deepcopy(form_version_binding),
    )

    assert second == first


def test_scope_guard_rejects_non_income_tax_outputs() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    finalized_output["tax_type"] = "vat"

    with pytest.raises(IncomeTaxFormArtifactGenerationError) as error_info:
        generate_income_tax_form_artifact(
            finalized_output=finalized_output,
            form_ready_output=form_ready_output,
            form_version_binding=form_version_binding,
        )

    assert error_info.value.reason == "unsupported_tax_type"


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
