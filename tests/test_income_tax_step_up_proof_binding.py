"""Verify deterministic binding and consumption of step-up proof for action authorization."""

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
AUTHORIZED_AT = "2026-03-20T00:03:00+03:00"
EXPIRED_AUTHORIZED_AT = "2026-03-20T00:06:30+03:00"


def _confirmed_record() -> dict[str, object]:
    draft = prompt_flow_support.execute_income_tax_prompt_flow(SUPPORTED_PROMPT)
    awaiting = prompt_flow_support.prepare_income_tax_confirmation_review(draft)
    awaiting_record = cast(Mapping[str, object], awaiting["state_record"])
    confirmed = prompt_flow_support.resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_record,
        decision="confirm",
    )
    return cast(dict[str, object], confirmed["state_record"])


def _verified_high_risk_flow() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    confirmed_record = _confirmed_record()
    action_attempt = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    challenge_record = cast(Mapping[str, object], challenge["challenge_record"])
    verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code=prompt_flow_support.get_income_tax_step_up_test_proof_code(),
        verified_at=VERIFIED_AT,
    )
    return confirmed_record, action_attempt, verification


def _failed_verification_high_risk_flow() -> (
    tuple[dict[str, object], dict[str, object], dict[str, object]]
):
    confirmed_record = _confirmed_record()
    action_attempt = prompt_flow_support.attempt_income_tax_action_request(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], action_attempt["step_up_challenge"])
    challenge_record = cast(Mapping[str, object], challenge["challenge_record"])
    verification = prompt_flow_support.verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code="000000",
        verified_at=VERIFIED_AT,
    )
    return confirmed_record, action_attempt, verification


def test_valid_verified_proof_binds_and_authorizes_matching_context() -> None:
    confirmed_record, action_attempt, verification = _verified_high_risk_flow()
    binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    authorization = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(Mapping[str, object], binding["proof_binding"]),
        authorized_at=AUTHORIZED_AT,
    )

    bound_record = cast(dict[str, object], binding["proof_binding"])
    consumed_record = cast(dict[str, object], authorization["proof_binding"])
    assert binding["binding_status"] == "bound"
    assert bound_record["proof_status"] == "bound"
    assert authorization["action_status"] == "authorized"
    assert authorization["execution_status"] == "not_executed"
    assert consumed_record["proof_status"] == "consumed"
    assert consumed_record["consumed_at"] == AUTHORIZED_AT


def test_mismatched_context_rejects_deterministically() -> None:
    confirmed_record, action_attempt, verification = _verified_high_risk_flow()
    binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    mismatched_binding = deepcopy(cast(dict[str, object], binding["proof_binding"]))
    context = cast(dict[str, object], mismatched_binding["context"])
    context["tax_year"] = 2021
    authorization = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=mismatched_binding,
        authorized_at=AUTHORIZED_AT,
    )

    rejection = cast(dict[str, object], authorization["rejection"])
    assert authorization["action_status"] == "rejected"
    assert rejection["reason_code"] == "step_up_proof_context_mismatch"


def test_replayed_consumed_proof_rejects_second_use() -> None:
    confirmed_record, action_attempt, verification = _verified_high_risk_flow()
    binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    first = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(Mapping[str, object], binding["proof_binding"]),
        authorized_at=AUTHORIZED_AT,
    )
    second = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(Mapping[str, object], first["proof_binding"]),
        authorized_at=AUTHORIZED_AT,
    )

    rejection = cast(dict[str, object], second["rejection"])
    assert first["action_status"] == "authorized"
    assert second["action_status"] == "rejected"
    assert rejection["reason_code"] == "step_up_proof_already_consumed"


def test_expired_bound_proof_rejects_deterministically() -> None:
    confirmed_record, action_attempt, verification = _verified_high_risk_flow()
    binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )
    authorization = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(Mapping[str, object], binding["proof_binding"]),
        authorized_at=EXPIRED_AUTHORIZED_AT,
    )

    rejection = cast(dict[str, object], authorization["rejection"])
    assert authorization["action_status"] == "rejected"
    assert rejection["reason_code"] == "step_up_proof_expired"


def test_missing_bound_proof_when_required_rejects() -> None:
    confirmed_record = _confirmed_record()
    authorization = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=None,
        authorized_at=AUTHORIZED_AT,
    )

    rejection = cast(dict[str, object], authorization["rejection"])
    assert authorization["action_status"] == "rejected"
    assert rejection["reason_code"] == "step_up_proof_missing"


def test_same_invalid_input_yields_identical_rejection_payload() -> None:
    confirmed_record = _confirmed_record()
    first = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=deepcopy(confirmed_record),
        action_type="submission_execute",
        risk_class="high",
        proof_binding=None,
        authorized_at=AUTHORIZED_AT,
    )
    second = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=deepcopy(confirmed_record),
        action_type="submission_execute",
        risk_class="high",
        proof_binding=None,
        authorized_at=AUTHORIZED_AT,
    )
    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_failed_verification_binding_returns_canonical_rejection_envelope() -> None:
    _, action_attempt, verification = _failed_verification_high_risk_flow()
    binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
    )

    error = cast(dict[str, object], binding["error"])
    assert verification["verification_status"] == "failed"
    assert binding["binding_status"] == "rejected"
    assert error["error_code"] == "action_rejected_step_up_proof"
    assert error["message"] == (
        "Action request failed deterministic step-up proof authorization checks."
    )
    assert error["reason_code"] == "step_up_verification_not_verified"
    assert error["reason"] == "Step-up proof cannot be bound because verification did not succeed."
    assert error["required_controls"] == ["step_up_auth"]


def test_expired_verified_proof_binding_returns_canonical_rejection_envelope() -> None:
    _, action_attempt, verification = _verified_high_risk_flow()
    binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=action_attempt,
        verification_result=verification,
        bound_at=EXPIRED_AUTHORIZED_AT,
    )

    error = cast(dict[str, object], binding["error"])
    proof_binding = cast(dict[str, object], binding["proof_binding"])
    assert binding["binding_status"] == "rejected"
    assert proof_binding["proof_status"] == "expired"
    assert error["error_code"] == "action_rejected_step_up_proof"
    assert error["message"] == (
        "Action request failed deterministic step-up proof authorization checks."
    )
    assert error["reason_code"] == "step_up_proof_expired"
    assert error["reason"] == "Verified step-up proof is already expired at binding time."
    assert error["required_controls"] == ["step_up_auth"]


def test_failed_and_expired_step_up_paths_never_execute_adapter() -> None:
    call_count = 0

    def _adapter() -> Mapping[str, object]:
        nonlocal call_count
        call_count += 1
        return {"status": "executed"}

    confirmed_record, failed_attempt, failed_verification = _failed_verification_high_risk_flow()
    failed_binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=failed_attempt,
        verification_result=failed_verification,
    )
    failed_authorization = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(Mapping[str, object] | None, failed_binding["proof_binding"]),
        authorized_at=AUTHORIZED_AT,
        execution_adapter=_adapter,
    )

    _, expired_attempt, expired_verification = _verified_high_risk_flow()
    expired_binding = prompt_flow_support.bind_income_tax_action_step_up_proof(
        action_attempt=expired_attempt,
        verification_result=expired_verification,
    )
    expired_authorization = prompt_flow_support.authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(Mapping[str, object], expired_binding["proof_binding"]),
        authorized_at=EXPIRED_AUTHORIZED_AT,
        execution_adapter=_adapter,
    )

    failed_rejection = cast(dict[str, object], failed_authorization["rejection"])
    expired_rejection = cast(dict[str, object], expired_authorization["rejection"])
    assert failed_authorization["action_status"] == "rejected"
    assert failed_rejection["reason_code"] == "step_up_proof_missing"
    assert failed_rejection["required_controls"] == ["step_up_auth"]
    assert expired_authorization["action_status"] == "rejected"
    assert expired_rejection["reason_code"] == "step_up_proof_expired"
    assert expired_rejection["required_controls"] == ["step_up_auth"]
    assert call_count == 0
