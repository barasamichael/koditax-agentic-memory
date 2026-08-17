"""Verify validator-gated prompt-flow behavior before any side effects run."""

from __future__ import annotations

from copy import deepcopy

import pytest

import tests.income_tax_prompt_flow_support as prompt_flow_support
from services.orchestration.app.intent_to_plan import IncomeTaxOrchestrationPlan
from services.orchestration.app.intent_to_plan import translate_income_tax_intent_to_plan
from services.orchestration.app.prompt_intent_envelope import PromptIntentEnvelope
from services.orchestration.app.prompt_intent_envelope import (
    parse_income_tax_prompt_intent_envelope,
)


def test_prompt_flow_blocks_dispatch_when_intent_plan_validation_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    envelope = parse_income_tax_prompt_intent_envelope(prompt_text)
    invalid_plan = deepcopy(translate_income_tax_intent_to_plan(envelope))
    invalid_plan["steps"][0]["action_ref"] = "unsupported_side_effect_action"

    def _invalid_plan_for_dispatch(
        _: PromptIntentEnvelope,
    ) -> IncomeTaxOrchestrationPlan:
        return deepcopy(invalid_plan)

    execution_called = {"value": False}

    def _fail_if_execution_runs(_: object) -> object:
        execution_called["value"] = True
        raise AssertionError("Execution should not run after plan validation rejection.")

    monkeypatch.setattr(
        prompt_flow_support,
        "translate_income_tax_intent_to_plan",
        _invalid_plan_for_dispatch,
    )
    monkeypatch.setattr(
        prompt_flow_support,
        "execute_computation",
        _fail_if_execution_runs,
    )

    with pytest.raises(prompt_flow_support.IncomeTaxPromptFlowError) as error_info:
        prompt_flow_support.execute_income_tax_prompt_flow(prompt_text)

    details = error_info.value.details()
    assert execution_called["value"] is False
    assert error_info.value.reason == "unsupported_prompt_scope"
    assert details == {
        "reason": "unsupported_step_action",
        "error_code": "unsupported_prompt_scope",
        "message": "Plan step action is outside deterministic allowlisted supported scope.",
        "rejected_context": {
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
            "tax_domain": "income_tax",
            "prompt_class": "income_tax_prompt_flow",
        },
        "correlation_id": details["correlation_id"],
        "trace_id": details["trace_id"],
    }
