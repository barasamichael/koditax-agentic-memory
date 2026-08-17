"""Verify deterministic pilot feature-flag and kill-switch safety control behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Mapping
from collections.abc import Generator

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import get_income_tax_audit_events_for_correlation
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome
from services.orchestration.app.feature_flags import set_action_flag
from services.orchestration.app.feature_flags import set_kill_switch
from services.orchestration.app.feature_flags import set_capability_flag
from services.orchestration.app.feature_flags import reset_runtime_safety_control_config

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
SUPPORTED_LANE_ID = "resident_employment_income_2023_07_01"


@pytest.fixture(autouse=True)
def _reset_runtime_safety_controls() -> (
    Generator[None, None, None]
):  # pyright: ignore[reportUnusedFunction]
    reset_runtime_safety_control_config()
    yield
    reset_runtime_safety_control_config()


def _confirmed_record() -> dict[str, object]:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prepare_income_tax_confirmation_review(draft)
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=cast(Mapping[str, object], awaiting["state_record"]),
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def test_enabled_capability_and_action_execute_normally() -> None:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    result = attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="low",
    )

    assert draft["status"] == "draft_ready"
    assert result["action_status"] == "allowed"
    assert result["rejection"] is None


def test_disabled_capability_is_rejected_deterministically() -> None:
    set_capability_flag(capability_key=SUPPORTED_LANE_ID, enabled=False)

    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(SUPPORTED_PROMPT)

    details = error_info.value.details()
    assert details["error_code"] == "unsupported_prompt_scope"
    assert details["reason"] == "capability_disabled_by_flag"
    assert details["reason_code"] == "capability_disabled_by_flag"
    rejected_context = cast(dict[str, object], details["rejected_context"])
    assert rejected_context["supported_lane_id"] == SUPPORTED_LANE_ID


def test_disabled_action_is_rejected_deterministically() -> None:
    set_action_flag(action_key="submission_execute", enabled=False)

    result = attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="low",
    )
    rejection = cast(dict[str, object], result["rejection"])

    assert result["action_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_safety_control"
    assert rejection["reason_code"] == "action_disabled_by_flag"
    assert rejection["required_controls"] == ["enable_action_flag"]


def test_kill_switch_hard_blocks_targeted_action_path() -> None:
    set_kill_switch(switch_key="action:submission_execute", enabled=True)

    result = attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="low",
    )
    rejection = cast(dict[str, object], result["rejection"])

    assert result["action_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_safety_control"
    assert rejection["reason_code"] == "action_kill_switch_active"
    assert rejection["required_controls"] == ["disable_kill_switch"]


def test_same_flag_state_and_input_yields_identical_output() -> None:
    set_capability_flag(capability_key=SUPPORTED_LANE_ID, enabled=False)

    def _payload() -> dict[str, object]:
        with pytest.raises(IncomeTaxPromptFlowError) as error_info:
            execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
        return error_info.value.details()

    assert canonical_json_dumps(_payload()) == canonical_json_dumps(_payload())


def test_safety_control_decisions_are_traceable_and_auditable() -> None:
    set_capability_flag(capability_key=SUPPORTED_LANE_ID, enabled=False)

    final_envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    trace = cast(dict[str, object], final_envelope["trace"])
    audit = cast(dict[str, object], final_envelope["audit"])
    result = cast(dict[str, object], final_envelope["result"])
    correlation_id = cast(str, trace["correlation_id"])
    events = get_income_tax_audit_events_for_correlation(correlation_id)

    assert final_envelope["outcome_status"] == "rejected"
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert "safety_control_decision" in cast(list[str], audit["event_types"])
    assert result["reason_code"] == "capability_disabled_by_flag"

    safety_events = [event for event in events if event["event_type"] == "safety_control_decision"]
    assert safety_events
    latest = deepcopy(safety_events[-1])
    context = cast(dict[str, object], latest["context"])
    assert latest["correlation_id"] == trace["correlation_id"]
    assert latest["trace_id"] == trace["trace_id"]
    assert context["control_scope"] == "capability"
    assert context["reason_code"] == "capability_disabled_by_flag"
