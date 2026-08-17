"""Run deterministic golden regression coverage for supported income-tax forms outputs."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from typing import TypedDict
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact

FORMS_GOLDEN_CASE_DIR = Path("eval/golden/forms")
TAX_CORE_GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
REQUIRED_FORMS_FIXTURE_IDS = {
    "income_tax_form_resident_employment_2021_01_01_case_001",
    "income_tax_form_non_resident_employment_2021_01_01_case_001",
    "income_tax_form_resident_employment_2023_07_01_case_001",
    "income_tax_form_non_resident_employment_2023_07_01_case_001",
    "income_tax_form_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001",
}


class FormsGoldenFixtureRequest(TypedDict):
    """Represent one deterministic forms golden fixture request block."""

    tax_core_fixture_file: str
    finalized_at: str


class FormsGoldenFixture(TypedDict):
    """Represent one deterministic forms golden fixture."""

    fixture_version: int
    fixture_id: str
    request: FormsGoldenFixtureRequest
    expected_output: dict[str, object]


def _load_all_forms_golden_fixtures() -> list[FormsGoldenFixture]:
    fixtures: list[FormsGoldenFixture] = []
    for path in sorted(FORMS_GOLDEN_CASE_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as fixture_file:
            fixtures.append(cast(FormsGoldenFixture, json.load(fixture_file)))
    return fixtures


def _load_tax_core_golden_fixture(path_name: str) -> dict[str, object]:
    path = TAX_CORE_GOLDEN_CASE_DIR / path_name
    with path.open("r", encoding="utf-8") as fixture_file:
        return cast(dict[str, object], json.load(fixture_file))


def _build_finalized_output(fixture: FormsGoldenFixture) -> dict[str, object]:
    request = fixture["request"]
    tax_core_fixture = _load_tax_core_golden_fixture(request["tax_core_fixture_file"])
    tax_core_fixture_id = cast(str, tax_core_fixture["fixture_id"])
    expected_tax_core_output = cast(dict[str, object], tax_core_fixture["expected_output"])

    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{tax_core_fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": request["finalized_at"],
        "finalized_audit_event_id": str(
            uuid5(NAMESPACE_URL, f"{tax_core_fixture_id}:finalized-audit")
        ),
        "tax_type": expected_tax_core_output["tax_type"],
        "regime_type": expected_tax_core_output["regime_type"],
        "tax_year": expected_tax_core_output["tax_year"],
        "rule_version": expected_tax_core_output["rule_version"],
        "input_hash": expected_tax_core_output["input_hash"],
        "result_payload": copy.deepcopy(expected_tax_core_output["result_payload"]),
    }


def _execute_forms_fixture(fixture: FormsGoldenFixture) -> dict[str, object]:
    finalized_output = _build_finalized_output(fixture)
    mapping_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    binding_output = bind_income_tax_form_version(mapping_output)
    artifact_output = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=mapping_output,
        form_version_binding=binding_output,
    )

    return {
        "form_mapping_output": mapping_output,
        "form_version_binding_output": binding_output,
        "form_artifact_output": artifact_output,
        "audit_evidence_output": {
            "mapping": cast(dict[str, object], mapping_output["audit_evidence"]),
            "binding": cast(dict[str, object], binding_output["audit_evidence"]),
            "artifact_generation": cast(dict[str, object], artifact_output["audit_evidence"]),
        },
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _assert_matches_golden(
    actual_output: dict[str, object],
    expected_output: dict[str, object],
) -> None:
    assert _canonical_json(actual_output) == _canonical_json(expected_output)


def _fixture_id(fixture: FormsGoldenFixture) -> str:
    return fixture["fixture_id"]


def test_forms_golden_corpus_covers_supported_governed_lanes_only() -> None:
    """Verify forms golden corpus covers exactly the currently supported governed lanes."""

    fixture_ids = {fixture["fixture_id"] for fixture in _load_all_forms_golden_fixtures()}

    assert fixture_ids == REQUIRED_FORMS_FIXTURE_IDS


def test_forms_golden_corpus_represents_current_historical_and_mixed_lanes() -> None:
    """Verify forms fixtures lock current, historical, and mixed-income behavior."""

    fixtures = _load_all_forms_golden_fixtures()
    historical_versions = {
        cast(
            str,
            cast(dict[str, object], fixture["expected_output"]["form_artifact_output"])[
                "historical_version_id"
            ],
        )
        for fixture in fixtures
    }
    supported_lane_ids = {
        cast(
            str,
            cast(dict[str, object], fixture["expected_output"]["form_artifact_output"])[
                "supported_lane_id"
            ],
        )
        for fixture in fixtures
    }

    assert historical_versions == {"KIT-VER-20210101-A", "KIT-VER-20230701-A"}
    assert "resident_employment_plus_qualifying_interest_2023_07_01" in supported_lane_ids


@pytest.mark.parametrize("fixture", _load_all_forms_golden_fixtures(), ids=_fixture_id)
def test_forms_golden_fixture_matches_exact_expected_output(fixture: FormsGoldenFixture) -> None:
    """Verify each forms fixture locks exact mapping, binding, artifact, and audit outputs."""

    actual_output = _execute_forms_fixture(fixture)

    assert set(fixture) == {"fixture_version", "fixture_id", "request", "expected_output"}
    assert fixture["fixture_version"] == 1
    _assert_matches_golden(actual_output, fixture["expected_output"])


@pytest.mark.parametrize("fixture", _load_all_forms_golden_fixtures(), ids=_fixture_id)
def test_forms_golden_fixture_is_deterministic_across_repeated_execution(
    fixture: FormsGoldenFixture,
) -> None:
    """Verify repeated forms generation remains byte-equivalent for each fixture."""

    first_output = _execute_forms_fixture(fixture)
    second_output = _execute_forms_fixture(fixture)

    assert _canonical_json(first_output) == _canonical_json(second_output)


@pytest.mark.parametrize("fixture", _load_all_forms_golden_fixtures(), ids=_fixture_id)
def test_forms_golden_fixture_detects_output_drift(fixture: FormsGoldenFixture) -> None:
    """Verify forms golden harness fails deterministically when output drifts."""

    actual_output = _execute_forms_fixture(fixture)
    drifted_expected_output = copy.deepcopy(fixture["expected_output"])
    artifact_output = cast(dict[str, object], drifted_expected_output["form_artifact_output"])
    artifact_output["artifact_id"] = "drifted-artifact-id"

    with pytest.raises(AssertionError):
        _assert_matches_golden(actual_output, drifted_expected_output)
