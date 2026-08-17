"""Run deterministic golden regression coverage for supported tax_core fixtures."""

from __future__ import annotations

import copy
import json
from typing import cast
from typing import TypedDict
from pathlib import Path

import pytest

from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
REQUIRED_GOVERNED_FIXTURE_IDS = {
    "income_tax_resident_employment_2023_07_01_case_001",
    "income_tax_non_resident_employment_2023_07_01_case_001",
    "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001",
    "income_tax_resident_employment_2021_01_01_case_001",
    "income_tax_non_resident_employment_2021_01_01_case_001",
}


class GoldenCaseFixture(TypedDict):
    """Represent one canonical golden regression fixture."""

    fixture_version: int
    fixture_id: str
    request: dict[str, object]
    expected_output: dict[str, object]


def _load_all_golden_fixtures() -> list[GoldenCaseFixture]:
    fixtures: list[GoldenCaseFixture] = []
    for path in sorted(GOLDEN_CASE_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as fixture_file:
            fixture = json.load(fixture_file)
        fixtures.append(cast(GoldenCaseFixture, fixture))
    return fixtures


def _load_fixture_by_id(fixture_id: str) -> GoldenCaseFixture:
    for fixture in _load_all_golden_fixtures():
        if fixture["fixture_id"] == fixture_id:
            return fixture
    raise AssertionError(f"Missing golden fixture: {fixture_id}")


def _execute_fixture_request(fixture: GoldenCaseFixture) -> dict[str, object]:
    request = ComputationExecutionRequest.model_validate(fixture["request"])
    return execute_computation(request).model_dump(mode="json")


def _assert_matches_golden(
    actual_output: dict[str, object],
    expected_output: dict[str, object],
) -> None:
    assert _canonical_json(actual_output) == _canonical_json(expected_output)


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _fixture_id(fixture: GoldenCaseFixture) -> str:
    return fixture["fixture_id"]


def test_income_tax_golden_corpus_covers_all_supported_governed_lanes() -> None:
    """Verify the golden corpus includes every currently supported governed lane."""

    fixtures = _load_all_golden_fixtures()
    fixture_ids = {fixture["fixture_id"] for fixture in fixtures}

    assert REQUIRED_GOVERNED_FIXTURE_IDS.issubset(fixture_ids)


def test_income_tax_golden_corpus_represents_historical_and_current_windows() -> None:
    """Verify golden fixtures cover both current and historical implemented windows."""

    fixtures = _load_all_golden_fixtures()
    version_ids = {
        cast(
            str,
            cast(
                dict[str, object],
                cast(dict[str, object], fixture["expected_output"]["result_payload"])[
                    "version_identity"
                ],
            )["historical_version_id"],
        )
        for fixture in fixtures
        if fixture["fixture_id"] in REQUIRED_GOVERNED_FIXTURE_IDS
    }

    assert version_ids == {"KIT-VER-20210101-A", "KIT-VER-20230701-A"}


@pytest.mark.parametrize("fixture", _load_all_golden_fixtures(), ids=_fixture_id)
def test_golden_fixture_matches_exact_expected_output(fixture: GoldenCaseFixture) -> None:
    """Verify every fixture locks exact deterministic output."""

    actual_output = _execute_fixture_request(fixture)

    assert set(fixture) == {"fixture_version", "fixture_id", "request", "expected_output"}
    assert fixture["fixture_version"] == 1
    _assert_matches_golden(
        actual_output=actual_output,
        expected_output=fixture["expected_output"],
    )


@pytest.mark.parametrize("fixture", _load_all_golden_fixtures(), ids=_fixture_id)
def test_golden_fixture_is_deterministic_across_repeated_execution(
    fixture: GoldenCaseFixture,
) -> None:
    """Verify repeated execution remains byte-equivalent for every fixture."""

    first_output = _execute_fixture_request(fixture)
    second_output = _execute_fixture_request(fixture)

    assert _canonical_json(first_output) == _canonical_json(second_output)


@pytest.mark.parametrize("fixture", _load_all_golden_fixtures(), ids=_fixture_id)
def test_golden_fixture_detects_output_drift(fixture: GoldenCaseFixture) -> None:
    """Verify the harness fails when any fixture output drifts."""

    actual_output = _execute_fixture_request(fixture)
    drifted_expected_output = copy.deepcopy(fixture["expected_output"])
    drifted_payload = cast(dict[str, object], drifted_expected_output["result_payload"])
    liability_summary = drifted_payload.get("liability_summary")
    if isinstance(liability_summary, dict) and "net_income_tax_due_kes" in liability_summary:
        liability_summary["net_income_tax_due_kes"] = "999999.99"
    else:
        drifted_payload["fixture_drift_marker"] = "drifted"

    with pytest.raises(AssertionError):
        _assert_matches_golden(
            actual_output=actual_output,
            expected_output=drifted_expected_output,
        )


def test_stub_seed_fixture_locks_replay_relevant_fields() -> None:
    """Verify the original stub seed still locks its replay-relevant deterministic fields."""

    fixture = _load_fixture_by_id("income_tax_v1_case_001")
    expected_output = fixture["expected_output"]
    result_payload = cast(dict[str, object], expected_output["result_payload"])

    assert expected_output["tax_year"] == 2025
    assert expected_output["rule_version"] == "v1"
    assert expected_output["input_hash"] == (
        "c37c2ca4699b84e03fc99aed8da097d1202941a4f5567d215ce466d63114674f"
    )
    assert result_payload == {
        "binding_id": "income_tax_default_v1_2025",
        "execution_mode": "deterministic_stub",
        "input_hash": "c37c2ca4699b84e03fc99aed8da097d1202941a4f5567d215ce466d63114674f",
        "normalized_input": {
            "deductions": {
                "charity": 2000,
                "retirement": 5000,
            },
            "dependents": ["child_1", "child_2"],
            "income": 125000,
            "metadata": {
                "filing_status": "single",
                "resident": True,
            },
        },
        "rule_version": "v1",
    }
