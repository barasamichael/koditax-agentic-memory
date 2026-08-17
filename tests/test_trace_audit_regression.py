"""Lock phase 6.6 trace/audit/final-envelope invariants against deterministic drift."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import bind_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import verify_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import get_income_tax_audit_events_for_correlation
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome
from tests.income_tax_prompt_flow_support import authorize_income_tax_action_with_step_up_proof
from tests.income_tax_prompt_flow_support import build_income_tax_action_final_outcome_envelope

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
UNSUPPORTED_PROMPT = "Compute VAT filing output for Q3 and submit to regulator."

REQUIRED_SUCCESS_AUDIT_EVENTS = {
    "intent_parsed",
    "plan_generated",
    "plan_validated",
}
REQUIRED_UNSUPPORTED_AUDIT_EVENTS = {
    "intent_parsed",
    "plan_generated",
}
REQUIRED_STEP_UP_BLOCKED_AUDIT_EVENTS = {
    "confirmation_transition",
    "policy_decision",
    "step_up_challenge_issued",
    "step_up_verification_result",
}


def test_success_final_outcome_links_trace_and_required_audit_events() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)

    _assert_final_outcome_shape(envelope)
    assert envelope["outcome_status"] == "success"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    event_types = set(cast(list[str], audit["event_types"]))

    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert REQUIRED_SUCCESS_AUDIT_EVENTS.issubset(event_types)
    assert cast(dict[str, str], audit["latest_event_id_by_type"])["intent_parsed"]
    assert trace["correlation_id"] == result["correlation_id"]
    assert trace["trace_id"] == result["trace_id"]


def test_unsupported_final_outcome_preserves_rejection_semantics_with_trace_and_audit() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(UNSUPPORTED_PROMPT)

    _assert_final_outcome_shape(envelope)
    assert envelope["outcome_status"] == "rejected"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    rejected_context = cast(dict[str, object], result["rejected_context"])
    event_types = set(cast(list[str], audit["event_types"]))

    assert result["error_code"] == "unsupported_prompt_scope"
    assert result["reason"] == "unsupported_domain"
    assert rejected_context["tax_domain"] == "vat"
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert REQUIRED_UNSUPPORTED_AUDIT_EVENTS.issubset(event_types)


def test_policy_step_up_blocked_outcome_has_canonical_rejection_with_trace_and_audit() -> None:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
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
        proof_code="000000",
        verified_at="2026-03-20T00:02:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object] | None, binding["proof_binding"]),
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    envelope = build_income_tax_action_final_outcome_envelope(authorization)

    _assert_final_outcome_shape(envelope)
    assert envelope["outcome_status"] == "rejected"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    rejection = cast(dict[str, object], result["rejection"])
    event_types = set(cast(list[str], audit["event_types"]))

    assert result["action_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_step_up_proof"
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert REQUIRED_STEP_UP_BLOCKED_AUDIT_EVENTS.issubset(event_types)
    assert "action_execution_requested" not in event_types


def test_blocked_action_outcome_keeps_operator_fields_and_trace_audit_linkage() -> None:
    draft = execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_record = cast(dict[str, object], awaiting["state_record"])
    blocked = attempt_income_tax_action_request(
        confirmation_record=awaiting_record,
        action_type="submission_execute",
        risk_class="low",
    )
    envelope = build_income_tax_action_final_outcome_envelope(blocked)

    _assert_final_outcome_shape(envelope)
    assert envelope["outcome_status"] == "rejected"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    policy = cast(dict[str, object], result["policy_decision"])
    rejection = cast(dict[str, object], result["rejection"])

    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert result["action_status"] == "rejected"
    assert policy["reason_code"] == "confirmation_required"
    assert rejection["reason_code"] == "confirmation_required"
    assert rejection["required_controls"] == ["confirmation"]
    assert rejection["next_allowed_actions"] == ["confirm", "reject", "revise_input"]
    assert cast(int, audit["event_count"]) > 0
    assert "policy_decision" in cast(list[str], audit["event_types"])


def test_repeated_identical_final_outcome_is_byte_equivalent() -> None:
    first = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    second = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_drift_guard_fails_when_required_audit_event_missing() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    audit = cast(dict[str, object], envelope["audit"])
    drifted_event_types = [
        event_type
        for event_type in cast(list[str], audit["event_types"])
        if event_type != "plan_validated"
    ]

    with pytest.raises(AssertionError):
        assert REQUIRED_SUCCESS_AUDIT_EVENTS.issubset(set(drifted_event_types))


def test_drift_guard_fails_when_trace_field_missing() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    drifted = deepcopy(envelope)
    trace = cast(dict[str, object], drifted["trace"])
    trace.pop("trace_id")

    with pytest.raises(AssertionError):
        _assert_final_outcome_shape(drifted)


def test_audit_event_linkage_matches_final_trace_correlation() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    trace = cast(dict[str, object], envelope["trace"])
    correlation_id = cast(str, trace["correlation_id"])
    trace_id = cast(str, trace["trace_id"])
    events = get_income_tax_audit_events_for_correlation(correlation_id)

    assert events
    assert all(event["correlation_id"] == correlation_id for event in events)
    assert all(event["trace_id"] == trace_id for event in events)


def _assert_final_outcome_shape(envelope: dict[str, object]) -> None:
    assert set(envelope) == {"outcome_status", "message", "trace", "audit", "result"}
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    assert set(trace) == {"trace_id", "correlation_id", "lineage_refs"}
    assert set(audit) == {"event_count", "event_ids", "event_types", "latest_event_id_by_type"}
    assert isinstance(trace["correlation_id"], str)
    assert isinstance(trace["trace_id"], str)
