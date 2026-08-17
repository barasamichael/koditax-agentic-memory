"""Test canonical deterministic hashing behavior for governed computation inputs."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast

import pytest
from fastapi.testclient import TestClient

from services.tax_core.app.main import create_app
from shared.determinism.input_hash import InputHashError
from shared.determinism.input_hash import compute_computation_input_hash
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import MaterializedComputationExecutionResult

TEST_PRINCIPAL_ID = UUID("33333333-3333-3333-3333-333333333333")
TEST_COMPUTATION_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
TEST_AUDIT_EVENT_ID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
TEST_IDEMPOTENCY_KEY = "idem-tax-core-hash"
TEST_CORRELATION_ID = "corr-tax-core-hash"


def test_hash_is_stable_for_logical_equivalent_nested_payloads() -> None:
    """Verify nested key reordering does not change canonical input hash."""

    hash_one = compute_computation_input_hash(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"b": {"y": 2, "x": 1}, "a": [3, {"k": 9, "j": 8}]},
    ).sha256_hex
    hash_two = compute_computation_input_hash(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"a": [3, {"j": 8, "k": 9}], "b": {"x": 1, "y": 2}},
    ).sha256_hex

    assert hash_one == hash_two


def test_hash_normalizes_tuple_to_list_but_preserves_list_order() -> None:
    """Verify tuple normalization and list-order sensitivity."""

    tuple_hash = compute_computation_input_hash(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"items": (1, {"b": 2, "a": 1})},
    ).sha256_hex
    list_hash = compute_computation_input_hash(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"items": [1, {"a": 1, "b": 2}]},
    ).sha256_hex
    reordered_list_hash = compute_computation_input_hash(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"items": [{"a": 1, "b": 2}, 1]},
    ).sha256_hex

    assert tuple_hash == list_hash
    assert list_hash != reordered_list_hash


def test_hash_rejects_unsupported_value_type() -> None:
    """Verify unsupported non-JSON-safe value types fail deterministically."""

    with pytest.raises(InputHashError) as error:
        compute_computation_input_hash(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2025,
            rule_version="v1",
            input_payload={"bad": {1, 2}},
        )

    assert error.value.reason == "unsupported_value_type"
    assert error.value.path == "$.input_payload.bad"


@pytest.mark.parametrize(
    "non_finite_value",
    [float("nan"), float("inf"), float("-inf")],
)
def test_hash_rejects_non_finite_numbers(non_finite_value: float) -> None:
    """Verify NaN and infinities fail deterministically."""

    with pytest.raises(InputHashError) as error:
        compute_computation_input_hash(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2025,
            rule_version="v1",
            input_payload={"value": non_finite_value},
        )

    assert error.value.reason == "non_finite_number"
    assert error.value.path == "$.input_payload.value"


def test_hash_changes_when_governed_fields_change() -> None:
    """Verify governed envelope fields affect deterministic hash identity."""

    base_hash = compute_computation_input_hash(
        tax_type="income_tax",
        regime_type="income_tax",
        regime_identifier=None,
        tax_year=2025,
        rule_version="v1",
        input_payload={"income": 1000},
    ).sha256_hex

    assert (
        compute_computation_input_hash(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2025,
            rule_version="v2",
            input_payload={"income": 1000},
        ).sha256_hex
        != base_hash
    )
    assert (
        compute_computation_input_hash(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2026,
            rule_version="v1",
            input_payload={"income": 1000},
        ).sha256_hex
        != base_hash
    )
    assert (
        compute_computation_input_hash(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier="sha",
            tax_year=2025,
            rule_version="v1",
            input_payload={"income": 1000},
        ).sha256_hex
        != base_hash
    )
    assert (
        compute_computation_input_hash(
            tax_type="income_tax",
            regime_type="income_tax",
            regime_identifier=None,
            tax_year=2025,
            rule_version="v1",
            input_payload={"income": 1001},
        ).sha256_hex
        != base_hash
    )


def test_endpoint_returns_stable_input_hash_for_logical_equivalent_requests() -> None:
    """Verify endpoint returns deterministic top-level input_hash."""

    client = _build_hash_test_client()
    request_one = {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2025,
        "rule_version": "v1",
        "input_payload": {"b": {"y": 2, "x": 1}, "a": [3, {"k": 9, "j": 8}]},
    }
    request_two = {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": None,
        "tax_year": 2025,
        "rule_version": "v1",
        "input_payload": {"a": [3, {"j": 8, "k": 9}], "b": {"x": 1, "y": 2}},
    }

    response_one = client.post(
        "/computations/execute",
        json=request_one,
        headers=_execution_headers(),
    )
    response_two = client.post(
        "/computations/execute",
        json=request_two,
        headers=_execution_headers(),
    )

    assert response_one.status_code == 200
    assert response_two.status_code == 200

    payload_one = _response_json(response_one)
    payload_two = _response_json(response_two)

    hash_one = cast(str, payload_one["input_hash"])
    hash_two = cast(str, payload_two["input_hash"])
    result_payload_one = cast(dict[str, object], payload_one["result_payload"])
    result_payload_two = cast(dict[str, object], payload_two["result_payload"])
    assert hash_one == hash_two
    assert hash_one == cast(str, result_payload_one["input_hash"])
    assert hash_two == cast(str, result_payload_two["input_hash"])


def _response_json(response: object) -> dict[str, object]:
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _build_hash_test_client() -> TestClient:
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
