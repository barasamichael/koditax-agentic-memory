"""Verify deterministic action policy gate behavior for confirmation and risk controls."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Mapping

from tests import income_tax_prompt_flow_support as prompt_flow_support
from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.action_policy_gate import evaluate_income_tax_action_policy

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)


def _confirmed_record() -> dict[str, object]:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(Mapping[str, object], awaiting["state_record"])
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def test_confirmed_low_risk_side_effect_capable_action_is_allowed() -> None:
    record = _confirmed_record()
    decision = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=record,
        action_type="submission_execute",
        risk_class="low",
    )

    assert decision["policy_decision"] == "allowed"
    assert decision["step_up_decision"] == "not_required"
    assert decision["step_up_reason_code"] == "step_up_not_required"
    assert decision["reason_code"] == "policy_allow"
    assert decision["required_controls"] == []


def test_awaiting_confirmation_side_effect_capable_action_is_blocked() -> None:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(Mapping[str, object], awaiting["state_record"])
    decision = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=awaiting_state_record,
        action_type="submission_execute",
        risk_class="low",
    )

    assert decision["policy_decision"] == "blocked"
    assert decision["step_up_decision"] == "not_required"
    assert decision["step_up_reason_code"] == "step_up_not_required"
    assert decision["reason_code"] == "confirmation_required"
    assert decision["required_controls"] == ["confirmed_state"]


def test_confirmed_high_risk_side_effect_capable_action_is_step_up_required() -> None:
    record = _confirmed_record()
    decision = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=record,
        action_type="submission_execute",
        risk_class="high",
    )

    assert decision["policy_decision"] == "step_up_required"
    assert decision["step_up_decision"] == "required"
    assert decision["step_up_reason_code"] == "step_up_required_for_high_risk_action"
    assert decision["reason_code"] == "step_up_auth_required"
    assert decision["required_controls"] == ["step_up_auth"]


def test_unknown_action_type_or_risk_class_is_blocked_deterministically() -> None:
    record = _confirmed_record()
    unknown_action_decision = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=record,
        action_type="unsupported_action",
        risk_class="low",
    )
    unknown_risk_decision = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=record,
        action_type="submission_execute",
        risk_class="unsupported_risk",
    )

    assert unknown_action_decision["policy_decision"] == "blocked"
    assert unknown_action_decision["step_up_decision"] == "unsupported_context"
    assert unknown_action_decision["step_up_reason_code"] == "unsupported_action_type"
    assert unknown_action_decision["reason_code"] == "unsupported_action_type"
    assert unknown_risk_decision["policy_decision"] == "blocked"
    assert unknown_risk_decision["step_up_decision"] == "unsupported_context"
    assert unknown_risk_decision["step_up_reason_code"] == "unsupported_risk_class"
    assert unknown_risk_decision["reason_code"] == "unsupported_risk_class"


def test_same_action_policy_input_yields_identical_deterministic_output() -> None:
    record = _confirmed_record()
    first = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=deepcopy(record),
        action_type="submission_execute",
        risk_class="high",
    )
    second = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=deepcopy(record),
        action_type="submission_execute",
        risk_class="high",
    )

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_read_only_action_is_allowed_without_confirmed_state() -> None:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(Mapping[str, object], awaiting["state_record"])
    decision = prompt_flow_support.evaluate_income_tax_action_request_policy(
        confirmation_record=awaiting_state_record,
        action_type="read_only_review",
        risk_class="low",
    )

    assert decision["policy_decision"] == "allowed"
    assert decision["step_up_decision"] == "not_required"
    assert decision["step_up_reason_code"] == "step_up_not_required"
    assert decision["reason_code"] == "policy_allow"


def test_invalid_confirmation_state_is_blocked() -> None:
    decision = evaluate_income_tax_action_policy(
        current_state="unknown_state",
        action_type="submission_execute",
        risk_class="low",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid",
    )

    assert decision["policy_decision"] == "blocked"
    assert decision["step_up_decision"] == "unsupported_context"
    assert decision["step_up_reason_code"] == "invalid_confirmation_state"
    assert decision["reason_code"] == "invalid_confirmation_state"
