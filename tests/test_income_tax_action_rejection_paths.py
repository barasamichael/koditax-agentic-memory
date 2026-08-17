"""Verify explicit deterministic rejection paths for blocked income-tax action requests."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Mapping

from tests import income_tax_prompt_flow_support as prompt_flow_support
from shared.determinism.input_hash import canonical_json_dumps

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)


def _awaiting_record() -> dict[str, object]:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    return cast(dict[str, object], awaiting["state_record"])


def _confirmed_record() -> dict[str, object]:
    awaiting_state_record = _awaiting_record()
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=cast(Mapping[str, object], awaiting_state_record),
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def test_unconfirmed_side_effect_action_returns_canonical_rejection() -> None:
    result = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_awaiting_record(),
        action_type="submission_execute",
        risk_class="low",
    )

    rejection = cast(dict[str, object], result["rejection"])
    rejected_context = cast(dict[str, object], rejection["rejected_context"])

    assert result["action_status"] == "rejected"
    assert result["execution_status"] == "not_executed"
    assert rejection["error_code"] == "action_rejected_unconfirmed"
    assert rejection["reason_code"] == "confirmation_required"
    assert rejection["required_controls"] == ["confirmation"]
    assert rejection["next_allowed_actions"] == ["confirm", "reject", "revise_input"]
    assert rejected_context["action_type"] == "submission_execute"
    assert rejected_context["risk_class"] == "low"


def test_high_risk_action_issues_step_up_challenge_without_execution() -> None:
    result = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="high",
    )

    challenge = cast(dict[str, object], result["step_up_challenge"])
    assert result["action_status"] == "step_up_challenge_issued"
    assert result["execution_status"] == "not_executed"
    assert result["rejection"] is None
    assert challenge["challenge_status"] == "issued"
    assert challenge["reason_code"] == "step_up_challenge_issued"
    assert challenge["allowed_attempts"] == 3


def test_malformed_action_context_returns_canonical_rejection() -> None:
    result = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="unsupported_action",
        risk_class="low",
    )

    rejection = cast(dict[str, object], result["rejection"])
    assert result["action_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_invalid_context"
    assert rejection["reason_code"] == "unsupported_action_type"
    assert rejection["required_controls"] == ["revise_action_context"]


def test_blocked_rejection_payload_is_deterministic() -> None:
    confirmation_record = _awaiting_record()
    first = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=deepcopy(confirmation_record),
        action_type="submission_execute",
        risk_class="low",
    )
    second = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=deepcopy(confirmation_record),
        action_type="submission_execute",
        risk_class="low",
    )

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_blocked_action_never_calls_execution_adapter() -> None:
    adapter_call_count = 0

    def _adapter() -> Mapping[str, object]:
        nonlocal adapter_call_count
        adapter_call_count += 1
        return {"status": "executed"}

    result = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_awaiting_record(),
        action_type="submission_execute",
        risk_class="low",
        execution_adapter=_adapter,
    )

    assert result["action_status"] == "rejected"
    assert result["execution_status"] == "not_executed"
    assert adapter_call_count == 0


def test_allowed_read_only_action_preserves_success_behavior() -> None:
    def _adapter() -> Mapping[str, object]:
        return {"status": "review_complete", "artifact_ref": "artifact-preview-001"}

    result = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_awaiting_record(),
        action_type="read_only_review",
        risk_class="low",
        execution_adapter=_adapter,
    )

    execution_result = cast(dict[str, object], result["execution_result"])
    assert result["action_status"] == "allowed"
    assert result["execution_status"] == "executed"
    assert execution_result["status"] == "review_complete"
