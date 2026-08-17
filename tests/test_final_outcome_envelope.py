"""Verify canonical deterministic final outcome envelope behavior."""

from __future__ import annotations

from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import bind_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import verify_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome
from tests.income_tax_prompt_flow_support import authorize_income_tax_action_with_step_up_proof
from tests.income_tax_prompt_flow_support import build_income_tax_action_final_outcome_envelope

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
UNSUPPORTED_PROMPT = "Compute VAT filing output for Q3 and submit to regulator."


def test_supported_prompt_returns_canonical_final_success_envelope() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)

    assert envelope["outcome_status"] == "success"
    assert envelope["message"] == "Income-tax prompt flow completed successfully."
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    lineage_refs = cast(dict[str, object], trace["lineage_refs"])

    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert lineage_refs["prompt_id"] == result["prompt_id"]
    assert lineage_refs["computation_id"]
    assert lineage_refs["finalized_audit_event_id"]
    assert cast(int, audit["event_count"]) > 0
    assert "intent_parsed" in cast(list[str], audit["event_types"])
    assert "plan_generated" in cast(list[str], audit["event_types"])
    assert "plan_validated" in cast(list[str], audit["event_types"])
    assert cast(dict[str, str], audit["latest_event_id_by_type"])["intent_parsed"]


def test_unsupported_prompt_returns_canonical_final_rejection_envelope() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(UNSUPPORTED_PROMPT)

    assert envelope["outcome_status"] == "rejected"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])

    assert result["error_code"] == "unsupported_prompt_scope"
    assert result["reason"] == "unsupported_domain"
    assert result["rejected_context"] == {
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
        "tax_domain": "vat",
        "prompt_class": "income_tax_prompt_flow",
    }
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert cast(int, audit["event_count"]) >= 2
    assert "intent_parsed" in cast(list[str], audit["event_types"])
    assert "plan_generated" in cast(list[str], audit["event_types"])


def test_step_up_rejection_path_maps_to_canonical_final_rejection_envelope() -> None:
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
    assert envelope["outcome_status"] == "rejected"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    result = cast(dict[str, object], envelope["result"])
    rejection = cast(dict[str, object], result["rejection"])

    assert result["action_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_step_up_proof"
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    assert "policy_decision" in cast(list[str], audit["event_types"])
    assert "step_up_challenge_issued" in cast(list[str], audit["event_types"])
    assert "step_up_verification_result" in cast(list[str], audit["event_types"])


def test_final_envelope_is_deterministic_for_identical_input() -> None:
    first = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    second = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_final_envelope_contract_drift_detection_fails() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(SUPPORTED_PROMPT)
    drifted = dict(envelope)
    drifted.pop("audit")

    with pytest.raises(AssertionError):
        assert set(drifted) == {"outcome_status", "message", "trace", "audit", "result"}
