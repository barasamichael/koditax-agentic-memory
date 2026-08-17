"""Verify deterministic submission action-adapter contract behavior for pilot orchestration."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Mapping

import pytest

from tests import income_tax_prompt_flow_support as prompt_flow_support
from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_registry import dispatch_submission_action_request
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import (
    reset_default_action_execution_idempotency_store,
)

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)


def _supported_adapter_request() -> ActionAdapterRequest:
    return {
        "action_type": "submission_execute",
        "correlation_id": "corr-action-adapter-001",
        "submission_payload_ref": "submission-payload-preview-001",
        "capability_context": {
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
        },
    }


def _unsupported_adapter_request() -> ActionAdapterRequest:
    request = deepcopy(_supported_adapter_request())
    request["action_type"] = "unsupported_external_action"
    request["correlation_id"] = "corr-action-adapter-unsupported-001"
    return request


def _confirmed_record() -> dict[str, object]:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    awaiting_record = cast(Mapping[str, object], awaiting["state_record"])
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_record,
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def test_supported_submission_action_is_accepted_by_adapter_contract() -> None:
    result = dispatch_submission_action_request(_supported_adapter_request())

    assert result["adapter_status"] == "mock_pending"
    assert result["provider_reference"] is None
    assert result["action_result_code"] == "submission_action_mock_pending"
    assert result["error"] is None
    assert result["message"] == (
        "Submission action accepted by deterministic adapter contract. "
        "External provider execution is not enabled in this phase."
    )
    trace = cast(dict[str, object], result["trace"])
    assert trace["correlation_id"] == "corr-action-adapter-001"
    assert isinstance(trace["trace_id"], str)
    assert len(trace["trace_id"]) == 64
    assert trace["adapter_name"] == "deterministic_submission_mock_adapter_v1"
    assert trace["submission_payload_ref"] == "submission-payload-preview-001"
    assert isinstance(trace["adapter_request_id"], str)
    assert len(trace["adapter_request_id"]) == 64


def test_unknown_action_type_is_rejected_deterministically() -> None:
    result = dispatch_submission_action_request(_unsupported_adapter_request())

    error = cast(dict[str, object], result["error"])
    assert result["adapter_status"] == "unsupported"
    assert result["provider_reference"] is None
    assert result["action_result_code"] == "unsupported_action_type"
    assert error["error_code"] == "unsupported_submission_action"
    assert error["reason_code"] == "unsupported_action_type"
    assert error["required_controls"] == ["revise_action_type"]
    assert error["next_allowed_actions"] == ["revise_input", "reject"]


def test_identical_adapter_request_returns_identical_response() -> None:
    request = _supported_adapter_request()
    first = dispatch_submission_action_request(deepcopy(request))
    second = dispatch_submission_action_request(deepcopy(request))
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_prompt_flow_boundary_uses_adapter_contract_not_direct_execution_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_default_action_execution_idempotency_store()
    adapter_dispatch_count = 0
    execution_adapter_count = 0

    def _adapter_dispatch_stub(request: ActionExecutionRequest) -> dict[str, object]:
        nonlocal adapter_dispatch_count
        adapter_dispatch_count += 1
        return {
            "idempotency_key": request["idempotency_key"],
            "correlation_id": request["correlation_id"],
            "request_fingerprint": "c" * 64,
            "action_context": {
                "action_type": request["action_type"],
                "supported_lane_id": request["capability_context"]["supported_lane_id"],
                "historical_version_id": request["capability_context"]["historical_version_id"],
                "tax_year": request["capability_context"]["tax_year"],
            },
            "execution_status": "resolved",
            "adapter_response": {
                "adapter_status": "mock_pending",
                "provider_reference": None,
                "action_result_code": "submission_action_mock_pending",
                "message": "stubbed adapter dispatch",
                "trace": {
                    "correlation_id": request["correlation_id"],
                    "trace_id": "b" * 64,
                    "adapter_request_id": "a" * 64,
                    "adapter_name": "stubbed_adapter",
                    "submission_payload_ref": request["submission_payload_ref"],
                },
                "error": None,
            },
            "error": None,
            "trace": {
                "correlation_id": request["correlation_id"],
                "trace_id": "d" * 64,
                "execution_envelope_id": "d" * 64,
                "idempotency_key": request["idempotency_key"],
                "request_fingerprint": "c" * 64,
            },
        }

    def _execution_adapter() -> Mapping[str, object]:
        nonlocal execution_adapter_count
        execution_adapter_count += 1
        return {"status": "executed"}

    monkeypatch.setattr(
        prompt_flow_support,
        "dispatch_submission_action_request_with_envelope",
        _adapter_dispatch_stub,
    )
    result = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="low",
        execution_adapter=_execution_adapter,
    )

    adapter_response = cast(dict[str, object], result["adapter_response"])
    execution_envelope = cast(dict[str, object], result["execution_envelope"])
    assert result["action_status"] == "allowed"
    assert result["execution_status"] == "not_executed"
    assert execution_envelope["execution_status"] == "resolved"
    assert adapter_response["adapter_status"] == "mock_pending"
    assert adapter_dispatch_count == 1
    assert execution_adapter_count == 0


def test_adapter_response_contract_keys_are_stable() -> None:
    supported = dispatch_submission_action_request(_supported_adapter_request())
    unsupported = dispatch_submission_action_request(_unsupported_adapter_request())

    assert set(supported.keys()) == {
        "adapter_status",
        "provider_reference",
        "action_result_code",
        "message",
        "trace",
        "error",
    }
    assert set(cast(dict[str, object], unsupported["error"]).keys()) == {
        "error_code",
        "message",
        "reason_code",
        "reason",
        "rejected_context",
        "required_controls",
        "next_allowed_actions",
        "trace_id",
    }
