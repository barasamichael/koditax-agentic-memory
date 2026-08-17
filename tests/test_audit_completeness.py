"""Verify deterministic audit-event completeness across intent-to-action control stages."""

from __future__ import annotations

from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import bind_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import get_income_tax_step_up_test_proof_code
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import verify_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import get_income_tax_audit_events_for_correlation
from tests.income_tax_prompt_flow_support import authorize_income_tax_action_with_step_up_proof

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
UNSUPPORTED_PROMPT = "Compute VAT filing output for Q3 and submit to regulator."

REQUIRED_FULL_PATH_EVENT_TYPES = {
    "intent_parsed",
    "plan_generated",
    "plan_validated",
    "evidence_lineage_linked",
    "confirmation_transition",
    "policy_decision",
    "step_up_challenge_issued",
    "step_up_verification_result",
    "action_execution_requested",
    "action_execution_result_mapped",
}


def test_full_supported_flow_emits_complete_required_audit_events() -> None:
    events = _run_full_supported_high_risk_flow_and_collect_events()

    _assert_required_event_types(events, REQUIRED_FULL_PATH_EVENT_TYPES)
    _assert_common_event_fields(events)
    assert [events[0]["event_type"], events[1]["event_type"], events[2]["event_type"]] == [
        "intent_parsed",
        "plan_generated",
        "plan_validated",
    ]


def test_blocked_unsupported_flow_emits_rejection_audit_evidence() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(UNSUPPORTED_PROMPT)

    details = error_info.value.details()
    correlation_id = cast(str, details["correlation_id"])
    trace_id = cast(str, details["trace_id"])
    events = get_income_tax_audit_events_for_correlation(correlation_id)

    assert events
    _assert_common_event_fields(events)
    event_types = [cast(str, event["event_type"]) for event in events]
    assert "intent_parsed" in event_types
    assert "plan_generated" in event_types
    rejected_plan_events = [
        event
        for event in events
        if event["event_type"] == "plan_generated" and event["status"] == "rejected"
    ]
    assert rejected_plan_events
    assert all(event["trace_id"] == trace_id for event in events)


def test_step_up_failed_and_expired_paths_emit_verification_audit_evidence() -> None:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    correlation_id = cast(str, draft["correlation_id"])
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_record,
        decision="confirm",
    )
    confirmed_record = cast(dict[str, object], confirmed["state_record"])

    failed_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
    )
    failed_challenge = cast(dict[str, object], failed_attempt["step_up_challenge"])
    verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], failed_challenge["challenge_record"]),
        proof_code="000000",
        verified_at="2026-03-20T00:02:00+03:00",
    )
    expired_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
    )
    expired_challenge = cast(dict[str, object], expired_attempt["step_up_challenge"])
    verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], expired_challenge["challenge_record"]),
        proof_code=get_income_tax_step_up_test_proof_code(),
        verified_at="2026-03-20T00:06:00+03:00",
    )

    events = get_income_tax_audit_events_for_correlation(correlation_id)
    verification_statuses = [
        cast(str, event["status"])
        for event in events
        if event["event_type"] == "step_up_verification_result"
    ]
    assert "failed" in verification_statuses
    assert "expired" in verification_statuses


def test_repeated_identical_flow_yields_identical_audit_event_structure_and_order() -> None:
    first = _run_full_supported_high_risk_flow_and_collect_events()
    second = _run_full_supported_high_risk_flow_and_collect_events()

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_audit_completeness_guard_fails_when_required_event_missing() -> None:
    events = _run_full_supported_high_risk_flow_and_collect_events()
    drifted = [event for event in events if event["event_type"] != "plan_validated"]

    with pytest.raises(AssertionError):
        _assert_required_event_types(drifted, REQUIRED_FULL_PATH_EVENT_TYPES)


def _run_full_supported_high_risk_flow_and_collect_events() -> list[dict[str, object]]:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    correlation_id = cast(str, draft["correlation_id"])
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_record,
        decision="confirm",
    )
    confirmed_record = cast(dict[str, object], confirmed["state_record"])
    action_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    verification = verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], challenge["challenge_record"]),
        proof_code=get_income_tax_step_up_test_proof_code(),
        verified_at="2026-03-20T00:02:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object], binding["proof_binding"]),
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    return get_income_tax_audit_events_for_correlation(correlation_id)


def _assert_required_event_types(
    events: list[dict[str, object]],
    required_event_types: set[str],
) -> None:
    event_types = {cast(str, event["event_type"]) for event in events}
    assert required_event_types.issubset(event_types)


def _assert_common_event_fields(events: list[dict[str, object]]) -> None:
    for event in events:
        assert isinstance(event["event_type"], str)
        assert isinstance(event["event_time"], str)
        assert isinstance(event["status"], str)
        assert isinstance(event["correlation_id"], str)
        assert isinstance(event["trace_id"], str)
