"""Failure-injection regression coverage for phase 6.7 pilot safety controls."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Generator

import pytest

from services.orchestration.app import income_tax_capability_gate
from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import bind_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import verify_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import get_income_tax_audit_events_for_correlation
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome
from tests.income_tax_prompt_flow_support import authorize_income_tax_action_with_step_up_proof
from tests.income_tax_prompt_flow_support import build_income_tax_action_final_outcome_envelope
from services.orchestration.app.feature_flags import set_kill_switch
from services.orchestration.app.feature_flags import reset_runtime_safety_control_config
from shared.validation.income_tax_capability_manifest import CapabilityManifestError
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import execute_idempotent_action_request
from services.orchestration.app.action_execution_envelope import (
    InMemoryActionExecutionIdempotencyStore,
)

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)


@pytest.fixture(autouse=True)
def _reset_runtime_controls() -> (
    Generator[None, None, None]
):  # pyright: ignore[reportUnusedFunction]
    reset_runtime_safety_control_config()
    yield
    reset_runtime_safety_control_config()


def _confirmed_record() -> dict[str, object]:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prepare_income_tax_confirmation_review(draft)
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=cast(dict[str, object], awaiting["state_record"]),
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def _execution_request() -> ActionExecutionRequest:
    return {
        "idempotency_key": "failure-injection-idem-001",
        "correlation_id": "failure-injection-corr-001",
        "action_type": "submission_execute",
        "submission_payload_ref": "submission-preview-fi-001",
        "capability_context": {
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
    }


def _mock_pending_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
    return {
        "adapter_status": "mock_pending",
        "provider_reference": None,
        "action_result_code": "submission_action_mock_pending",
        "message": "deterministic pending adapter",
        "trace": {
            "correlation_id": "failure-injection-corr-001",
            "trace_id": "1" * 64,
            "adapter_request_id": "2" * 64,
            "adapter_name": "mock_pending_adapter",
            "submission_payload_ref": "submission-preview-fi-001",
        },
        "error": None,
    }


def _retryable_dispatch(_: ActionAdapterRequest) -> ActionAdapterResponse:
    return {
        "adapter_status": "accepted",
        "provider_reference": "provider-fi-retryable-001",
        "action_result_code": "provider_retryable_failure",
        "message": "provider timeout, retry later",
        "trace": {
            "correlation_id": "failure-injection-corr-001",
            "trace_id": "3" * 64,
            "adapter_request_id": "4" * 64,
            "adapter_name": "mock_retryable_adapter",
            "submission_payload_ref": "submission-preview-fi-001",
        },
        "error": None,
    }


def test_capability_gate_manifest_load_failure_fails_closed_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_manifest_error() -> dict[str, object]:
        raise CapabilityManifestError(
            reason="manifest_not_found",
            message="injected manifest load failure",
        )

    monkeypatch.setattr(
        income_tax_capability_gate,
        "load_income_tax_vertical_slice_manifest",
        _raise_manifest_error,
    )

    first = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    second = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    result = cast(dict[str, object], first["result"])
    trace = cast(dict[str, object], first["trace"])
    audit = cast(dict[str, object], first["audit"])

    assert first["outcome_status"] == "rejected"
    assert result["error_code"] == "unsupported_prompt_scope"
    assert result["reason"] == "manifest_load_failure"
    assert isinstance(result["correlation_id"], str)
    assert isinstance(result["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert isinstance(trace["trace_id"], str)
    assert "plan_validated" in cast(list[str], audit["event_types"])
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_tenant_guardrail_rejection_is_canonical_traceable_and_deterministic() -> None:
    first = execute_income_tax_prompt_flow_final_outcome(
        SUPPORTED_PROMPT,
        tenant_id="pilot_tenant_unknown",
    )
    second = execute_income_tax_prompt_flow_final_outcome(
        SUPPORTED_PROMPT,
        tenant_id="pilot_tenant_unknown",
    )
    result = cast(dict[str, object], first["result"])
    trace = cast(dict[str, object], first["trace"])
    audit = cast(dict[str, object], first["audit"])
    rejected_context = cast(dict[str, object], result["rejected_context"])

    assert first["outcome_status"] == "rejected"
    assert result["error_code"] == "pilot_tenant_not_allowed"
    assert result["reason_code"] == "tenant_not_allowlisted"
    assert rejected_context["tenant_id"] == "pilot_tenant_unknown"
    assert isinstance(trace["correlation_id"], str)
    assert isinstance(trace["trace_id"], str)
    assert "pilot_tenant_guard_decision" in cast(list[str], audit["event_types"])
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_kill_switch_enabled_mid_flow_blocks_action_before_execution() -> None:
    set_kill_switch(switch_key="action:submission_execute", enabled=True)
    action_attempt = attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="low",
    )
    rejection = cast(dict[str, object], action_attempt["rejection"])
    correlation_id = cast(str, rejection["correlation_id"])
    events = get_income_tax_audit_events_for_correlation(correlation_id)
    event_types = {cast(str, event["event_type"]) for event in events}

    assert action_attempt["action_status"] == "rejected"
    assert action_attempt["execution_status"] == "not_executed"
    assert rejection["error_code"] == "action_rejected_safety_control"
    assert rejection["reason_code"] == "action_kill_switch_active"
    assert isinstance(rejection["trace_id"], str)
    assert "safety_control_decision" in event_types
    assert "action_execution_requested" not in event_types


def test_step_up_failed_proof_path_rejects_and_keeps_trace_audit_linkage() -> None:
    confirmed_record = _confirmed_record()
    action_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    verification = verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], challenge["challenge_record"]),
        proof_code="000000",
        verified_at="2026-03-20T00:02:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object] | None, binding["proof_binding"]),
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    envelope = build_income_tax_action_final_outcome_envelope(authorization)
    result = cast(dict[str, object], envelope["result"])
    rejection = cast(dict[str, object], result["rejection"])
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])

    assert verification["verification_status"] == "failed"
    assert verification["reason_code"] == "proof_invalid"
    assert binding["binding_status"] == "rejected"
    assert binding["reason_code"] == "step_up_verification_not_verified"
    assert envelope["outcome_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_step_up_proof"
    assert rejection["reason_code"] == "step_up_proof_missing"
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert "step_up_verification_result" in cast(list[str], audit["event_types"])


def test_step_up_expired_path_rejects_deterministically() -> None:
    action_attempt = attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    verification = verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], challenge["challenge_record"]),
        proof_code="246810",
        verified_at="2026-03-20T00:10:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )

    assert verification["verification_status"] == "expired"
    assert verification["reason_code"] == "challenge_expired"
    assert binding["binding_status"] == "rejected"
    assert binding["reason_code"] == "step_up_verification_not_verified"


def test_retryable_adapter_failure_replays_without_duplicate_execution() -> None:
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
    second = execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_counted_retryable_dispatch,
        idempotency_store=store,
    )

    assert dispatch_count == 1
    assert first["mapped_result"]["action_status"] == "retryable_failure"
    assert first["mapped_result"]["retryable"] is True
    assert isinstance(first["mapped_result"]["trace_id"], str)
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_idempotency_conflict_rejects_and_prevents_duplicate_adapter_dispatch() -> None:
    store = InMemoryActionExecutionIdempotencyStore()
    dispatch_count = 0

    def _counted_pending_dispatch(request: ActionAdapterRequest) -> ActionAdapterResponse:
        nonlocal dispatch_count
        dispatch_count += 1
        return _mock_pending_dispatch(request)

    request = _execution_request()
    execute_idempotent_action_request(
        request=deepcopy(request),
        dispatch_adapter_request=_counted_pending_dispatch,
        idempotency_store=store,
    )

    conflicting = deepcopy(request)
    conflicting["submission_payload_ref"] = "submission-preview-fi-conflict"
    conflict_response = execute_idempotent_action_request(
        request=conflicting,
        dispatch_adapter_request=_counted_pending_dispatch,
        idempotency_store=store,
    )
    error = cast(dict[str, object], conflict_response["error"])

    assert dispatch_count == 1
    assert conflict_response["execution_status"] == "rejected"
    assert conflict_response["mapped_result"]["action_status"] == "rejected"
    assert error["error_code"] == "idempotent_action_execution_rejected"
    assert error["reason_code"] == "idempotency_key_payload_conflict"
    assert isinstance(error["trace_id"], str)


def test_failure_envelope_drift_guard_detects_shape_mismatch() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(
        SUPPORTED_PROMPT,
        tenant_id="pilot_tenant_unknown",
    )
    result = cast(dict[str, object], envelope["result"])
    _assert_failure_result_shape(result)

    drifted = deepcopy(result)
    drifted.pop("reason_code")
    with pytest.raises(AssertionError):
        _assert_failure_result_shape(drifted)


def _assert_failure_result_shape(result: dict[str, object]) -> None:
    required_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "rejected_context",
        "correlation_id",
        "trace_id",
    }
    assert required_fields.issubset(set(result))
    assert isinstance(result["rejected_context"], dict)
    assert isinstance(result["correlation_id"], str)
    assert isinstance(result["trace_id"], str)
