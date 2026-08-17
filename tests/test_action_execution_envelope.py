"""Verify deterministic idempotent action execution envelope behavior."""

from __future__ import annotations

from copy import deepcopy

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse
from services.orchestration.app.action_adapter_registry import (
    dispatch_submission_action_request_with_envelope,
)
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import execute_idempotent_action_request
from services.orchestration.app.action_execution_envelope import (
    InMemoryActionExecutionIdempotencyStore,
)
from services.orchestration.app.action_execution_envelope import (
    reset_default_action_execution_idempotency_store,
)


def _build_execution_request() -> ActionExecutionRequest:
    return {
        "idempotency_key": "idem-action-001",
        "correlation_id": "corr-action-001",
        "action_type": "submission_execute",
        "submission_payload_ref": "submission-preview-001",
        "capability_context": {
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
    }


def _mock_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
    return {
        "adapter_status": "mock_pending",
        "provider_reference": None,
        "action_result_code": "submission_action_mock_pending",
        "message": "deterministic mock provider pending",
        "trace": {
            "correlation_id": "corr-action-001",
            "trace_id": "t" * 64,
            "adapter_request_id": "b" * 64,
            "adapter_name": "mock_adapter",
            "submission_payload_ref": "submission-preview-001",
        },
        "error": None,
    }


def test_first_valid_request_returns_execution_envelope_and_persists_state() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    response = execute_idempotent_action_request(
        request=_build_execution_request(),
        dispatch_adapter_request=_mock_dispatch,
        idempotency_store=store,
    )

    assert response["execution_status"] == "resolved"
    assert response["error"] is None
    assert response["adapter_response"] is not None
    assert response["mapped_result"]["action_status"] == "pending"
    assert response["mapped_result"]["reason_code"] == "submission_action_mock_pending"
    assert response["mapped_result"]["retryable"] is False
    assert response["mapped_result"]["correlation_id"] == "corr-action-001"
    assert response["mapped_result"]["idempotency_key"] == "idem-action-001"
    assert response["mapped_result"]["trace_id"] == response["trace"]["trace_id"]
    assert response["trace"]["correlation_id"] == "corr-action-001"
    assert isinstance(response["trace"]["trace_id"], str)
    assert len(response["trace"]["trace_id"]) == 64
    assert store.get("idem-action-001") is not None


def test_same_key_same_payload_replays_exact_previous_response_without_duplicate_dispatch() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    dispatch_count = 0

    def _counted_dispatch(request: ActionAdapterRequest) -> ActionAdapterResponse:
        nonlocal dispatch_count
        dispatch_count += 1
        return _mock_dispatch(request)

    request = _build_execution_request()
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
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_same_key_different_payload_is_rejected_deterministically() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    request = _build_execution_request()
    execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_mock_dispatch,
        idempotency_store=store,
    )

    conflicting_request = deepcopy(request)
    conflicting_request["submission_payload_ref"] = "submission-preview-002"
    conflicting = execute_idempotent_action_request(
        request=conflicting_request,
        dispatch_adapter_request=_mock_dispatch,
        idempotency_store=store,
    )

    assert conflicting["execution_status"] == "rejected"
    assert conflicting["adapter_response"] is None
    assert conflicting["error"] is not None
    assert conflicting["error"]["reason_code"] == "idempotency_key_payload_conflict"
    assert conflicting["error"]["rejected_context"]["correlation_id"] == "corr-action-001"
    assert conflicting["error"]["rejected_context"]["idempotency_key"] == "idem-action-001"
    assert conflicting["error"]["trace_id"] == conflicting["trace"]["trace_id"]
    assert conflicting["mapped_result"]["action_status"] == "rejected"
    assert conflicting["mapped_result"]["reason_code"] == "idempotency_key_payload_conflict"


def test_missing_idempotency_key_is_rejected_deterministically() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    request = _build_execution_request()
    request["idempotency_key"] = "   "
    result = execute_idempotent_action_request(
        request=request,
        dispatch_adapter_request=_mock_dispatch,
        idempotency_store=store,
    )

    assert result["execution_status"] == "rejected"
    assert result["adapter_response"] is None
    assert result["error"] is not None
    assert result["error"]["reason_code"] == "missing_idempotency_key"
    assert result["error"]["required_controls"] == ["provide_idempotency_key"]
    assert result["mapped_result"]["action_status"] == "rejected"
    assert result["mapped_result"]["reason_code"] == "missing_idempotency_key"


def test_registry_envelope_boundary_replays_without_duplicate_adapter_execution() -> None:
    reset_default_action_execution_idempotency_store()
    request = _build_execution_request()
    first = dispatch_submission_action_request_with_envelope(deepcopy(request))
    second = dispatch_submission_action_request_with_envelope(deepcopy(request))

    assert first["execution_status"] == "resolved"
    assert first["adapter_response"] is not None
    assert first["mapped_result"]["action_status"] == "pending"
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_retryable_failure_replay_preserves_retry_semantics_without_duplicate_dispatch() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    dispatch_count = 0

    def _retryable_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
        nonlocal dispatch_count
        dispatch_count += 1
        return {
            "adapter_status": "accepted",
            "provider_reference": "provider-retryable-001",
            "action_result_code": "provider_retryable_failure",
            "message": "provider timeout, retry later",
            "trace": {
                "correlation_id": "corr-action-001",
                "trace_id": "e" * 64,
                "adapter_request_id": "c" * 64,
                "adapter_name": "retryable_adapter",
                "submission_payload_ref": "submission-preview-001",
            },
            "error": None,
        }

    request = _build_execution_request()
    first = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_retryable_dispatch,
        idempotency_store=store,
    )
    second = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_retryable_dispatch,
        idempotency_store=store,
    )

    assert dispatch_count == 1
    assert first["mapped_result"]["action_status"] == "retryable_failure"
    assert first["mapped_result"]["retryable"] is True
    assert first["mapped_result"]["correlation_id"] == request["correlation_id"]
    assert first["mapped_result"]["idempotency_key"] == request["idempotency_key"]
    assert first["mapped_result"]["trace_id"] == first["trace"]["trace_id"]
    assert first["mapped_result"]["next_retry_at"] is None
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_non_retryable_rejected_adapter_outcome_does_not_enter_retry_path() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    dispatch_count = 0

    def _rejected_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
        nonlocal dispatch_count
        dispatch_count += 1
        return {
            "adapter_status": "unsupported",
            "provider_reference": None,
            "action_result_code": "unsupported_action_type",
            "message": "action type unsupported",
            "trace": {
                "correlation_id": "corr-action-001",
                "trace_id": "f" * 64,
                "adapter_request_id": "d" * 64,
                "adapter_name": "unsupported_adapter",
                "submission_payload_ref": "submission-preview-001",
            },
            "error": {
                "error_code": "unsupported_submission_action",
                "message": "Submission action type unsupported.",
                "reason_code": "unsupported_action_type",
                "reason": "No adapter registered for action type.",
                "rejected_context": {
                    "action_type": "submission_execute",
                    "supported_lane_id": "resident_employment_income_2023_07_01",
                    "historical_version_id": "KIT-VER-20230701-A",
                    "tax_year": 2023,
                    "correlation_id": "corr-action-001",
                },
                "required_controls": ["revise_action_type"],
                "next_allowed_actions": ["revise_input", "reject"],
                "trace_id": "f" * 64,
            },
        }

    request = _build_execution_request()
    first = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_rejected_dispatch,
        idempotency_store=store,
    )
    second = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_rejected_dispatch,
        idempotency_store=store,
    )

    assert dispatch_count == 1
    assert first["execution_status"] == "resolved"
    assert first["mapped_result"]["action_status"] == "rejected"
    assert first["mapped_result"]["retryable"] is False
    assert first["mapped_result"]["next_retry_at"] is None
    assert canonical_json_dumps(second) == canonical_json_dumps(first)
