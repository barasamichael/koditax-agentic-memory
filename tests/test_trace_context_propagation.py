"""Verify deterministic trace_id and correlation_id propagation across prompt-action flow."""

from __future__ import annotations

from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)


def test_supported_prompt_flow_response_includes_trace_context() -> None:
    result = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    trace_context = cast(dict[str, object], result["trace_context"])

    assert isinstance(result["correlation_id"], str)
    assert isinstance(result["trace_id"], str)
    assert len(result["correlation_id"]) == 64
    assert len(result["trace_id"]) == 64
    assert trace_context["correlation_id"] == result["correlation_id"]
    assert trace_context["trace_id"] == result["trace_id"]


def test_intermediate_action_envelopes_preserve_same_trace_and_correlation() -> None:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prepare_income_tax_confirmation_review(draft)
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=cast(dict[str, object], awaiting["state_record"]),
        decision="confirm",
    )
    action = attempt_income_tax_action_request(
        confirmation_record=cast(dict[str, object], confirmed["state_record"]),
        action_type="submission_execute",
        risk_class="low",
    )

    policy = cast(dict[str, object], action["policy_decision"])
    execution_envelope = cast(dict[str, object], action["execution_envelope"])
    trace = cast(dict[str, object], execution_envelope["trace"])
    adapter_response = cast(dict[str, object], action["adapter_response"])
    adapter_trace = cast(dict[str, object], adapter_response["trace"])
    mapped_result = cast(dict[str, object], action["mapped_result"])

    assert policy["correlation_id"] == draft["correlation_id"]
    assert policy["trace_id"] == draft["trace_id"]
    assert trace["correlation_id"] == draft["correlation_id"]
    assert trace["trace_id"] == draft["trace_id"]
    assert adapter_trace["correlation_id"] == draft["correlation_id"]
    assert adapter_trace["trace_id"] == draft["trace_id"]
    assert mapped_result["correlation_id"] == draft["correlation_id"]
    assert mapped_result["trace_id"] == draft["trace_id"]


def test_unsupported_prompt_rejection_includes_trace_context() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow("Compute VAT filing output for Q3 and submit to regulator.")

    details = error_info.value.details()
    assert details["error_code"] == "unsupported_prompt_scope"
    assert isinstance(details["correlation_id"], str)
    assert isinstance(details["trace_id"], str)


def test_idempotent_replay_keeps_trace_and_correlation_linkage_stable() -> None:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prepare_income_tax_confirmation_review(draft)
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=cast(dict[str, object], awaiting["state_record"]),
        decision="confirm",
    )
    confirmation_record = cast(dict[str, object], confirmed["state_record"])
    first = attempt_income_tax_action_request(
        confirmation_record=confirmation_record,
        action_type="submission_execute",
        risk_class="low",
    )
    second = attempt_income_tax_action_request(
        confirmation_record=confirmation_record,
        action_type="submission_execute",
        risk_class="low",
    )

    assert canonical_json_dumps(second["execution_envelope"]) == canonical_json_dumps(
        first["execution_envelope"]
    )
