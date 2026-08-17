"""Verify deterministic step-up auth challenge and verification workflow behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import cast
from collections.abc import Mapping

from tests import income_tax_prompt_flow_support as prompt_flow_support
from shared.determinism.input_hash import canonical_json_dumps

SUPPORTED_PROMPT = (
    "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
)
VERIFIED_AT = "2026-03-20T00:02:00+03:00"
EXPIRED_AT = "2026-03-20T00:06:00+03:00"


def _confirmed_record() -> dict[str, object]:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    awaiting_record = cast(Mapping[str, object], awaiting["state_record"])
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_record,
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def _issue_high_risk_challenge() -> dict[str, object]:
    action_attempt = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="high",
    )
    return action_attempt


def test_high_risk_action_issues_challenge_and_valid_proof_verifies() -> None:
    action_attempt = _issue_high_risk_challenge()
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    challenge_record = cast(Mapping[str, object], challenge["challenge_record"])
    verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code=prompt_flow_support.get_income_tax_step_up_test_proof_code(),
        verified_at=VERIFIED_AT,
    )

    assert action_attempt["action_status"] == "step_up_challenge_issued"
    assert action_attempt["execution_status"] == "not_executed"
    assert challenge["challenge_status"] == "issued"
    assert challenge["allowed_attempts"] == 3
    assert verification["verification_status"] == "verified"
    assert verification["reason_code"] == "proof_verified"


def test_wrong_proof_fails_deterministically() -> None:
    action_attempt = _issue_high_risk_challenge()
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    challenge_record = cast(Mapping[str, object], challenge["challenge_record"])
    verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code="000000",
        verified_at=VERIFIED_AT,
    )

    assert verification["verification_status"] == "failed"
    assert verification["reason_code"] == "proof_invalid"


def test_expired_challenge_is_rejected() -> None:
    action_attempt = _issue_high_risk_challenge()
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    challenge_record = cast(Mapping[str, object], challenge["challenge_record"])
    verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code=prompt_flow_support.get_income_tax_step_up_test_proof_code(),
        verified_at=EXPIRED_AT,
    )

    assert verification["verification_status"] == "expired"
    assert verification["reason_code"] == "challenge_expired"


def test_exceeded_attempts_fails_deterministically() -> None:
    action_attempt = _issue_high_risk_challenge()
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    current_record = cast(dict[str, object], challenge["challenge_record"])
    for _ in range(2):
        outcome = prompt_flow_support.verify_income_tax_action_step_up_proof(
            challenge_record=current_record,
            proof_code="111111",
            verified_at=VERIFIED_AT,
        )
        current_record = cast(dict[str, object], outcome["challenge_record"])

    final_outcome = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=current_record,
        proof_code="111111",
        verified_at=VERIFIED_AT,
    )
    assert final_outcome["verification_status"] == "failed"
    assert final_outcome["reason_code"] == "attempts_exceeded"


def test_same_context_yields_deterministic_challenge_and_verification_output() -> None:
    first_attempt = _issue_high_risk_challenge()
    second_attempt = _issue_high_risk_challenge()
    first_challenge = cast(dict[str, object], first_attempt["step_up_challenge"])
    second_challenge = cast(dict[str, object], second_attempt["step_up_challenge"])

    assert canonical_json_dumps(second_challenge) == canonical_json_dumps(first_challenge)

    first_verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=deepcopy(cast(dict[str, object], first_challenge["challenge_record"])),
        proof_code=prompt_flow_support.get_income_tax_step_up_test_proof_code(),
        verified_at=VERIFIED_AT,
    )
    second_verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=deepcopy(cast(dict[str, object], second_challenge["challenge_record"])),
        proof_code=prompt_flow_support.get_income_tax_step_up_test_proof_code(),
        verified_at=VERIFIED_AT,
    )
    assert canonical_json_dumps(second_verification) == canonical_json_dumps(first_verification)


def test_no_challenge_cannot_verify_success() -> None:
    verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=None,
        proof_code=prompt_flow_support.get_income_tax_step_up_test_proof_code(),
        verified_at=VERIFIED_AT,
    )
    assert verification["verification_status"] == "invalid"
    assert verification["reason_code"] == "challenge_missing"


def test_2fa_required_pending_blocks_execution_until_verification() -> None:
    call_count = 0

    def _adapter() -> Mapping[str, object]:
        nonlocal call_count
        call_count += 1
        return {"status": "executed"}

    action_attempt = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="high",
        execution_adapter=_adapter,
    )

    policy_decision = cast(dict[str, object], action_attempt["policy_decision"])
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    challenge_record = cast(dict[str, object], challenge["challenge_record"])
    assert action_attempt["action_status"] == "step_up_challenge_issued"
    assert action_attempt["execution_status"] == "not_executed"
    assert action_attempt["rejection"] is None
    assert policy_decision["policy_decision"] == "step_up_required"
    assert policy_decision["required_controls"] == ["step_up_auth"]
    assert challenge["challenge_status"] == "issued"
    assert challenge_record["challenge_status"] == "issued"
    assert call_count == 0


def test_unverified_high_risk_action_remains_blocked() -> None:
    call_count = 0

    def _adapter() -> Mapping[str, object]:
        nonlocal call_count
        call_count += 1
        return {"status": "executed"}

    action_attempt = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=_confirmed_record(),
        action_type="submission_execute",
        risk_class="high",
        execution_adapter=_adapter,
    )
    assert action_attempt["action_status"] == "step_up_challenge_issued"
    assert action_attempt["execution_status"] == "not_executed"
    assert call_count == 0
