"""Verify deterministic governed health-contribution validation findings."""

from __future__ import annotations

import copy
import json
from uuid import uuid4
from typing import cast
from typing import TypedDict
from pathlib import Path

import pytest

from services.tax_core.app.engine.execution_contract import PersistedValidationSource
from services.tax_core.app.rules.health_contribution.validation_catalog import (
    derive_health_contribution_validation_findings,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
_REPLAY_CONTEXT_KEY = "_kodi_replay_context"


class GoldenFixture(TypedDict):
    fixture_version: int
    fixture_id: str
    request: dict[str, object]
    expected_output: dict[str, object]


@pytest.mark.parametrize(
    ("fixture_id", "expected_transition_route"),
    [
        ("health_contribution_nhif_legacy_case_001", False),
        ("health_contribution_sha_shif_case_001", False),
        ("health_contribution_sha_shif_2024_non_salaried_case_001", False),
        ("health_contribution_transition_boundary_sha_case_001", True),
    ],
)
def test_supported_health_lanes_emit_governed_validation_findings(
    fixture_id: str,
    expected_transition_route: bool,
) -> None:
    """Verify supported governed health lanes return deterministic catalog findings."""

    persisted_source = _build_persisted_source(fixture_id)

    findings = derive_health_contribution_validation_findings(persisted_source)

    assert [finding.code for finding in findings] == [
        "health_contribution_supported_lane_detected",
        "health_contribution_version_binding_consistent",
        "health_contribution_effective_window_consistent",
        "health_contribution_summary_consistent",
    ]
    assert [finding.severity for finding in findings] == ["info"] * 4
    assert findings[0].details["transition_route"] is expected_transition_route


def test_health_validation_findings_include_stable_governed_version_window_details() -> None:
    """Verify supported health findings expose deterministic governed window details."""

    findings = derive_health_contribution_validation_findings(
        _build_persisted_source("health_contribution_nhif_legacy_case_001")
    )

    lane_detection = findings[0]
    version_binding = findings[1]
    effective_window = findings[2]

    assert lane_detection.details["historical_version_id"] == "HCH-VER-20221231-REG"
    assert lane_detection.details["resolved_regime_identifier"] == "nhif_legacy"
    assert lane_detection.details["resolved_domain_path"] == "nhif_legacy"
    assert version_binding.details["request_regime_identifier"] == "nhif_legacy"
    assert effective_window.details["effective_start"] == "2022-12-31"
    assert effective_window.details["effective_end"] == "2023-11-21"
    assert effective_window.details["primary_effective_date"] == "2023-05-31"


def test_health_validation_flags_non_ready_version_identity_deterministically() -> None:
    """Verify a persisted non-ready health window yields an error finding, not silent success."""

    persisted_source = _build_persisted_source("health_contribution_nhif_legacy_case_001")
    version_identity = cast(
        dict[str, object], persisted_source.stored_result_payload["version_identity"]
    )
    version_identity["historical_version_id"] = "HCH-VER-20031205-A"

    findings = derive_health_contribution_validation_findings(persisted_source)

    assert [finding.code for finding in findings] == [
        "health_contribution_version_window_unsupported"
    ]
    assert findings[0].severity == "error"
    assert findings[0].details["governed_window_status"] == "partially_specified"


def test_health_validation_flags_malformed_result_with_unsupported_scope_finding() -> None:
    """Verify malformed persisted health results emit canonical unsupported-scope findings."""

    persisted_source = _build_persisted_source("health_contribution_sha_shif_case_001")
    persisted_source.stored_result_payload.pop("version_identity")

    findings = derive_health_contribution_validation_findings(persisted_source)

    assert [finding.code for finding in findings] == [
        "health_contribution_validation_scope_unsupported"
    ]
    assert findings[0].severity == "error"
    assert findings[0].details["reason"] == "malformed_health_contribution_result"


def test_health_validation_is_deterministic_for_supported_lane() -> None:
    """Verify repeated validation over the same health lane yields identical findings."""

    persisted_source = _build_persisted_source(
        "health_contribution_sha_shif_2024_non_salaried_case_001"
    )

    first = derive_health_contribution_validation_findings(copy.deepcopy(persisted_source))
    second = derive_health_contribution_validation_findings(copy.deepcopy(persisted_source))

    assert [finding.model_dump(mode="json") for finding in first] == [
        finding.model_dump(mode="json") for finding in second
    ]


def _build_persisted_source(fixture_id: str) -> PersistedValidationSource:
    fixture = _load_fixture(fixture_id)
    request = copy.deepcopy(fixture["request"])
    expected_output = fixture["expected_output"]
    result_payload = cast(dict[str, object], copy.deepcopy(expected_output["result_payload"]))
    result_payload[_REPLAY_CONTEXT_KEY] = {"normalized_input": request["input_payload"]}

    request_payload = request
    return PersistedValidationSource(
        computation_id=uuid4(),
        user_id=uuid4(),
        tax_type=cast(str, request_payload["tax_type"]),
        regime_type=cast(str, request_payload["regime_type"]),
        regime_identifier=cast(str | None, request_payload["regime_identifier"]),
        tax_year=cast(int, request_payload["tax_year"]),
        rule_version=cast(str, request_payload["rule_version"]),
        input_hash=cast(str, expected_output["input_hash"]),
        stored_result_payload=result_payload,
    )


def _load_fixture(fixture_id: str) -> GoldenFixture:
    for path in sorted(GOLDEN_CASE_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as fixture_file:
            fixture = cast(GoldenFixture, json.load(fixture_file))
        if fixture["fixture_id"] == fixture_id:
            return fixture
    raise AssertionError(f"Missing golden fixture: {fixture_id}")
