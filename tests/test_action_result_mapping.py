"""Verify deterministic canonical action-result mapping behavior."""

from __future__ import annotations

from copy import deepcopy

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.action_result_mapping import map_action_result
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse


def _base_adapter_response() -> ActionAdapterResponse:
    return {
        "adapter_status": "accepted",
        "provider_reference": "provider-ref-001",
        "action_result_code": "submission_action_accepted",
        "message": "Provider accepted submission action.",
        "trace": {
            "correlation_id": "corr-map-001",
            "trace_id": "3" * 64,
            "adapter_request_id": "a" * 64,
            "adapter_name": "deterministic_submission_mock_adapter_v1",
            "submission_payload_ref": "submission-preview-001",
        },
        "error": None,
    }


def test_maps_accepted_adapter_outcome_to_canonical_accepted() -> None:
    result = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=_base_adapter_response(),
        execution_error=None,
    )

    assert result["action_status"] == "accepted"
    assert result["reason_code"] == "submission_action_accepted"
    assert result["retryable"] is False
    assert result["next_retry_at"] is None
    assert result["provider_reference"] == "provider-ref-001"
    assert result["trace_id"] == "trace-map-001"


def test_maps_pending_adapter_outcome_to_canonical_pending() -> None:
    response = _base_adapter_response()
    response["adapter_status"] = "mock_pending"
    response["action_result_code"] = "submission_action_mock_pending"
    response["provider_reference"] = None
    response["message"] = "Submission action is pending in deterministic mock adapter."
    result = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=response,
        execution_error=None,
    )

    assert result["action_status"] == "pending"
    assert result["reason_code"] == "submission_action_mock_pending"
    assert result["retryable"] is False
    assert result["provider_reference"] is None


def test_maps_rejected_execution_to_canonical_rejected() -> None:
    result = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="rejected",
        adapter_response=None,
        execution_error={
            "reason_code": "idempotency_key_payload_conflict",
            "reason": "Idempotency key has already been used with different payload.",
        },
    )

    assert result["action_status"] == "rejected"
    assert result["reason_code"] == "idempotency_key_payload_conflict"
    assert result["retryable"] is False
    assert result["next_retry_at"] is None
    assert result["correlation_id"] == "corr-map-001"
    assert result["idempotency_key"] == "idem-map-001"
    assert result["trace_id"] == "trace-map-001"


def test_maps_transient_provider_failure_to_retryable_failure() -> None:
    response = _base_adapter_response()
    response["action_result_code"] = "provider_retryable_failure"
    response["message"] = "Provider is temporarily unavailable."
    result = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=response,
        execution_error=None,
    )

    assert result["action_status"] == "retryable_failure"
    assert result["reason_code"] == "provider_retryable_failure"
    assert result["retryable"] is True
    assert result["next_retry_at"] is None


def test_unknown_adapter_outcome_safe_fails_deterministically_to_rejected() -> None:
    response = _base_adapter_response()
    response["action_result_code"] = "provider_unknown_result_state"
    response["message"] = "Unknown provider state."
    result = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=response,
        execution_error=None,
    )

    assert result["action_status"] == "rejected"
    assert result["reason_code"] == "unknown_adapter_outcome"
    assert result["retryable"] is False


def test_identical_mapping_inputs_produce_identical_canonical_json() -> None:
    response = _base_adapter_response()
    response["adapter_status"] = "mock_pending"
    response["action_result_code"] = "submission_action_mock_pending"

    first = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=deepcopy(response),
        execution_error=None,
    )
    second = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=deepcopy(response),
        execution_error=None,
    )

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_adapter_error_maps_to_non_retryable_rejection() -> None:
    response = _base_adapter_response()
    response["adapter_status"] = "unsupported"
    response["action_result_code"] = "unsupported_action_type"
    response["error"] = {
        "error_code": "unsupported_submission_action",
        "message": "Submission action is unsupported.",
        "reason_code": "unsupported_action_type",
        "reason": "Adapter registry has no matching deterministic action adapter.",
        "rejected_context": {
            "action_type": "submission_execute",
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
            "correlation_id": "corr-map-001",
        },
        "required_controls": ["revise_action_type"],
        "next_allowed_actions": ["revise_input", "reject"],
        "trace_id": "trace-map-001",
    }

    result = map_action_result(
        idempotency_key="idem-map-001",
        correlation_id="corr-map-001",
        trace_id="trace-map-001",
        execution_status="resolved",
        adapter_response=response,
        execution_error=None,
    )

    assert result["action_status"] == "rejected"
    assert result["reason_code"] == "unsupported_action_type"
    assert result["retryable"] is False
    assert result["next_retry_at"] is None
    assert result["correlation_id"] == "corr-map-001"
    assert result["idempotency_key"] == "idem-map-001"
    assert result["trace_id"] == "trace-map-001"


def test_retryable_mapping_preserves_correlation_and_idempotency_ids() -> None:
    response = _base_adapter_response()
    response["action_result_code"] = "provider_timeout"
    response["message"] = "Provider timeout; retry permitted."
    response["provider_reference"] = "provider-ref-timeout-001"

    result = map_action_result(
        idempotency_key="idem-map-xyz",
        correlation_id="corr-map-xyz",
        trace_id="trace-map-xyz",
        execution_status="resolved",
        adapter_response=response,
        execution_error=None,
    )

    assert result["action_status"] == "retryable_failure"
    assert result["retryable"] is True
    assert result["correlation_id"] == "corr-map-xyz"
    assert result["idempotency_key"] == "idem-map-xyz"
    assert result["trace_id"] == "trace-map-xyz"


def test_rejected_execution_preserves_tax_domain_aware_reason_codes() -> None:
    result = map_action_result(
        idempotency_key="idem-map-domain",
        correlation_id="corr-map-domain",
        trace_id="trace-map-domain",
        execution_status="rejected",
        adapter_response=None,
        execution_error={
            "reason_code": "invalid_tax_domain",
            "reason": "Requested tax domain is not recognized by the downstream boundary.",
        },
    )

    assert result["action_status"] == "rejected"
    assert result["reason_code"] == "invalid_tax_domain"
    assert result["reason"] == "Requested tax domain is not recognized by the downstream boundary."
    assert result["retryable"] is False
