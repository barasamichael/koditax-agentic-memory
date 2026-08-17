"""Golden regression coverage for governed health-contribution fixtures."""

from __future__ import annotations

import copy
import json
from typing import cast
from typing import TypedDict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest

_HEALTH_EXECUTION_HEADERS = {
    "Authorization": "Bearer 11111111-1111-1111-1111-111111111111:IndividualTaxpayer",
    "Idempotency-Key": "idem-tax-core-contract",
    "X-Correlation-ID": "corr-tax-core-contract",
}

SUCCESS_GOLDEN_CASES = (
    (
        Path("eval/golden/tax_core/health_contribution_nhif_legacy_2010_case_001.json"),
        "health_contribution_nhif_legacy_2010_case_001",
        "HCH-VER-20100716-A",
        "nhif_legacy",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_nhif_legacy_2015_case_001.json"),
        "health_contribution_nhif_legacy_2015_case_001",
        "HCH-VER-20150401-A",
        "nhif_legacy",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_nhif_legacy_2021_case_001.json"),
        "health_contribution_nhif_legacy_2021_case_001",
        "HCH-VER-20210528-A",
        "nhif_legacy",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_nhif_legacy_case_001.json"),
        "health_contribution_nhif_legacy_case_001",
        "HCH-VER-20221231-REG",
        "nhif_legacy",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_sha_shif_case_001.json"),
        "health_contribution_sha_shif_case_001",
        "HCH-VER-20241001-A",
        "sha_shif",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_sha_shif_2024_non_salaried_case_001.json"),
        "health_contribution_sha_shif_2024_non_salaried_case_001",
        "HCH-VER-20241001-A",
        "sha_shif",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_sha_shif_2025_salaried_case_001.json"),
        "health_contribution_sha_shif_2025_salaried_case_001",
        "HCH-VER-20250228-PIT",
        "sha_shif",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_transition_boundary_nhif_case_001.json"),
        "health_contribution_transition_boundary_nhif_case_001",
        "HCH-VER-20221231-REG",
        "nhif_legacy",
    ),
    (
        Path("eval/golden/tax_core/health_contribution_transition_boundary_sha_case_001.json"),
        "health_contribution_transition_boundary_sha_case_001",
        "HCH-VER-20241001-A",
        "sha_shif",
    ),
)

REJECTION_GOLDEN_CASE = (
    Path("eval/golden/tax_core/health_contribution_historical_rejection_2003_case_001.json"),
    "health_contribution_historical_rejection_2003_case_001",
)


class GoldenFixture(TypedDict):
    fixture_version: int
    fixture_id: str
    request: dict[str, object]
    expected_output: dict[str, object]


@pytest.mark.parametrize(
    ("fixture_path", "required_fixture_id"),
    tuple((fixture_path, fixture_id) for fixture_path, fixture_id, *_ in SUCCESS_GOLDEN_CASES)
    + (REJECTION_GOLDEN_CASE,),
)
def test_health_golden_fixture_exists_for_supported_window_or_rejection_case(
    fixture_path: Path,
    required_fixture_id: str,
) -> None:
    fixture = _load_fixture(fixture_path)

    assert fixture["fixture_version"] == 1
    assert fixture["fixture_id"] == required_fixture_id


@pytest.mark.parametrize(
    ("fixture_path", "_required_fixture_id", "_expected_historical_version_id", "_expected_regime"),
    SUCCESS_GOLDEN_CASES,
)
def test_health_supported_golden_fixture_matches_exact_expected_output(
    fixture_path: Path,
    _required_fixture_id: str,
    _expected_historical_version_id: str,
    _expected_regime: str,
) -> None:
    fixture = _load_fixture(fixture_path)
    actual_output = _execute_success_request(fixture["request"])

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


@pytest.mark.parametrize(
    ("fixture_path", "_required_fixture_id", "expected_historical_version_id", "expected_regime"),
    SUCCESS_GOLDEN_CASES,
)
def test_health_supported_golden_fixture_locks_governed_version_resolution(
    fixture_path: Path,
    _required_fixture_id: str,
    expected_historical_version_id: str,
    expected_regime: str,
) -> None:
    fixture = _load_fixture(fixture_path)
    actual_output = _execute_success_request(fixture["request"])
    result_payload = cast(dict[str, object], actual_output["result_payload"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    version_identity = cast(dict[str, object], result_payload["version_identity"])

    assert contribution_summary["coverage_status"] == "implementation_ready"
    assert version_identity["historical_version_id"] == expected_historical_version_id
    assert version_identity["regime_identifier"] == expected_regime


@pytest.mark.parametrize(
    ("fixture_path", "_required_fixture_id", "_expected_historical_version_id", "_expected_regime"),
    SUCCESS_GOLDEN_CASES,
)
def test_health_supported_golden_fixture_detects_output_drift(
    fixture_path: Path,
    _required_fixture_id: str,
    _expected_historical_version_id: str,
    _expected_regime: str,
) -> None:
    fixture = _load_fixture(fixture_path)
    actual_output = _execute_success_request(fixture["request"])
    drifted_expected_output = copy.deepcopy(fixture["expected_output"])
    result_payload = cast(dict[str, object], drifted_expected_output["result_payload"])
    contribution_summary = cast(dict[str, object], result_payload["contribution_summary"])
    contribution_summary["total_contribution_kes"] = "9999.99"

    with pytest.raises(AssertionError):
        assert _canonical_json(actual_output) == _canonical_json(drifted_expected_output)


def test_health_historical_rejection_fixture_matches_exact_expected_output() -> None:
    fixture = _load_fixture(REJECTION_GOLDEN_CASE[0])
    actual_output = _execute_rejection_request(fixture["request"])

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def test_health_historical_rejection_fixture_locks_canonical_reason() -> None:
    fixture = _load_fixture(REJECTION_GOLDEN_CASE[0])
    actual_output = _execute_rejection_request(fixture["request"])
    response_json = cast(dict[str, object], actual_output["response_json"])
    detail = cast(dict[str, object], response_json["detail"])
    details = cast(dict[str, object], detail["details"])
    selection_key = cast(dict[str, object], details["selection_key"])

    assert actual_output["status_code"] == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert detail["message"] == "Invalid rule binding for computation request."
    assert detail["correlation_id"] == "corr-tax-core-contract"
    assert details["reason"] == "unsupported_partially_specified_window"
    assert selection_key["historical_version_id"] == "HCH-VER-20031205-A"
    assert selection_key["regime_identifier"] == "nhif_legacy"


def test_health_historical_rejection_fixture_detects_output_drift() -> None:
    fixture = _load_fixture(REJECTION_GOLDEN_CASE[0])
    actual_output = _execute_rejection_request(fixture["request"])
    drifted_expected_output = copy.deepcopy(fixture["expected_output"])
    response_json = cast(dict[str, object], drifted_expected_output["response_json"])
    detail = cast(dict[str, object], response_json["detail"])
    details = cast(dict[str, object], detail["details"])
    details["reason"] = "drifted_reason"

    with pytest.raises(AssertionError):
        assert _canonical_json(actual_output) == _canonical_json(drifted_expected_output)


@pytest.mark.parametrize(
    ("fixture_path", "mode"),
    tuple((fixture_path, "execute") for fixture_path, *_ in SUCCESS_GOLDEN_CASES)
    + ((REJECTION_GOLDEN_CASE[0], "endpoint"),),
)
def test_health_golden_requests_are_byte_equivalent_for_logically_equivalent_payloads(
    fixture_path: Path,
    mode: str,
) -> None:
    fixture = _load_fixture(fixture_path)
    reordered_request = cast(
        dict[str, object],
        json.loads(json.dumps(fixture["request"], sort_keys=True, ensure_ascii=True)),
    )

    if mode == "execute":
        original_output = _execute_success_request(fixture["request"])
        reordered_output = _execute_success_request(reordered_request)
    else:
        original_output = _execute_rejection_request(fixture["request"])
        reordered_output = _execute_rejection_request(reordered_request)

    assert _canonical_json(original_output) == _canonical_json(reordered_output)


def _load_fixture(fixture_path: Path) -> GoldenFixture:
    return cast(
        GoldenFixture,
        json.loads(fixture_path.read_text(encoding="utf-8")),
    )


def _execute_success_request(request_payload: dict[str, object]) -> dict[str, object]:
    request = ComputationExecutionRequest.model_validate(request_payload)
    return execute_computation(request).model_dump(mode="json")


def _execute_rejection_request(request_payload: dict[str, object]) -> dict[str, object]:
    response = _build_health_contract_client().post(
        "/computations/execute",
        json=request_payload,
        headers=_HEALTH_EXECUTION_HEADERS,
    )
    return {
        "status_code": response.status_code,
        "response_json": cast(dict[str, object], response.json()),
    }


def _build_health_contract_client() -> TestClient:
    return TestClient(create_app())


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
