"""Lock deterministic retry and duplicate-prevention behavior for action execution."""

from __future__ import annotations

from copy import deepcopy

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import execute_idempotent_action_request
from services.orchestration.app.action_execution_envelope import (
    InMemoryActionExecutionIdempotencyStore,
)


def _execution_request() -> ActionExecutionRequest:
    return {
        "idempotency_key": "idem-retry-001",
        "correlation_id": "corr-retry-001",
        "action_type": "submission_execute",
        "submission_payload_ref": "submission-preview-retry-001",
        "capability_context": {
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
    }


def _pending_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
    return {
        "adapter_status": "mock_pending",
        "provider_reference": None,
        "action_result_code": "submission_action_mock_pending",
        "message": "Submission action pending in deterministic mock adapter.",
        "trace": {
            "correlation_id": "corr-retry-001",
            "trace_id": "1" * 64,
            "adapter_request_id": "e" * 64,
            "adapter_name": "pending_adapter",
            "submission_payload_ref": "submission-preview-retry-001",
        },
        "error": None,
    }


def _retryable_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
    return {
        "adapter_status": "accepted",
        "provider_reference": "provider-retry-001",
        "action_result_code": "provider_transient_failure",
        "message": "Transient provider failure, retry allowed.",
        "trace": {
            "correlation_id": "corr-retry-001",
            "trace_id": "2" * 64,
            "adapter_request_id": "f" * 64,
            "adapter_name": "retryable_adapter",
            "submission_payload_ref": "submission-preview-retry-001",
        },
        "error": None,
    }


def test_duplicate_prevention_replays_identical_response_without_duplicate_adapter_call() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    dispatch_count = 0

    def _counted_dispatch(request: ActionAdapterRequest) -> ActionAdapterResponse:
        nonlocal dispatch_count
        dispatch_count += 1
        return _pending_dispatch(request)

    request = _execution_request()
    first = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_counted_dispatch,
        idempotency_store=store,
    )
    second = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_counted_dispatch,
        idempotency_store=store,
    )

    assert dispatch_count == 1
    assert first["mapped_result"]["action_status"] == "pending"
    assert first["mapped_result"]["retryable"] is False
    assert first["mapped_result"]["correlation_id"] == "corr-retry-001"
    assert first["mapped_result"]["idempotency_key"] == "idem-retry-001"
    assert first["mapped_result"]["trace_id"] == first["trace"]["trace_id"]
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_same_key_with_different_payload_rejects_with_canonical_conflict_payload() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    request = _execution_request()
    execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_pending_dispatch,
        idempotency_store=store,
    )
    conflict_request = deepcopy(request)
    conflict_request["submission_payload_ref"] = "submission-preview-retry-conflict"

    first_conflict = execute_idempotent_action_request(
        request=deepcopy(conflict_request),
        dispatch_adapter_request=_pending_dispatch,
        idempotency_store=store,
    )
    second_conflict = execute_idempotent_action_request(
        request=deepcopy(conflict_request),
        dispatch_adapter_request=_pending_dispatch,
        idempotency_store=store,
    )

    assert first_conflict["execution_status"] == "rejected"
    assert first_conflict["error"] is not None
    assert first_conflict["error"]["reason_code"] == "idempotency_key_payload_conflict"
    assert first_conflict["mapped_result"]["action_status"] == "rejected"
    assert first_conflict["mapped_result"]["reason_code"] == "idempotency_key_payload_conflict"
    assert first_conflict["mapped_result"]["retryable"] is False
    assert canonical_json_dumps(second_conflict) == canonical_json_dumps(first_conflict)


def test_retryable_failure_classification_and_replay_semantics_are_deterministic() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    dispatch_count = 0

    def _counted_retryable_dispatch(request: ActionAdapterRequest) -> ActionAdapterResponse:
        nonlocal dispatch_count
        dispatch_count += 1
        return _retryable_dispatch(request)

    request = _execution_request()
    first = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_counted_retryable_dispatch,
        idempotency_store=store,
    )
    replay = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_counted_retryable_dispatch,
        idempotency_store=store,
    )

    assert dispatch_count == 1
    assert first["execution_status"] == "resolved"
    assert first["mapped_result"]["action_status"] == "retryable_failure"
    assert first["mapped_result"]["reason_code"] == "provider_transient_failure"
    assert first["mapped_result"]["retryable"] is True
    assert first["mapped_result"]["next_retry_at"] is None
    assert canonical_json_dumps(replay) == canonical_json_dumps(first)
