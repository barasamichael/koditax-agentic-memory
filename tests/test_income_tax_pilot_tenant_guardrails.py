"""Verify deterministic pilot tenant allowlist and runtime guardrail enforcement."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Mapping

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import get_income_tax_audit_events_for_correlation
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome

SUPPORTED_PROMPT_2023 = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
SUPPORTED_PROMPT_2021 = (
    "Compute income tax for resident employment lane in tax year 2021 under KIT-VER-20210101-A."
)


def _confirmed_record(*, tenant_id: str) -> dict[str, object]:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT_2023, tenant_id=tenant_id)
    awaiting = prepare_income_tax_confirmation_review(draft)
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=cast(Mapping[str, object], awaiting["state_record"]),
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def test_allowlisted_tenant_for_supported_lane_passes_prompt_guardrail() -> None:
    result = execute_income_tax_prompt_flow(
        SUPPORTED_PROMPT_2023,
        tenant_id="pilot_tenant_alpha",
    )

    assert result["status"] == "draft_ready"


def test_missing_tenant_context_is_rejected_deterministically() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(SUPPORTED_PROMPT_2023, tenant_id=None)

    details = error_info.value.details()
    rejected_context = cast(dict[str, object], details["rejected_context"])

    assert error_info.value.reason == "pilot_tenant_not_allowed"
    assert details["error_code"] == "pilot_tenant_not_allowed"
    assert details["reason"] == "missing_tenant_context"
    assert details["reason_code"] == "missing_tenant_context"
    assert rejected_context["tenant_id"] is None


def test_unknown_tenant_is_rejected_deterministically() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(
            SUPPORTED_PROMPT_2023,
            tenant_id="pilot_tenant_unknown",
        )

    details = error_info.value.details()
    rejected_context = cast(dict[str, object], details["rejected_context"])

    assert details["error_code"] == "pilot_tenant_not_allowed"
    assert details["reason"] == "tenant_not_allowlisted"
    assert rejected_context["tenant_id"] == "pilot_tenant_unknown"


def test_disabled_tenant_is_rejected_deterministically() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(
            SUPPORTED_PROMPT_2023,
            tenant_id="pilot_tenant_disabled",
        )

    details = error_info.value.details()
    rejected_context = cast(dict[str, object], details["rejected_context"])

    assert details["error_code"] == "pilot_tenant_not_allowed"
    assert details["reason"] == "tenant_disabled"
    assert rejected_context["tenant_id"] == "pilot_tenant_disabled"


def test_allowlisted_tenant_with_disallowed_lane_is_rejected_deterministically() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(
            SUPPORTED_PROMPT_2021,
            tenant_id="pilot_tenant_limited",
        )

    details = error_info.value.details()
    rejected_context = cast(dict[str, object], details["rejected_context"])

    assert details["error_code"] == "pilot_tenant_not_allowed"
    assert details["reason"] == "tenant_lane_not_allowed"
    assert rejected_context["tenant_id"] == "pilot_tenant_limited"
    assert rejected_context["supported_lane_id"] == "resident_employment_income_2021_01_01"


def test_allowlisted_tenant_with_disallowed_action_is_blocked_before_execution() -> None:
    adapter_call_count = 0

    def _adapter() -> Mapping[str, object]:
        nonlocal adapter_call_count
        adapter_call_count += 1
        return {"status": "executed"}

    result = attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(tenant_id="pilot_tenant_limited"),
        action_type="submission_execute",
        risk_class="low",
        tenant_id="pilot_tenant_limited",
        execution_adapter=_adapter,
    )
    rejection = cast(dict[str, object], result["rejection"])

    assert result["action_status"] == "rejected"
    assert result["execution_status"] == "not_executed"
    assert rejection["error_code"] == "pilot_tenant_not_allowed"
    assert rejection["reason_code"] == "tenant_action_not_allowed"
    assert adapter_call_count == 0


def test_same_denied_tenant_request_yields_identical_payload() -> None:
    def _payload() -> dict[str, object]:
        with pytest.raises(IncomeTaxPromptFlowError) as error_info:
            execute_income_tax_prompt_flow(
                SUPPORTED_PROMPT_2023,
                tenant_id="pilot_tenant_unknown",
            )
        return error_info.value.details()

    assert canonical_json_dumps(_payload()) == canonical_json_dumps(_payload())


def test_tenant_guard_decision_is_traceable_and_auditable() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(
        SUPPORTED_PROMPT_2023,
        tenant_id="pilot_tenant_disabled",
    )
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    correlation_id = cast(str, trace["correlation_id"])
    events = get_income_tax_audit_events_for_correlation(correlation_id)

    assert envelope["outcome_status"] == "rejected"
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert "pilot_tenant_guard_decision" in cast(list[str], audit["event_types"])
    assert result["reason_code"] == "tenant_disabled"

    guard_events = [
        event for event in events if event["event_type"] == "pilot_tenant_guard_decision"
    ]
    assert guard_events
    latest = deepcopy(guard_events[-1])
    context = cast(dict[str, object], latest["context"])
    assert latest["correlation_id"] == trace["correlation_id"]
    assert latest["trace_id"] == trace["trace_id"]
    assert context["tenant_id"] == "pilot_tenant_disabled"
    assert context["reason_code"] == "tenant_disabled"
