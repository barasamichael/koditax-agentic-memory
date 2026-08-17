"""Verify deterministic confirmation-state lifecycle handling for draft outcomes."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from tests import income_tax_prompt_flow_support as prompt_flow_support
from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.confirmation_state import initialize_income_tax_confirmation_state
from services.orchestration.app.confirmation_state import transition_income_tax_confirmation_state

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)


def test_confirmation_transitions_apply_for_supported_flow() -> None:
    draft_result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)

    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft_result)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    awaiting_lineage = cast(dict[str, object], awaiting["lineage"])
    draft_lineage = cast(dict[str, object], draft_result["lineage"])
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    assert awaiting["transition_status"] == "applied"
    assert awaiting["previous_state"] == "draft_ready"
    assert awaiting["next_state"] == "awaiting_confirmation"
    assert awaiting_state_record["current_state"] == "awaiting_confirmation"
    assert awaiting_lineage["prompt_id"] == draft_result["prompt_id"]
    assert awaiting_lineage["computation_id"] == draft_lineage["computation_id"]

    assert confirmed["transition_status"] == "applied"
    assert confirmed["previous_state"] == "awaiting_confirmation"
    assert confirmed["next_state"] == "confirmed"
    assert confirmed_state_record["current_state"] == "confirmed"
    assert confirmed["error"] is None


def test_confirmation_rejected_branch_applies_for_supported_flow() -> None:
    draft_result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)

    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft_result)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    rejected = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="reject",
    )
    rejected_state_record = cast(dict[str, object], rejected["state_record"])

    assert rejected["transition_status"] == "applied"
    assert rejected["previous_state"] == "awaiting_confirmation"
    assert rejected["next_state"] == "rejected"
    assert rejected_state_record["current_state"] == "rejected"


def test_invalid_confirmation_transition_is_rejected_deterministically() -> None:
    draft_result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft_result)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    invalid = transition_income_tax_confirmation_state(
        confirmation_record=confirmed_state_record,
        target_state="draft_ready",
    )

    assert invalid["transition_status"] == "rejected"
    assert invalid["reason"] == "invalid_state_transition"
    assert invalid["error"] == {
        "error_code": "invalid_confirmation_transition",
        "message": "Confirmation state transition is not allowed for current state.",
        "reason": "invalid_state_transition",
        "rejected_context": {
            "previous_state": "confirmed",
            "requested_next_state": "draft_ready",
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
            "prompt_id": draft_result["prompt_id"],
        },
    }


def test_unknown_target_state_is_rejected_deterministically() -> None:
    draft_result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    confirmation_record = initialize_income_tax_confirmation_state(draft_outcome=draft_result)

    invalid = transition_income_tax_confirmation_state(
        confirmation_record=confirmation_record,
        target_state="externally_submitted",
    )

    assert invalid["transition_status"] == "rejected"
    assert invalid["reason"] == "invalid_target_state"
    assert invalid["error"] is not None
    assert invalid["error"]["error_code"] == "invalid_confirmation_transition"


def test_confirmation_transition_is_deterministic_for_identical_input() -> None:
    draft_result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    confirmation_record = initialize_income_tax_confirmation_state(draft_outcome=draft_result)

    first = transition_income_tax_confirmation_state(
        confirmation_record=deepcopy(confirmation_record),
        target_state="awaiting_confirmation",
    )
    second = transition_income_tax_confirmation_state(
        confirmation_record=deepcopy(confirmation_record),
        target_state="awaiting_confirmation",
    )

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_confirmation_transitions_do_not_trigger_action_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_result = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    action_called = {"value": False}

    def _fail_if_called(*_: object, **__: object) -> None:
        action_called["value"] = True
        raise AssertionError("No action execution should be triggered by confirmation transitions.")

    monkeypatch.setattr(prompt_flow_support, "execute_computation", _fail_if_called)
    monkeypatch.setattr(
        prompt_flow_support,
        "construct_income_tax_submission_payload",
        _fail_if_called,
    )

    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft_result)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    _ = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )

    assert action_called["value"] is False
