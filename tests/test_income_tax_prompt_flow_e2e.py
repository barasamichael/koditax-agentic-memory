"""Verify deterministic end-to-end income-tax prompt flow for supported lanes."""

from __future__ import annotations

from typing import cast

import pytest

from shared.determinism.input_hash import canonical_json_dumps
import tests.income_tax_prompt_flow_support as prompt_flow_support
from tests.income_tax_prompt_flow_support import IncomeTaxPromptFlowError
from tests.income_tax_prompt_flow_support import SUPPORTED_PROMPT_BINDINGS
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow
from tests.income_tax_prompt_flow_support import attempt_income_tax_action_request
from tests.income_tax_prompt_flow_support import bind_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import get_income_tax_step_up_test_proof_code
from tests.income_tax_prompt_flow_support import prepare_income_tax_confirmation_review
from tests.income_tax_prompt_flow_support import verify_income_tax_action_step_up_proof
from tests.income_tax_prompt_flow_support import resolve_income_tax_confirmation_decision
from tests.income_tax_prompt_flow_support import evaluate_income_tax_action_request_policy
from tests.income_tax_prompt_flow_support import execute_income_tax_prompt_flow_final_outcome
from tests.income_tax_prompt_flow_support import authorize_income_tax_action_with_step_up_proof


@pytest.mark.parametrize(
    ("prompt_text", "expected_lane_id", "expected_historical_version_id"),
    [
        (
            prompt_text,
            binding.supported_lane_id,
            binding.historical_version_id,
        )
        for prompt_text, binding in SUPPORTED_PROMPT_BINDINGS.items()
    ],
)
def test_supported_income_tax_prompts_complete_end_to_end_deterministically(
    prompt_text: str,
    expected_lane_id: str,
    expected_historical_version_id: str,
) -> None:
    prompt_result = execute_income_tax_prompt_flow(prompt_text)

    assert prompt_result["status"] == "draft_ready"
    assert prompt_result["message"] == (
        "Draft income-tax outcome is ready for review. Confirm to continue, "
        "reject to stop, or revise input."
    )
    draft_context = prompt_result["draft_context"]
    review_summary = prompt_result["review_summary"]
    artifacts = prompt_result["artifacts"]
    lineage = prompt_result["lineage"]

    assert isinstance(draft_context, dict)
    assert isinstance(review_summary, dict)
    assert isinstance(artifacts, dict)
    assert isinstance(lineage, dict)
    assert isinstance(prompt_result["correlation_id"], str)
    assert isinstance(prompt_result["trace_id"], str)
    trace_context = cast(dict[str, object], prompt_result["trace_context"])
    assert trace_context["correlation_id"] == prompt_result["correlation_id"]
    assert trace_context["trace_id"] == prompt_result["trace_id"]

    assert draft_context["tax_type"] == "income_tax"
    assert draft_context["supported_lane_id"] == expected_lane_id
    assert draft_context["historical_version_id"] == expected_historical_version_id
    assert draft_context["tax_year"] in {2021, 2023}

    assert review_summary["chargeable_income_kes"]
    assert review_summary["gross_tax_kes"]
    assert review_summary["total_reliefs_kes"]
    assert review_summary["net_income_tax_due_kes"]
    assert review_summary["refund_due_kes"]

    assert artifacts["form_artifact_id"]
    assert artifacts["form_version_id"]
    assert artifacts["report_id"]
    assert artifacts["report_version_id"]
    assert artifacts["submission_preview_payload_id"]
    assert artifacts["submission_preview_payload_version"]

    assert prompt_result["next_allowed_actions"] == ["confirm", "reject", "revise_input"]

    assert lineage["computation_id"]
    assert lineage["input_hash"]
    assert lineage["rule_version"]
    assert lineage["finalized_audit_event_id"]
    assert lineage["form_audit_evidence_id"]
    assert lineage["report_audit_evidence_id"]
    assert lineage["payload_audit_evidence_id"]


def test_unsupported_income_tax_prompt_scope_fails_explicitly() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow("Compute VAT filing output for Q3 and submit to regulator.")

    assert error_info.value.reason == "unsupported_prompt_scope"
    details = error_info.value.details()
    assert details["error_code"] == "unsupported_prompt_scope"
    assert details["message"] == (
        "Prompt scope is not supported by governed income-tax pilot capability."
    )
    assert details["reason"] == "unsupported_domain"
    assert isinstance(details["correlation_id"], str)
    assert isinstance(details["trace_id"], str)
    assert details["rejected_context"] == {
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
        "tax_domain": "vat",
        "prompt_class": "income_tax_prompt_flow",
    }


def test_unallowlisted_tenant_prompt_scope_fails_explicitly() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(
            prompt_text,
            tenant_id="pilot_tenant_unknown",
        )

    assert error_info.value.reason == "pilot_tenant_not_allowed"
    details = error_info.value.details()
    rejected_context = cast(dict[str, object], details["rejected_context"])
    assert details["error_code"] == "pilot_tenant_not_allowed"
    assert details["reason"] == "tenant_not_allowlisted"
    assert rejected_context["tenant_id"] == "pilot_tenant_unknown"


def test_malformed_prompt_input_fails_explicitly() -> None:
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow("   ")

    assert error_info.value.reason == "invalid_prompt_input"
    details = error_info.value.details()
    assert details["error_code"] == "invalid_prompt_input"
    assert details["reason"] == "empty_prompt_text"
    assert isinstance(details["correlation_id"], str)
    assert isinstance(details["trace_id"], str)


def test_prompt_flow_is_deterministic_for_identical_prompt_inputs() -> None:
    prompt_text = (
        "Compute income tax for resident employment plus qualifying interest "
        "lane in tax year 2023 under KIT-VER-20230701-A."
    )

    first = execute_income_tax_prompt_flow(prompt_text)
    second = execute_income_tax_prompt_flow(prompt_text)

    assert canonical_json_dumps(second) == canonical_json_dumps(first)


def test_prompt_flow_final_outcome_boundary_returns_canonical_envelope() -> None:
    envelope = execute_income_tax_prompt_flow_final_outcome(
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )

    assert set(envelope) == {"outcome_status", "message", "trace", "audit", "result"}
    assert envelope["outcome_status"] == "success"
    trace = cast(dict[str, object], envelope["trace"])
    audit = cast(dict[str, object], envelope["audit"])
    assert isinstance(trace["trace_id"], str)
    assert isinstance(trace["correlation_id"], str)
    lineage_refs = cast(dict[str, object], trace["lineage_refs"])
    document_evidence_refs = cast(dict[str, object], lineage_refs["document_evidence_refs"])
    assert isinstance(document_evidence_refs["document_id"], str)
    assert isinstance(document_evidence_refs["representation_id"], str)
    assert isinstance(document_evidence_refs["projection_ref_id"], str)
    assert document_evidence_refs["conflict_report_ref_id"] is None
    assert document_evidence_refs["conflict_policy_decision_ref_id"] is None
    assert cast(int, audit["event_count"]) > 0


def test_unsupported_evidence_mapping_scope_is_blocked_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unsupported_projection(**kwargs: object) -> dict[str, object]:
        document_id_str = str(kwargs["document_id"])
        representation_id = str(kwargs["representation_id"])
        return {
            "projection_version": "1.0.0",
            "document_id": document_id_str,
            "representation_id": representation_id,
            "supported_lane_id": "resident_employment_income_2023_07_01",
            "historical_version_id": "KIT-VER-20230701-A",
            "tax_year": 2023,
            "mapped_evidence_fields": {
                "taxpayer_pin": "P051234567Z",
                "resident_status_assertion": "resident",
                "document_tax_year": 2023,
                "rental_income": 50000.0,
            },
            "unresolved_fields": [],
            "mapping_warnings": [],
            "traceability": {
                "trace_id": "a" * 64,
                "correlation_id": "b" * 64,
                "source_field_refs": [],
            },
        }

    monkeypatch.setattr(
        prompt_flow_support,
        "_build_canonical_evidence_projection",
        _unsupported_projection,
    )
    with pytest.raises(IncomeTaxPromptFlowError) as error_info:
        execute_income_tax_prompt_flow(
            "Compute income tax for resident employment lane in tax year 2023 "
            "under KIT-VER-20230701-A."
        )

    details = error_info.value.details()
    assert error_info.value.reason == "unsupported_evidence_mapping_scope"
    assert details["error_code"] == "unsupported_evidence_mapping_scope"
    assert details["message"] == (
        "Evidence mapping scope is not supported by governed income-tax pilot capability."
    )
    assert details["reason"] == "unsupported_field_mapping_scope"
    rejected_context = cast(dict[str, object], details["rejected_context"])
    assert rejected_context["lane_id"] == "resident_employment_income_2023_07_01"
    assert rejected_context["historical_version_id"] == "KIT-VER-20230701-A"
    assert rejected_context["tax_year"] == 2023
    assert rejected_context["field_path"] == "mapped_evidence_fields.rental_income"


def test_prompt_flow_boundary_drift_detection_fails_on_expected_mismatch() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    result = execute_income_tax_prompt_flow(prompt_text)
    drifted_expected = dict(result)
    drifted_expected["supported_lane_id"] = "resident_employment_income_2099_01_01"

    with pytest.raises(AssertionError):
        assert canonical_json_dumps(result) == canonical_json_dumps(drifted_expected)


def test_prompt_flow_confirmation_state_branches_to_confirmed_and_rejected() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)

    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    rejected = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="reject",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])
    rejected_state_record = cast(dict[str, object], rejected["state_record"])

    assert awaiting_state_record["current_state"] == "awaiting_confirmation"
    assert confirmed_state_record["current_state"] == "confirmed"
    assert rejected_state_record["current_state"] == "rejected"


def test_prompt_flow_action_policy_gate_reflects_block_allow_and_step_up() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    blocked = evaluate_income_tax_action_request_policy(
        confirmation_record=awaiting_state_record,
        action_type="submission_execute",
        risk_class="low",
    )
    allowed = evaluate_income_tax_action_request_policy(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="low",
    )
    step_up = evaluate_income_tax_action_request_policy(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )

    assert blocked["policy_decision"] == "blocked"
    assert blocked["step_up_decision"] == "not_required"
    assert blocked["reason_code"] == "confirmation_required"
    assert allowed["policy_decision"] == "allowed"
    assert allowed["step_up_decision"] == "not_required"
    assert allowed["reason_code"] == "policy_allow"
    assert step_up["policy_decision"] == "step_up_required"
    assert step_up["step_up_decision"] == "required"
    assert step_up["reason_code"] == "step_up_auth_required"


def test_prompt_flow_action_attempt_returns_rejection_or_step_up_challenge() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    unconfirmed_rejection = attempt_income_tax_action_request(
        confirmation_record=awaiting_state_record,
        action_type="submission_execute",
        risk_class="low",
    )
    high_risk_step_up = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )

    unconfirmed_payload = cast(dict[str, object], unconfirmed_rejection["rejection"])
    high_risk_challenge = cast(dict[str, object], high_risk_step_up["step_up_challenge"])

    assert unconfirmed_rejection["action_status"] == "rejected"
    assert unconfirmed_rejection["execution_status"] == "not_executed"
    assert unconfirmed_payload["error_code"] == "action_rejected_unconfirmed"
    assert unconfirmed_payload["required_controls"] == ["confirmation"]
    assert unconfirmed_payload["next_allowed_actions"] == ["confirm", "reject", "revise_input"]

    assert high_risk_step_up["action_status"] == "step_up_challenge_issued"
    assert high_risk_step_up["execution_status"] == "not_executed"
    assert high_risk_step_up["rejection"] is None
    assert high_risk_challenge["challenge_status"] == "issued"
    assert high_risk_challenge["reason_code"] == "step_up_challenge_issued"


def test_prompt_flow_allowed_submission_action_routes_through_adapter_contract() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    allowed_submission = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="low",
    )
    adapter_response = cast(dict[str, object], allowed_submission["adapter_response"])
    execution_envelope = cast(dict[str, object], allowed_submission["execution_envelope"])
    mapped_result = cast(dict[str, object], allowed_submission["mapped_result"])
    trace = cast(dict[str, object], execution_envelope["trace"])

    assert allowed_submission["action_status"] == "allowed"
    assert allowed_submission["execution_status"] == "not_executed"
    assert allowed_submission["rejection"] is None
    assert execution_envelope["execution_status"] == "resolved"
    assert execution_envelope["idempotency_key"]
    assert execution_envelope["request_fingerprint"]
    assert adapter_response["adapter_status"] == "mock_pending"
    assert adapter_response["action_result_code"] == "submission_action_mock_pending"
    assert mapped_result["action_status"] == "pending"
    assert mapped_result["reason_code"] == "submission_action_mock_pending"
    assert mapped_result["trace_id"] == trace["trace_id"]


def test_prompt_flow_submission_adapter_envelope_replays_identical_response() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])
    first = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="low",
    )
    second = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="low",
    )

    assert canonical_json_dumps(second["execution_envelope"]) == canonical_json_dumps(
        first["execution_envelope"]
    )


def test_prompt_flow_step_up_binding_authorizes_only_with_valid_bound_proof() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    high_risk_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], high_risk_attempt["step_up_challenge"])
    challenge_record = cast(dict[str, object], challenge["challenge_record"])
    verification = verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code=get_income_tax_step_up_test_proof_code(),
        verified_at="2026-03-20T00:02:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=high_risk_attempt,
        verification_result=verification,
    )
    authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object], binding["proof_binding"]),
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    adapter_response = cast(dict[str, object], authorization["adapter_response"])
    mapped_result = cast(dict[str, object], authorization["mapped_result"])
    execution_envelope = cast(dict[str, object], authorization["execution_envelope"])
    trace = cast(dict[str, object], execution_envelope["trace"])
    proof_binding = cast(dict[str, object], authorization["proof_binding"])
    binding_context = cast(dict[str, object], proof_binding["context"])
    assert authorization["action_status"] == "authorized"
    assert authorization["execution_status"] == "not_executed"
    assert adapter_response["adapter_status"] == "mock_pending"
    assert adapter_response["action_result_code"] == "submission_action_mock_pending"
    assert mapped_result["action_status"] == "pending"
    assert mapped_result["trace_id"] == trace["trace_id"]
    assert isinstance(proof_binding["challenge_id"], str)
    assert isinstance(proof_binding["issued_at"], str)
    assert isinstance(proof_binding["expires_at"], str)
    assert binding_context["action_type"] == "submission_execute"
    assert binding_context["tenant_id"] == "pilot_tenant_alpha"
    assert isinstance(binding_context["principal_user_id"], str)
    assert isinstance(binding_context["action_reference_id"], str)
    assert binding_context["step_up_purpose"] == "high_risk_action_step_up"


def test_prompt_flow_step_up_proof_context_mismatch_is_rejected_deterministically() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    high_risk_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], high_risk_attempt["step_up_challenge"])
    challenge_record = cast(dict[str, object], challenge["challenge_record"])
    verification = verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code=get_income_tax_step_up_test_proof_code(),
        verified_at="2026-03-20T00:02:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=high_risk_attempt,
        verification_result=verification,
    )
    tampered_binding = dict(cast(dict[str, object], binding["proof_binding"]))
    tampered_context = dict(cast(dict[str, object], tampered_binding["context"]))
    tampered_context["tenant_id"] = "pilot_tenant_tampered"
    tampered_binding["context"] = tampered_context

    authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=tampered_binding,
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    repeated_authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=tampered_binding,
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    rejection = cast(dict[str, object], authorization["rejection"])
    repeated_rejection = cast(dict[str, object], repeated_authorization["rejection"])

    assert authorization["action_status"] == "rejected"
    assert repeated_authorization["action_status"] == "rejected"
    assert rejection["error_code"] == "action_rejected_step_up_proof"
    assert rejection["reason_code"] == "step_up_proof_context_mismatch"
    assert repeated_rejection["error_code"] == "action_rejected_step_up_proof"
    assert repeated_rejection["reason_code"] == "step_up_proof_context_mismatch"
    assert set(rejection) == set(repeated_rejection)


def test_prompt_flow_step_up_proof_replay_is_rejected_after_consumption() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])

    high_risk_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )
    challenge = cast(dict[str, object], high_risk_attempt["step_up_challenge"])
    challenge_record = cast(dict[str, object], challenge["challenge_record"])
    verification = verify_income_tax_action_step_up_proof(
        challenge_record=challenge_record,
        proof_code=get_income_tax_step_up_test_proof_code(),
        verified_at="2026-03-20T00:02:00+03:00",
    )
    binding = bind_income_tax_action_step_up_proof(
        action_attempt=high_risk_attempt,
        verification_result=verification,
    )
    first_authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object], binding["proof_binding"]),
        authorized_at="2026-03-20T00:03:00+03:00",
    )
    replay_authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object], first_authorization["proof_binding"]),
        authorized_at="2026-03-20T00:03:30+03:00",
    )
    repeated_replay_authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object], first_authorization["proof_binding"]),
        authorized_at="2026-03-20T00:03:30+03:00",
    )
    replay_rejection = cast(dict[str, object], replay_authorization["rejection"])
    repeated_replay_rejection = cast(dict[str, object], repeated_replay_authorization["rejection"])

    assert first_authorization["action_status"] == "authorized"
    assert replay_authorization["action_status"] == "rejected"
    assert repeated_replay_authorization["action_status"] == "rejected"
    assert replay_rejection["error_code"] == "action_rejected_step_up_proof"
    assert replay_rejection["reason_code"] == "step_up_proof_already_consumed"
    assert repeated_replay_rejection["error_code"] == "action_rejected_step_up_proof"
    assert repeated_replay_rejection["reason_code"] == "step_up_proof_already_consumed"
    assert set(replay_rejection) == set(repeated_replay_rejection)


def test_prompt_flow_no_2fa_required_read_only_path_is_usable() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    call_count = 0

    def _adapter() -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"status": "review_rendered"}

    action = attempt_income_tax_action_request(
        confirmation_record=awaiting_state_record,
        action_type="read_only_review",
        risk_class="low",
        execution_adapter=_adapter,
    )
    policy = cast(dict[str, object], action["policy_decision"])
    assert policy["step_up_decision"] == "not_required"
    assert policy["required_controls"] == []
    assert action["action_status"] == "allowed"
    assert action["execution_status"] == "executed"
    assert call_count == 1


def test_prompt_flow_failed_or_expired_2fa_paths_return_canonical_block_and_no_execution() -> None:
    prompt_text = (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    )
    draft = execute_income_tax_prompt_flow(prompt_text)
    awaiting = prepare_income_tax_confirmation_review(draft)
    awaiting_state_record = cast(dict[str, object], awaiting["state_record"])
    confirmed = resolve_income_tax_confirmation_decision(
        confirmation_record=awaiting_state_record,
        decision="confirm",
    )
    confirmed_state_record = cast(dict[str, object], confirmed["state_record"])
    call_count = 0

    def _adapter() -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"status": "executed"}

    failed_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )
    failed_challenge = cast(dict[str, object], failed_attempt["step_up_challenge"])
    failed_verification = verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], failed_challenge["challenge_record"]),
        proof_code="000000",
        verified_at="2026-03-20T00:02:00+03:00",
    )
    failed_binding = bind_income_tax_action_step_up_proof(
        action_attempt=failed_attempt,
        verification_result=failed_verification,
    )
    failed_authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object] | None, failed_binding["proof_binding"]),
        authorized_at="2026-03-20T00:03:00+03:00",
        execution_adapter=_adapter,
    )

    expired_attempt = attempt_income_tax_action_request(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
    )
    expired_challenge = cast(dict[str, object], expired_attempt["step_up_challenge"])
    expired_verification = verify_income_tax_action_step_up_proof(
        challenge_record=cast(dict[str, object], expired_challenge["challenge_record"]),
        proof_code=get_income_tax_step_up_test_proof_code(),
        verified_at="2026-03-20T00:02:00+03:00",
    )
    expired_binding = bind_income_tax_action_step_up_proof(
        action_attempt=expired_attempt,
        verification_result=expired_verification,
    )
    expired_authorization = authorize_income_tax_action_with_step_up_proof(
        confirmation_record=confirmed_state_record,
        action_type="submission_execute",
        risk_class="high",
        proof_binding=cast(dict[str, object], expired_binding["proof_binding"]),
        authorized_at="2026-03-20T00:06:30+03:00",
        execution_adapter=_adapter,
    )

    failed_rejection = cast(dict[str, object], failed_authorization["rejection"])
    expired_rejection = cast(dict[str, object], expired_authorization["rejection"])

    assert failed_authorization["action_status"] == "rejected"
    assert failed_rejection["error_code"] == "action_rejected_step_up_proof"
    assert failed_rejection["message"] == (
        "Action request failed deterministic step-up proof authorization checks."
    )
    assert failed_rejection["reason_code"] == "step_up_proof_missing"
    assert failed_rejection["required_controls"] == ["step_up_auth"]

    assert expired_authorization["action_status"] == "rejected"
    assert expired_rejection["error_code"] == "action_rejected_step_up_proof"
    assert expired_rejection["message"] == (
        "Action request failed deterministic step-up proof authorization checks."
    )
    assert expired_rejection["reason_code"] == "step_up_proof_expired"
    assert expired_rejection["required_controls"] == ["step_up_auth"]
    assert call_count == 0
