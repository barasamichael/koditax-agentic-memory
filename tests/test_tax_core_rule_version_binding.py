"""Test deterministic tax-core rule-version binding behavior."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Any
from typing import cast
from datetime import date

import pytest
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.rule_binding import bind_rule_selection
from services.tax_core.app.engine.execution_contract import RuleSelectionKey
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import MaterializedComputationExecutionResult

TEST_PRINCIPAL_ID = UUID("22222222-2222-2222-2222-222222222222")
TEST_COMPUTATION_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TEST_AUDIT_EVENT_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
TEST_IDEMPOTENCY_KEY = "idem-tax-core-binding"
TEST_CORRELATION_ID = "corr-tax-core-binding"


def test_execute_computation_succeeds_with_explicit_known_rule_version() -> None:
    """Verify deterministic execution succeeds for known explicit rule version."""

    request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1000},
    )

    result = execute_computation(request)

    assert result.status == "ok"
    assert result.rule_version == "v1"


def test_bind_rule_selection_rejects_unknown_rule_version() -> None:
    """Verify unknown rule binding keys fail deterministically."""

    key = RuleSelectionKey(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v999",
    )

    try:
        bind_rule_selection(key)
    except RuleBindingError as error:
        assert error.reason == "unknown_rule_binding"
        return

    raise AssertionError("Expected unknown rule binding to raise RuleBindingError.")


def test_execution_endpoint_rejects_missing_rule_version_at_boundary() -> None:
    """Verify missing rule_version fails request-shape boundary validation."""

    client = _build_rule_binding_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "income_tax",
            "regime_type": "income_tax",
            "regime_identifier": None,
            "tax_year": 2025,
            "input_payload": {"income": 1000},
        },
        headers=_execution_headers(),
    )
    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_computation_request"


def test_execution_endpoint_rejects_unknown_rule_version_binding() -> None:
    """Verify unknown explicit version fails deterministic rule binding."""

    client = _build_rule_binding_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "income_tax",
            "regime_type": "income_tax",
            "regime_identifier": None,
            "tax_year": 2025,
            "rule_version": "v999",
            "input_payload": {"income": 1000},
        },
        headers=_execution_headers(),
    )
    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert detail["message"] == "Invalid rule binding for computation request."
    assert details["reason"] == "unknown_rule_binding"


def test_execution_endpoint_rejects_ambiguous_rule_binding() -> None:
    """Verify ambiguous explicit version candidate set fails hard."""

    client = _build_rule_binding_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "income_tax",
            "regime_type": "income_tax",
            "regime_identifier": None,
            "tax_year": 2025,
            "rule_version": "v_ambiguous",
            "input_payload": {"income": 1000},
        },
        headers=_execution_headers(),
    )
    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert details["reason"] == "ambiguous_rule_binding"


def test_execution_endpoint_rejects_missing_regime_identifier_for_health_binding() -> None:
    """Verify health-contribution binding requires regime_identifier."""

    client = _build_rule_binding_test_client()
    response = client.post(
        "/computations/execute",
        json={
            "tax_type": "health_contribution",
            "regime_type": "health_contribution",
            "regime_identifier": None,
            "tax_year": 2023,
            "rule_version": "v1",
            "input_payload": {
                "version_context": {
                    "primary_effective_date": "2023-05-31",
                    "version_selection_basis": "payroll_period_end",
                }
            },
        },
        headers=_execution_headers(),
    )
    payload = _response_json(response)
    detail = cast(dict[str, object], payload["detail"])
    details = cast(dict[str, object], detail["details"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_rule_binding"
    assert details["reason"] == "missing_regime_identifier"


@pytest.mark.parametrize(
    (
        "regime_identifier",
        "tax_year",
        "primary_effective_date",
        "historical_version_id",
        "expected_binding_id",
    ),
    [
        (
            "nhif_legacy",
            2012,
            date(2012, 1, 31),
            "HCH-VER-20100716-A",
            "health_contribution_nhif_legacy_v1_2010_07_16",
        ),
        (
            "nhif_legacy",
            2019,
            date(2019, 7, 31),
            "HCH-VER-20150401-A",
            "health_contribution_nhif_legacy_v1_2015_04_01",
        ),
        (
            "nhif_legacy",
            2022,
            date(2022, 6, 30),
            "HCH-VER-20210528-A",
            "health_contribution_nhif_legacy_v1_2021_05_28",
        ),
        (
            "nhif_legacy",
            2023,
            date(2023, 5, 31),
            "HCH-VER-20221231-REG",
            "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        ),
        (
            "sha_shif",
            2024,
            date(2024, 10, 31),
            "HCH-VER-20241001-A",
            "health_contribution_sha_shif_v1_2024_10_01",
        ),
        (
            "sha_shif",
            2025,
            date(2025, 3, 31),
            "HCH-VER-20250228-PIT",
            "health_contribution_sha_shif_v1_2025_02_28_pit",
        ),
    ],
)
def test_bind_rule_selection_resolves_supported_health_rule_windows(
    regime_identifier: str,
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_binding_id: str,
) -> None:
    """Verify every implementation-ready health window binds deterministically."""

    key = RuleSelectionKey(
        tax_type="health_contribution",
        regime_type="health_contribution",
        regime_identifier=regime_identifier,
        tax_year=tax_year,
        rule_version="v1",
        primary_effective_date=primary_effective_date,
        historical_version_id=historical_version_id,
    )

    bound_rule = bind_rule_selection(key)

    assert bound_rule.binding_id == expected_binding_id


@pytest.mark.parametrize(
    (
        "regime_identifier",
        "tax_year",
        "primary_effective_date",
        "historical_version_id",
        "expected_reason",
    ),
    [
        (
            "nhif_legacy",
            2009,
            date(2009, 12, 31),
            "HCH-VER-20031205-A",
            "unsupported_partially_specified_window",
        ),
        (
            "nhif_legacy",
            2023,
            date(2023, 5, 31),
            "HCH-VER-20221231-ACT",
            "unsupported_governed_boundary_only_window",
        ),
        (
            "sha_shif",
            2024,
            date(2024, 9, 30),
            "HCH-VER-20240920-PIT",
            "unsupported_governed_boundary_only_window",
        ),
        (
            "sha_shif",
            2025,
            date(2025, 3, 31),
            "HCH-VER-20250228-AMD",
            "unsupported_governed_boundary_only_window",
        ),
    ],
)
def test_bind_rule_selection_rejects_non_ready_health_windows_with_governed_reasons(
    regime_identifier: str,
    tax_year: int,
    primary_effective_date: date,
    historical_version_id: str,
    expected_reason: str,
) -> None:
    """Verify direct health binding rejects known non-ready windows explicitly."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier=regime_identifier,
                tax_year=tax_year,
                rule_version="v1",
                primary_effective_date=primary_effective_date,
                historical_version_id=historical_version_id,
            )
        )

    assert error.value.reason == expected_reason


def test_bind_rule_selection_resolves_fail_closed_mixed_context_binding() -> None:
    """Verify explicit mixed_context requests bind to the governed fail-closed module."""

    key = RuleSelectionKey(
        tax_type="health_contribution",
        regime_type="health_contribution",
        regime_identifier="mixed_context",
        tax_year=2025,
        rule_version="v1",
    )

    bound_rule = bind_rule_selection(key)

    assert bound_rule.binding_id == "health_contribution_mixed_context_v1_fail_closed"


def test_bind_rule_selection_resolves_transition_boundary_to_nhif_window() -> None:
    """Verify transition mode resolves a supported pre-cutover date to NHIF."""

    key = RuleSelectionKey(
        tax_type="health_contribution",
        regime_type="health_contribution",
        regime_identifier="transition_boundary",
        tax_year=2023,
        rule_version="v1",
        primary_effective_date=date(2023, 5, 31),
    )

    bound_rule = bind_rule_selection(key)

    assert bound_rule.binding_id == "health_contribution_nhif_legacy_v1_2022_12_31_reg"


def test_bind_rule_selection_rejects_unresolved_transition_boundary_date() -> None:
    """Verify unresolved transition-only dates fail closed at binding time."""

    with pytest.raises(RuleBindingError) as error:
        bind_rule_selection(
            RuleSelectionKey(
                tax_type="health_contribution",
                regime_type="health_contribution",
                regime_identifier="transition_boundary",
                tax_year=2024,
                rule_version="v1",
                primary_effective_date=date(2024, 7, 15),
            )
        )

    assert error.value.reason == "unresolved_transition_window"


def test_execute_computation_is_deterministic_for_identical_requests_with_same_version() -> None:
    """Verify identical request payloads with same version produce same envelope."""

    request = ComputationExecutionRequest(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1000, "deductions": {"b": 2, "a": 1}},
    )

    first = execute_computation(request).model_dump(mode="json")
    second = execute_computation(request).model_dump(mode="json")

    assert _canonical_json(first) == _canonical_json(second)


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _build_rule_binding_test_client() -> TestClient:
    app = create_app()
    app.state.materializer = _stub_materializer
    return TestClient(app)


def _stub_materializer(
    execution_request: ComputationExecutionRequest,
    execution_result: ComputationExecutionResult,
    context: MaterializationContext,
) -> MaterializedComputationExecutionResult:
    return MaterializedComputationExecutionResult(
        status="ok",
        computation_id=TEST_COMPUTATION_ID,
        computation_result_id=TEST_COMPUTATION_ID,
        audit_event_id=TEST_AUDIT_EVENT_ID,
        idempotency_key=context.idempotency_key,
        correlation_id=context.correlation_id,
        tax_type=execution_request.tax_type,
        regime_type=execution_request.regime_type,
        tax_year=execution_request.tax_year,
        rule_version=execution_result.rule_version,
        input_hash=execution_result.input_hash,
        result_payload=execution_result.result_payload,
    )


def _execution_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TEST_PRINCIPAL_ID}:IndividualTaxpayer",
        "Idempotency-Key": TEST_IDEMPOTENCY_KEY,
        "X-Correlation-ID": TEST_CORRELATION_ID,
    }


def _response_json(response: object) -> dict[str, object]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
