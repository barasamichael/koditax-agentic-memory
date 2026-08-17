"""Run deterministic end-to-end regression coverage for supported income-tax prompt flows."""

from __future__ import annotations

import copy
import json
from typing import cast
from typing import TypedDict
from typing import NotRequired
from pathlib import Path

import pytest

from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow

E2E_GOLDEN_CASE_DIR = Path("eval/golden/e2e")
REQUIRED_SUPPORTED_FIXTURE_IDS = {
    "income_tax_prompt_flow_resident_employment_2021_01_01_case_001",
    "income_tax_prompt_flow_non_resident_employment_2021_01_01_case_001",
    "income_tax_prompt_flow_resident_employment_2023_07_01_case_001",
    "income_tax_prompt_flow_non_resident_employment_2023_07_01_case_001",
    "income_tax_prompt_flow_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001",
}
REQUIRED_NEGATIVE_FIXTURE_IDS = {
    "income_tax_prompt_flow_unsupported_scope_case_001",
    "income_tax_prompt_flow_invalid_prompt_input_case_001",
}


class PromptFlowFixtureRequest(TypedDict):
    """Represent one prompt-flow fixture request block."""

    prompt_text: str


class PromptFlowFixtureError(TypedDict):
    """Represent one expected prompt-flow failure payload."""

    reason: str
    message: str
    details: dict[str, object]


class PromptFlowFixture(TypedDict):
    """Represent one deterministic end-to-end prompt-flow fixture."""

    fixture_version: int
    fixture_id: str
    request: PromptFlowFixtureRequest
    expected_output: NotRequired[dict[str, object]]
    expected_error: NotRequired[PromptFlowFixtureError]


def _load_all_e2e_fixtures() -> list[PromptFlowFixture]:
    fixtures: list[PromptFlowFixture] = []
    for path in sorted(E2E_GOLDEN_CASE_DIR.glob("income_tax_prompt_flow_*.json")):
        fixtures.append(json.loads(path.read_text(encoding="utf-8")))
    return fixtures


def _supported_fixtures() -> list[PromptFlowFixture]:
    return [fixture for fixture in _load_all_e2e_fixtures() if "expected_output" in fixture]


def _negative_fixtures() -> list[PromptFlowFixture]:
    return [fixture for fixture in _load_all_e2e_fixtures() if "expected_error" in fixture]


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _assert_matches_golden(actual: object, expected: object) -> None:
    assert _canonical_json(actual) == _canonical_json(expected)


def _fixture_id(fixture: PromptFlowFixture) -> str:
    return fixture["fixture_id"]


def _expected_output(fixture: PromptFlowFixture) -> dict[str, object]:
    value = fixture.get("expected_output")
    if not isinstance(value, dict):
        raise AssertionError(f"Fixture '{fixture['fixture_id']}' is missing expected_output")
    return value


def _expected_error(fixture: PromptFlowFixture) -> PromptFlowFixtureError:
    value = fixture.get("expected_error")
    if not isinstance(value, dict):
        raise AssertionError(f"Fixture '{fixture['fixture_id']}' is missing expected_error")
    return value


def test_e2e_prompt_regression_corpus_covers_supported_governed_lanes() -> None:
    """Verify e2e prompt fixtures cover all currently supported governed lanes."""

    fixture_ids = {fixture["fixture_id"] for fixture in _load_all_e2e_fixtures()}

    assert REQUIRED_SUPPORTED_FIXTURE_IDS.issubset(fixture_ids)
    assert REQUIRED_NEGATIVE_FIXTURE_IDS.issubset(fixture_ids)


def test_e2e_prompt_regression_represents_current_historical_and_mixed_lanes() -> None:
    """Verify e2e prompt fixtures lock current, historical, and mixed-income contexts."""

    fixtures = _supported_fixtures()
    draft_contexts = [
        cast(dict[str, object], _expected_output(fixture)["draft_context"]) for fixture in fixtures
    ]
    historical_versions = {
        cast(str, draft_context["historical_version_id"]) for draft_context in draft_contexts
    }
    supported_lanes = {
        cast(str, draft_context["supported_lane_id"]) for draft_context in draft_contexts
    }

    assert historical_versions == {"KIT-VER-20210101-A", "KIT-VER-20230701-A"}
    assert "resident_employment_plus_qualifying_interest_2023_07_01" in supported_lanes


@pytest.mark.parametrize("fixture", _supported_fixtures(), ids=_fixture_id)
def test_e2e_prompt_supported_fixture_matches_exact_output(fixture: PromptFlowFixture) -> None:
    """Verify supported prompt fixtures lock exact deterministic end-to-end output."""

    request = fixture["request"]
    prompt_text = request["prompt_text"]
    actual_output = execute_income_tax_prompt_flow(prompt_text)

    assert fixture["fixture_version"] == 1
    _assert_matches_golden(actual_output, _expected_output(fixture))


@pytest.mark.parametrize("fixture", _negative_fixtures(), ids=_fixture_id)
def test_e2e_prompt_negative_fixture_matches_deterministic_failure(
    fixture: PromptFlowFixture,
) -> None:
    """Verify unsupported prompt fixtures fail with deterministic structured reasons."""

    request = fixture["request"]
    prompt_text = request["prompt_text"]

    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(prompt_text)

    expected_error = _expected_error(fixture)
    assert error_info.value.reason == expected_error["reason"]
    assert error_info.value.message == expected_error["message"]
    _assert_matches_golden(error_info.value.details(), expected_error["details"])


@pytest.mark.parametrize("fixture", _supported_fixtures(), ids=_fixture_id)
def test_e2e_prompt_supported_fixture_is_deterministic_on_repeated_run(
    fixture: PromptFlowFixture,
) -> None:
    """Verify repeated supported prompt execution remains canonically equivalent."""

    prompt_text = fixture["request"]["prompt_text"]
    first = execute_income_tax_prompt_flow(prompt_text)
    second = execute_income_tax_prompt_flow(prompt_text)

    assert _canonical_json(second) == _canonical_json(first)


@pytest.mark.parametrize("fixture", _supported_fixtures(), ids=_fixture_id)
def test_e2e_prompt_supported_fixture_detects_drift(fixture: PromptFlowFixture) -> None:
    """Verify the e2e regression harness fails deterministically on output drift."""

    prompt_text = fixture["request"]["prompt_text"]
    actual_output = execute_income_tax_prompt_flow(prompt_text)
    drifted_expected = copy.deepcopy(_expected_output(fixture))
    drifted_expected["status"] = "drifted"

    with pytest.raises(AssertionError):
        _assert_matches_golden(actual_output, drifted_expected)
