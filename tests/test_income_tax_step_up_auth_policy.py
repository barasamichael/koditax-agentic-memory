"""Verify deterministic step-up auth policy decisions for income-tax pilot actions."""

from __future__ import annotations

from copy import deepcopy

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.step_up_auth_policy import evaluate_income_tax_step_up_auth_policy


def test_high_risk_side_effect_action_requires_step_up() -> None:
    decision = evaluate_income_tax_step_up_auth_policy(
        current_state="confirmed",
        action_type="submission_execute",
        risk_class="high",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-001",
    )

    assert decision["step_up_decision"] == "required"
    assert decision["reason_code"] == "step_up_required_for_high_risk_action"
    assert decision["required_controls"] == ["step_up_auth"]


def test_low_risk_and_read_only_actions_do_not_require_step_up() -> None:
    low_risk_side_effect = evaluate_income_tax_step_up_auth_policy(
        current_state="confirmed",
        action_type="submission_execute",
        risk_class="low",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-002",
    )
    read_only = evaluate_income_tax_step_up_auth_policy(
        current_state="awaiting_confirmation",
        action_type="read_only_review",
        risk_class="low",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-003",
    )

    assert low_risk_side_effect["step_up_decision"] == "not_required"
    assert low_risk_side_effect["reason_code"] == "step_up_not_required"
    assert read_only["step_up_decision"] == "not_required"
    assert read_only["reason_code"] == "step_up_not_required"


def test_unknown_context_safe_fails_with_unsupported_context() -> None:
    unknown_action = evaluate_income_tax_step_up_auth_policy(
        current_state="confirmed",
        action_type="unknown_action",
        risk_class="high",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-004",
    )
    unknown_risk = evaluate_income_tax_step_up_auth_policy(
        current_state="confirmed",
        action_type="submission_execute",
        risk_class="unknown_risk",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-005",
    )

    assert unknown_action["step_up_decision"] == "unsupported_context"
    assert unknown_action["reason_code"] == "unsupported_action_type"
    assert unknown_risk["step_up_decision"] == "unsupported_context"
    assert unknown_risk["reason_code"] == "unsupported_risk_class"


def test_step_up_policy_is_deterministic_for_same_input() -> None:
    template = evaluate_income_tax_step_up_auth_policy(
        current_state="confirmed",
        action_type="submission_execute",
        risk_class="high",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-006",
    )
    first = deepcopy(template)
    second = deepcopy(template)
    assert canonical_json_dumps(first) == canonical_json_dumps(second)


def test_no_2fa_required_low_risk_read_only_context_is_explicit() -> None:
    decision = evaluate_income_tax_step_up_auth_policy(
        current_state="awaiting_confirmation",
        action_type="read_only_review",
        risk_class="low",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
        correlation_id="cid-007",
    )

    assert decision["step_up_decision"] == "not_required"
    assert decision["required_controls"] == []
    assert decision["reason_code"] == "step_up_not_required"
