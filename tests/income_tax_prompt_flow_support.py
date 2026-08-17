"""Shared deterministic support for income-tax prompt-flow tests."""

from __future__ import annotations

import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
import hashlib
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Callable

from shared.determinism.input_hash import canonical_json_dumps
from services.tax_core.app.engine.executor import execute_computation
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.audit_events import clear_income_tax_audit_events
from services.orchestration.app.trace_context import build_trace_context
from services.orchestration.app.intent_to_plan import IntentToPlanError
from services.orchestration.app.intent_to_plan import translate_income_tax_intent_to_plan
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.document_ai.app.evidence_conflicts import detect_evidence_input_conflicts
from services.orchestration.app.action_policy_gate import evaluate_income_tax_action_policy
from services.orchestration.app.confirmation_state import IncomeTaxConfirmationStateError
from services.orchestration.app.confirmation_state import initialize_income_tax_confirmation_state
from services.orchestration.app.confirmation_state import transition_income_tax_confirmation_state
from services.forms.app.income_tax.report_generation import generate_income_tax_report
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.orchestration.app.intent_plan_validator import (
    validate_income_tax_intent_plan_for_dispatch,
)
from services.orchestration.app.step_up_auth_workflow import TEST_STEP_UP_PROOF_CODE
from services.orchestration.app.step_up_auth_workflow import issue_income_tax_step_up_challenge
from services.orchestration.app.step_up_auth_workflow import verify_income_tax_step_up_challenge
from services.orchestration.app.step_up_proof_binding import bind_income_tax_verified_step_up_proof
from services.orchestration.app.step_up_proof_binding import (
    authorize_income_tax_action_with_bound_step_up_proof,
)
from services.document_ai.app.evidence_conflict_policy import evaluate_evidence_conflict_policy
from services.orchestration.app.action_rejection_paths import build_income_tax_action_rejection
from services.orchestration.app.final_outcome_envelope import OutcomeStatus
from services.orchestration.app.final_outcome_envelope import FinalOutcomeEnvelopeError
from services.orchestration.app.final_outcome_envelope import (
    build_income_tax_final_outcome_envelope,
)
from services.orchestration.app.prompt_intent_envelope import PromptIntentEnvelopeError
from services.orchestration.app.prompt_intent_envelope import (
    parse_income_tax_prompt_intent_envelope,
)
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.orchestration.app.action_adapter_registry import (
    dispatch_submission_action_request_with_envelope,
)
from services.forms.app.income_tax.report_version_binding import bind_income_tax_report_version
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import (
    reset_default_action_execution_idempotency_store,
)
from services.orchestration.app.income_tax_capability_gate import IncomeTaxCapabilityGateError
from services.orchestration.app.income_tax_capability_gate import (
    enforce_income_tax_runtime_capability_gate,
)
from services.forms.app.income_tax.form_artifact_generation import generate_income_tax_form_artifact
from services.orchestration.app.evidence_mapping_guardrails import EvidenceMappingGuardrailError
from services.orchestration.app.evidence_mapping_guardrails import (
    enforce_income_tax_evidence_mapping_scope,
)
from services.orchestration.app.income_tax_draft_outcome_contract import (
    IncomeTaxDraftOutcomeContractError,
)
from services.orchestration.app.income_tax_draft_outcome_contract import (
    build_income_tax_draft_outcome_response,
)
from services.forms.app.income_tax.submission_payload_construction import (
    construct_income_tax_submission_payload,
)

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-19T23:30:00+03:00"
STEP_UP_CHALLENGE_ISSUED_AT = "2026-03-20T00:00:00+03:00"
STEP_UP_PROOF_BOUND_AT = "2026-03-20T00:02:30+03:00"
STEP_UP_AUTHORIZED_AT = "2026-03-20T00:03:00+03:00"
DEFAULT_PILOT_TENANT_ID = "pilot_tenant_alpha"


@dataclass(frozen=True)
class PromptFixtureBinding:
    """Represent one supported prompt mapped to one governed fixture lane."""

    fixture_name: str
    supported_lane_id: str
    historical_version_id: str
    tax_year: int


SUPPORTED_PROMPT_BINDINGS: dict[str, PromptFixtureBinding] = {
    (
        "Compute income tax for resident employment lane in tax year 2021 under KIT-VER-20210101-A."
    ): PromptFixtureBinding(
        fixture_name="income_tax_resident_employment_2021_01_01_case_001.json",
        supported_lane_id="resident_employment_income_2021_01_01",
        historical_version_id="KIT-VER-20210101-A",
        tax_year=2021,
    ),
    (
        "Compute income tax for non-resident employment lane in tax year 2021 "
        "under KIT-VER-20210101-A."
    ): PromptFixtureBinding(
        fixture_name="income_tax_non_resident_employment_2021_01_01_case_001.json",
        supported_lane_id="non_resident_employment_income_2021_01_01",
        historical_version_id="KIT-VER-20210101-A",
        tax_year=2021,
    ),
    (
        "Compute income tax for resident employment lane in tax year 2023 under KIT-VER-20230701-A."
    ): PromptFixtureBinding(
        fixture_name="income_tax_resident_employment_2023_07_01_case_001.json",
        supported_lane_id="resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
    ),
    (
        "Compute income tax for non-resident employment lane in tax year 2023 "
        "under KIT-VER-20230701-A."
    ): PromptFixtureBinding(
        fixture_name="income_tax_non_resident_employment_2023_07_01_case_001.json",
        supported_lane_id="non_resident_employment_income_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
    ),
    (
        "Compute income tax for resident employment plus qualifying interest "
        "lane in tax year 2023 under KIT-VER-20230701-A."
    ): PromptFixtureBinding(
        fixture_name=(
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
        ),
        supported_lane_id="resident_employment_plus_qualifying_interest_2023_07_01",
        historical_version_id="KIT-VER-20230701-A",
        tax_year=2023,
    ),
}
SUPPORTED_PROMPT_BINDINGS_BY_CONTEXT: dict[tuple[str, str, int], PromptFixtureBinding] = {
    (
        binding.supported_lane_id,
        binding.historical_version_id,
        binding.tax_year,
    ): binding
    for binding in SUPPORTED_PROMPT_BINDINGS.values()
}


class IncomeTaxPromptFlowError(RuntimeError):
    """Represent deterministic prompt-flow failures."""

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self._details = details or {}

    def details(self) -> dict[str, object]:
        """Return stable structured details for deterministic failures."""

        return {"reason": self.reason, **self._details}


def execute_income_tax_prompt_flow(
    prompt_text: str,
    *,
    tenant_id: str | None = DEFAULT_PILOT_TENANT_ID,
) -> dict[str, object]:
    """Execute deterministic supported income-tax prompt flow end-to-end."""

    clear_income_tax_audit_events()
    reset_default_action_execution_idempotency_store()
    try:
        intent_envelope = parse_income_tax_prompt_intent_envelope(prompt_text)
    except PromptIntentEnvelopeError as error:
        raise IncomeTaxPromptFlowError(
            reason=error.error_code,
            message=error.message,
            details=error.payload(),
        ) from error

    try:
        intent_plan = translate_income_tax_intent_to_plan(intent_envelope)
    except IntentToPlanError as error:
        payload = error.payload()
        reason = payload.get("reason")
        rejected_context_raw = payload.get("rejected_context")
        if isinstance(reason, str) and isinstance(rejected_context_raw, dict):
            rejected_context = cast(dict[str, object], rejected_context_raw)
            canonical_reason = (
                "unsupported_domain"
                if reason == "unsupported_domain"
                else "unsupported_lane_context"
            )
            canonical_error: dict[str, object] = {
                "error_code": "unsupported_prompt_scope",
                "message": "Prompt scope is not supported by governed income-tax pilot capability.",
                "reason": canonical_reason,
                "rejected_context": {
                    "supported_lane_id": rejected_context.get("requested_lane_hint"),
                    "historical_version_id": rejected_context.get("historical_version_hint"),
                    "tax_year": rejected_context.get("tax_year_hint"),
                    "tax_domain": rejected_context.get("tax_domain_hint"),
                    "prompt_class": rejected_context.get("prompt_class"),
                },
            }
            correlation_id = payload.get("correlation_id")
            if isinstance(correlation_id, str):
                canonical_error["correlation_id"] = correlation_id
            trace_id = payload.get("trace_id")
            if isinstance(trace_id, str):
                canonical_error["trace_id"] = trace_id
            raise IncomeTaxPromptFlowError(
                reason="unsupported_prompt_scope",
                message="Prompt scope is not supported by governed income-tax pilot capability.",
                details=canonical_error,
            ) from error
        raise IncomeTaxPromptFlowError(
            reason=error.error_code,
            message=error.message,
            details=payload,
        ) from error

    validation = validate_income_tax_intent_plan_for_dispatch(intent_plan)
    if validation["validation_status"] != "accepted":
        error = validation["error"]
        if error is None:
            raise IncomeTaxPromptFlowError(
                reason="intent_plan_validation_failed",
                message="Plan validation rejected dispatch without explicit error payload.",
            )
        error_payload = dict(cast(dict[str, object], error))
        error_payload["correlation_id"] = intent_envelope["correlation_id"]
        error_payload["trace_id"] = intent_envelope["trace_id"]
        raise IncomeTaxPromptFlowError(
            reason=error["error_code"],
            message=error["message"],
            details=error_payload,
        )

    binding = _binding_from_plan(intent_plan)
    try:
        enforce_income_tax_runtime_capability_gate(
            prompt_text=intent_envelope["normalized_prompt_text"],
            supported_lane_id=intent_plan["supported_lane_id"],
            historical_version_id=intent_plan["historical_version_id"],
            tax_year=intent_plan["tax_year"],
            correlation_id=intent_envelope["correlation_id"],
            tenant_id=tenant_id,
        )
    except IncomeTaxCapabilityGateError as error:
        error_payload = error.payload()
        error_payload["trace_id"] = intent_envelope["trace_id"]
        raise IncomeTaxPromptFlowError(
            reason=error.error_code,
            message=error.message,
            details=error_payload,
        ) from error

    if binding is None:
        raise IncomeTaxPromptFlowError(
            reason="intent_binding_unavailable",
            message="Intent envelope did not resolve a supported fixture binding after gate pass.",
            details={"intent_envelope": intent_envelope},
        )

    fixture = _load_fixture(binding.fixture_name)
    request = ComputationExecutionRequest.model_validate(fixture["request"])
    computation_output = execute_computation(request).model_dump(mode="json")

    if computation_output.get("status") != "ok":
        raise IncomeTaxPromptFlowError(
            reason="computation_failed",
            message="Supported prompt flow did not produce an ok computation result.",
            details={"status": computation_output.get("status")},
        )

    result_payload = _require_object(computation_output, "result_payload")
    version_identity = _require_object(result_payload, "version_identity")
    if _require_string(version_identity, "historical_version_id") != binding.historical_version_id:
        raise IncomeTaxPromptFlowError(
            reason="lineage_mismatch",
            message="Computed historical version did not match prompt binding context.",
        )

    prompt_id = intent_envelope["correlation_id"]
    finalized_output = _build_finalized_output(
        prompt_id=prompt_id,
        computation_output=computation_output,
    )

    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_version_binding = bind_income_tax_form_version(form_ready_output)
    form_artifact = generate_income_tax_form_artifact(
        finalized_output=finalized_output,
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )
    report_output = generate_income_tax_report(form_artifact_output=form_artifact)
    report_binding = bind_income_tax_report_version(report_output)
    submission_payload = construct_income_tax_submission_payload(
        report_output=report_output,
        report_version_binding=report_binding,
    )
    try:
        draft_response = build_income_tax_draft_outcome_response(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            supported_lane_id=binding.supported_lane_id,
            historical_version_id=binding.historical_version_id,
            tax_year=binding.tax_year,
            computation_output=computation_output,
            finalized_output=finalized_output,
            form_artifact_output=form_artifact,
            report_output=report_output,
            report_version_binding=report_binding,
            submission_payload_output=submission_payload,
        )
        response_with_trace = dict(draft_response)
        response_with_trace["correlation_id"] = intent_envelope["correlation_id"]
        response_with_trace["trace_id"] = intent_envelope["trace_id"]
        response_with_trace["trace_context"] = build_trace_context(
            intent_envelope["correlation_id"]
        )
        document_evidence_refs = _build_document_evidence_refs_for_prompt_flow(
            prompt_flow_payload=response_with_trace
        )
        response_with_trace["document_evidence_refs"] = document_evidence_refs
        draft_context = _require_object(response_with_trace, "draft_context")
        emit_income_tax_audit_event(
            event_type="evidence_lineage_linked",
            status="recorded",
            correlation_id=_require_string(response_with_trace, "correlation_id"),
            supported_lane_id=_require_string(draft_context, "supported_lane_id"),
            historical_version_id=_require_string(draft_context, "historical_version_id"),
            tax_year=_require_int(draft_context, "tax_year"),
            context={"document_evidence_refs": document_evidence_refs},
        )
        return response_with_trace
    except IncomeTaxDraftOutcomeContractError as error:
        raise IncomeTaxPromptFlowError(
            reason=error.reason,
            message=error.message,
            details=error.payload(),
        ) from error
    except EvidenceMappingGuardrailError as error:
        error_payload = error.payload()
        error_payload["correlation_id"] = intent_envelope["correlation_id"]
        error_payload["trace_id"] = intent_envelope["trace_id"]
        raise IncomeTaxPromptFlowError(
            reason=error.error_code,
            message=error.message,
            details=error_payload,
        ) from error


def execute_income_tax_prompt_flow_final_outcome(
    prompt_text: str,
    *,
    tenant_id: str | None = DEFAULT_PILOT_TENANT_ID,
    document_evidence_refs_override: Mapping[str, object] | None = None,
    require_document_evidence_refs: bool = True,
) -> dict[str, object]:
    """Execute prompt flow and return canonical final outcome envelope for client consumption."""

    try:
        success_payload = execute_income_tax_prompt_flow(prompt_text, tenant_id=tenant_id)
    except IncomeTaxPromptFlowError as error:
        details = error.details()
        correlation_id = _optional_str(details.get("correlation_id"))
        trace_id = _optional_str(details.get("trace_id"))
        audit_events = (
            get_income_tax_audit_events_for_correlation(correlation_id)
            if correlation_id is not None
            else []
        )
        return cast(
            dict[str, object],
            build_income_tax_final_outcome_envelope(
                outcome_status=_map_error_reason_to_final_status(error.reason),
                message=error.message,
                result=details,
                correlation_id=correlation_id,
                trace_id=trace_id,
                lineage_refs={},
                audit_events=_as_audit_events(audit_events),
            ),
        )

    correlation_id = _require_string(success_payload, "correlation_id")
    trace_id = _require_string(success_payload, "trace_id")
    lineage = _require_object(success_payload, "lineage")
    document_evidence_refs = (
        dict(document_evidence_refs_override)
        if document_evidence_refs_override is not None
        else _optional_object(success_payload.get("document_evidence_refs"))
    )
    audit_events = get_income_tax_audit_events_for_correlation(correlation_id)
    try:
        return cast(
            dict[str, object],
            build_income_tax_final_outcome_envelope(
                outcome_status="success",
                message="Income-tax prompt flow completed successfully.",
                result=success_payload,
                correlation_id=correlation_id,
                trace_id=trace_id,
                lineage_refs={
                    "prompt_id": _require_string(success_payload, "prompt_id"),
                    "computation_id": _require_string(lineage, "computation_id"),
                    "finalized_audit_event_id": _require_string(
                        lineage, "finalized_audit_event_id"
                    ),
                },
                audit_events=_as_audit_events(audit_events),
                document_evidence_refs=document_evidence_refs,
                require_document_evidence_refs=require_document_evidence_refs,
            ),
        )
    except FinalOutcomeEnvelopeError as error:
        return cast(
            dict[str, object],
            build_income_tax_final_outcome_envelope(
                outcome_status="error",
                message=error.message,
                result=error.payload(),
                correlation_id=correlation_id,
                trace_id=trace_id,
                lineage_refs={},
                audit_events=_as_audit_events(audit_events),
            ),
        )


def build_income_tax_action_final_outcome_envelope(
    action_outcome: Mapping[str, object],
) -> dict[str, object]:
    """Map deterministic action outcome payload to canonical final outcome envelope."""

    policy_decision = _require_object(cast(dict[str, object], action_outcome), "policy_decision")
    correlation_id = _optional_str(policy_decision.get("correlation_id"))
    trace_id = _optional_str(policy_decision.get("trace_id"))
    audit_events = (
        get_income_tax_audit_events_for_correlation(correlation_id) if correlation_id else []
    )
    action_status = _require_string(cast(dict[str, object], action_outcome), "action_status")
    outcome_status = _map_action_status_to_final_status(action_status)
    return cast(
        dict[str, object],
        build_income_tax_final_outcome_envelope(
            outcome_status=outcome_status,
            message=_build_action_final_message(outcome_status, action_status),
            result=cast(dict[str, object], action_outcome),
            correlation_id=correlation_id,
            trace_id=trace_id,
            lineage_refs={},
            audit_events=_as_audit_events(audit_events),
        ),
    )


def prepare_income_tax_confirmation_review(
    draft_outcome: Mapping[str, object],
) -> dict[str, object]:
    """Enter awaiting_confirmation from one deterministic draft-ready response."""

    try:
        confirmation_record = initialize_income_tax_confirmation_state(draft_outcome=draft_outcome)
        transition_result = transition_income_tax_confirmation_state(
            confirmation_record=confirmation_record,
            target_state="awaiting_confirmation",
        )
        return cast(dict[str, object], transition_result)
    except IncomeTaxConfirmationStateError as error:
        raise IncomeTaxPromptFlowError(
            reason=error.reason,
            message=error.message,
            details=error.payload(),
        ) from error


def resolve_income_tax_confirmation_decision(
    *,
    confirmation_record: Mapping[str, object],
    decision: str,
) -> dict[str, object]:
    """Resolve awaiting_confirmation into confirmed/rejected deterministically."""

    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"confirm", "reject"}:
        raise IncomeTaxPromptFlowError(
            reason="invalid_confirmation_decision",
            message="Confirmation decision is not supported.",
            details={
                "error_code": "invalid_confirmation_decision",
                "reason": "invalid_confirmation_decision",
                "decision": decision,
                "allowed_decisions": ["confirm", "reject"],
            },
        )
    target_state = "confirmed" if normalized_decision == "confirm" else "rejected"
    try:
        transition_result = transition_income_tax_confirmation_state(
            confirmation_record=confirmation_record,
            target_state=target_state,
        )
        return cast(dict[str, object], transition_result)
    except IncomeTaxConfirmationStateError as error:
        raise IncomeTaxPromptFlowError(
            reason=error.reason,
            message=error.message,
            details=error.payload(),
        ) from error


def evaluate_income_tax_action_request_policy(
    *,
    confirmation_record: Mapping[str, object],
    action_type: str,
    risk_class: str,
    tenant_id: str | None = DEFAULT_PILOT_TENANT_ID,
) -> dict[str, object]:
    """Evaluate deterministic action policy decision before any side-effect-capable action path."""

    current_state = _require_string(cast(dict[str, object], confirmation_record), "current_state")
    draft_context = _require_object(cast(dict[str, object], confirmation_record), "draft_context")
    lineage = _require_object(cast(dict[str, object], confirmation_record), "lineage")
    decision = evaluate_income_tax_action_policy(
        current_state=current_state,
        action_type=action_type,
        risk_class=risk_class,
        supported_lane_id=_require_string(draft_context, "supported_lane_id"),
        historical_version_id=_require_string(draft_context, "historical_version_id"),
        tax_year=_require_int(draft_context, "tax_year"),
        correlation_id=_require_string(lineage, "prompt_id"),
        tenant_id=tenant_id,
    )
    return cast(dict[str, object], decision)


def attempt_income_tax_action_request(
    *,
    confirmation_record: Mapping[str, object],
    action_type: str,
    risk_class: str,
    tenant_id: str | None = DEFAULT_PILOT_TENANT_ID,
    execution_adapter: Callable[[], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Apply policy and return explicit deterministic rejection for blocked execution."""

    policy_decision = evaluate_income_tax_action_request_policy(
        confirmation_record=confirmation_record,
        action_type=action_type,
        risk_class=risk_class,
        tenant_id=tenant_id,
    )
    if policy_decision.get("policy_decision") == "step_up_required":
        step_up_challenge = issue_income_tax_step_up_challenge(
            policy_decision=cast(Mapping[str, object], policy_decision),
            issued_at=STEP_UP_CHALLENGE_ISSUED_AT,
        )
        return {
            "action_status": "step_up_challenge_issued",
            "policy_decision": policy_decision,
            "step_up_challenge": step_up_challenge,
            "rejection": None,
            "execution_status": "not_executed",
        }

    rejection = build_income_tax_action_rejection(policy_decision=policy_decision)
    if rejection is not None:
        return {
            "action_status": "rejected",
            "policy_decision": policy_decision,
            "rejection": rejection,
            "execution_status": "not_executed",
        }

    if action_type == "submission_execute":
        execution_envelope = _dispatch_submission_action_adapter(
            confirmation_record=confirmation_record,
            action_type=action_type,
            policy_decision=policy_decision,
        )
        mapped_result = execution_envelope.get("mapped_result")
        adapter_response = execution_envelope.get("adapter_response")
        if execution_envelope.get("execution_status") == "rejected":
            return {
                "action_status": "rejected",
                "policy_decision": policy_decision,
                "rejection": execution_envelope.get("error"),
                "mapped_result": mapped_result,
                "execution_envelope": execution_envelope,
                "execution_status": "not_executed",
            }
        if not isinstance(adapter_response, dict):
            raise IncomeTaxPromptFlowError(
                reason="adapter_execution_envelope_invalid",
                message="Submission action execution envelope is missing adapter response.",
            )
        if adapter_response["adapter_status"] == "unsupported":
            return {
                "action_status": "rejected",
                "policy_decision": policy_decision,
                "rejection": adapter_response["error"],
                "mapped_result": mapped_result,
                "adapter_response": adapter_response,
                "execution_envelope": execution_envelope,
                "execution_status": "not_executed",
            }
        return {
            "action_status": "allowed",
            "policy_decision": policy_decision,
            "rejection": None,
            "mapped_result": mapped_result,
            "adapter_response": adapter_response,
            "execution_envelope": execution_envelope,
            "execution_status": "not_executed",
        }

    if execution_adapter is None:
        return {
            "action_status": "allowed",
            "policy_decision": policy_decision,
            "rejection": None,
            "execution_status": "not_executed",
        }

    executed = execution_adapter()
    return {
        "action_status": "allowed",
        "policy_decision": policy_decision,
        "rejection": None,
        "execution_status": "executed",
        "execution_result": cast(dict[str, object], executed),
    }


def verify_income_tax_action_step_up_proof(
    *,
    challenge_record: Mapping[str, object] | None,
    proof_code: str,
    verified_at: str,
) -> dict[str, object]:
    """Verify deterministic step-up proof for one issued challenge record."""

    verification = verify_income_tax_step_up_challenge(
        challenge_record=challenge_record,
        proof_code=proof_code,
        verified_at=verified_at,
    )
    return cast(dict[str, object], verification)


def bind_income_tax_action_step_up_proof(
    *,
    action_attempt: Mapping[str, object],
    verification_result: Mapping[str, object],
    bound_at: str = STEP_UP_PROOF_BOUND_AT,
) -> dict[str, object]:
    """Bind deterministic verified step-up proof to one exact action execution context."""

    policy_decision = action_attempt.get("policy_decision")
    if not isinstance(policy_decision, Mapping):
        raise IncomeTaxPromptFlowError(
            reason="step_up_binding_missing_policy_context",
            message="Action attempt is missing policy decision context for step-up proof binding.",
        )
    binding = bind_income_tax_verified_step_up_proof(
        policy_decision=cast(Mapping[str, object], policy_decision),
        verification_result=verification_result,
        bound_at=bound_at,
    )
    return cast(dict[str, object], binding)


def authorize_income_tax_action_with_step_up_proof(
    *,
    confirmation_record: Mapping[str, object],
    action_type: str,
    risk_class: str,
    proof_binding: Mapping[str, object] | None,
    tenant_id: str | None = DEFAULT_PILOT_TENANT_ID,
    authorized_at: str = STEP_UP_AUTHORIZED_AT,
    execution_adapter: Callable[[], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Authorize progression with one bound step-up proof; no external execution is performed."""

    policy_decision = evaluate_income_tax_action_request_policy(
        confirmation_record=confirmation_record,
        action_type=action_type,
        risk_class=risk_class,
        tenant_id=tenant_id,
    )
    authorization = authorize_income_tax_action_with_bound_step_up_proof(
        policy_decision=cast(Mapping[str, object], policy_decision),
        proof_binding=proof_binding,
        authorized_at=authorized_at,
    )
    if authorization["authorization_status"] != "authorized":
        return {
            "action_status": "rejected",
            "policy_decision": policy_decision,
            "rejection": authorization["error"],
            "proof_binding": authorization["proof_binding"],
            "execution_status": "not_executed",
        }

    if action_type == "submission_execute":
        execution_envelope = _dispatch_submission_action_adapter(
            confirmation_record=confirmation_record,
            action_type=action_type,
            policy_decision=policy_decision,
        )
        mapped_result = execution_envelope.get("mapped_result")
        adapter_response = execution_envelope.get("adapter_response")
        if execution_envelope.get("execution_status") == "rejected":
            return {
                "action_status": "rejected",
                "policy_decision": policy_decision,
                "rejection": execution_envelope.get("error"),
                "proof_binding": authorization["proof_binding"],
                "mapped_result": mapped_result,
                "execution_envelope": execution_envelope,
                "execution_status": "not_executed",
            }
        if not isinstance(adapter_response, dict):
            raise IncomeTaxPromptFlowError(
                reason="adapter_execution_envelope_invalid",
                message="Submission action execution envelope is missing adapter response.",
            )
        if adapter_response["adapter_status"] == "unsupported":
            return {
                "action_status": "rejected",
                "policy_decision": policy_decision,
                "rejection": adapter_response["error"],
                "proof_binding": authorization["proof_binding"],
                "mapped_result": mapped_result,
                "adapter_response": adapter_response,
                "execution_envelope": execution_envelope,
                "execution_status": "not_executed",
            }
        return {
            "action_status": "authorized",
            "policy_decision": policy_decision,
            "rejection": None,
            "proof_binding": authorization["proof_binding"],
            "mapped_result": mapped_result,
            "adapter_response": adapter_response,
            "execution_envelope": execution_envelope,
            "execution_status": "not_executed",
        }

    if execution_adapter is None:
        return {
            "action_status": "authorized",
            "policy_decision": policy_decision,
            "rejection": None,
            "proof_binding": authorization["proof_binding"],
            "execution_status": "not_executed",
        }

    executed = execution_adapter()
    return {
        "action_status": "authorized",
        "policy_decision": policy_decision,
        "rejection": None,
        "proof_binding": authorization["proof_binding"],
        "execution_status": "executed",
        "execution_result": cast(dict[str, object], executed),
    }


def get_income_tax_step_up_test_proof_code() -> str:
    """Return deterministic local test proof used by step-up workflow tests."""

    return TEST_STEP_UP_PROOF_CODE


def get_income_tax_audit_events_for_correlation(
    correlation_id: str,
) -> list[dict[str, object]]:
    """Return deterministic orchestration audit events for one correlation identifier."""

    return [dict(event) for event in list_income_tax_audit_events(correlation_id=correlation_id)]


def _as_audit_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [dict(event) for event in events]


def _map_error_reason_to_final_status(reason: str) -> OutcomeStatus:
    if reason in {
        "unsupported_prompt_scope",
        "pilot_tenant_not_allowed",
        "invalid_prompt_input",
        "invalid_confirmation_decision",
        "unsupported_evidence_mapping_scope",
    }:
        return "rejected"
    return "error"


def _map_action_status_to_final_status(action_status: str) -> OutcomeStatus:
    if action_status in {"allowed", "authorized"}:
        return "success"
    if action_status == "rejected":
        return "rejected"
    if action_status == "step_up_challenge_issued":
        return "pending"
    return "error"


def _build_action_final_message(outcome_status: OutcomeStatus, action_status: str) -> str:
    if outcome_status == "success":
        return "Income-tax action outcome is available."
    if outcome_status == "rejected":
        return "Income-tax action request was rejected by deterministic controls."
    if outcome_status == "pending":
        return "Income-tax action request is pending required controls."
    return f"Income-tax action outcome could not be classified from status '{action_status}'."


def _dispatch_submission_action_adapter(
    *,
    confirmation_record: Mapping[str, object],
    action_type: str,
    policy_decision: Mapping[str, object],
) -> dict[str, object]:
    execution_request = _build_submission_action_execution_request(
        confirmation_record=confirmation_record,
        action_type=action_type,
        policy_decision=policy_decision,
    )
    return cast(
        dict[str, object],
        dispatch_submission_action_request_with_envelope(execution_request),
    )


def _build_submission_action_execution_request(
    *,
    confirmation_record: Mapping[str, object],
    action_type: str,
    policy_decision: Mapping[str, object],
) -> ActionExecutionRequest:
    draft_context = _require_object(cast(dict[str, object], confirmation_record), "draft_context")
    lineage = _require_object(cast(dict[str, object], confirmation_record), "lineage")
    action_context = _require_object(cast(dict[str, object], policy_decision), "decision_context")
    idempotency_payload = {
        "correlation_id": _require_string(lineage, "prompt_id"),
        "action_type": action_type,
        "submission_payload_ref": _require_string(lineage, "computation_id"),
        "risk_class": _require_string(action_context, "risk_class"),
        "supported_lane_id": _require_string(draft_context, "supported_lane_id"),
        "historical_version_id": _require_string(draft_context, "historical_version_id"),
        "tax_year": _require_int(draft_context, "tax_year"),
    }
    return {
        "idempotency_key": _sha256_hex(canonical_json_dumps(idempotency_payload)),
        "action_type": action_type,
        "correlation_id": _require_string(lineage, "prompt_id"),
        "submission_payload_ref": _require_string(lineage, "computation_id"),
        "capability_context": {
            "supported_lane_id": _require_string(draft_context, "supported_lane_id"),
            "historical_version_id": _require_string(draft_context, "historical_version_id"),
            "tax_year": _require_int(draft_context, "tax_year"),
        },
    }


def _binding_from_plan(
    plan: Mapping[str, object],
) -> PromptFixtureBinding | None:
    supported_lane_id = plan.get("supported_lane_id")
    historical_version_id = plan.get("historical_version_id")
    tax_year_hint = plan.get("tax_year")
    if (
        not isinstance(supported_lane_id, str)
        or not isinstance(historical_version_id, str)
        or not isinstance(tax_year_hint, int)
    ):
        return None
    return SUPPORTED_PROMPT_BINDINGS_BY_CONTEXT.get(
        (
            supported_lane_id,
            historical_version_id,
            tax_year_hint,
        )
    )


def _build_finalized_output(
    *,
    prompt_id: str,
    computation_output: dict[str, object],
) -> dict[str, object]:
    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{prompt_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": FINALIZED_AT,
        "finalized_audit_event_id": str(uuid5(NAMESPACE_URL, f"{prompt_id}:finalized-audit")),
        "tax_type": _require_string(computation_output, "tax_type"),
        "regime_type": _require_string(computation_output, "regime_type"),
        "tax_year": _require_int(computation_output, "tax_year"),
        "rule_version": _require_string(computation_output, "rule_version"),
        "input_hash": _require_string(computation_output, "input_hash"),
        "result_payload": _require_object(computation_output, "result_payload"),
    }


def _load_fixture(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    return cast(dict[str, object], json.loads(fixture_path.read_text(encoding="utf-8")))


def _require_object(source: dict[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if not isinstance(value, dict):
        raise IncomeTaxPromptFlowError(
            reason="missing_required_field",
            message=f"Required object field '{field_name}' is missing in prompt flow.",
            details={"field_name": field_name},
        )
    return cast(dict[str, object], value)


def _require_string(source: dict[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IncomeTaxPromptFlowError(
            reason="missing_required_field",
            message=f"Required string field '{field_name}' is missing in prompt flow.",
            details={"field_name": field_name},
        )
    return value


def _require_int(source: dict[str, object], field_name: str) -> int:
    value = source.get(field_name)
    if not isinstance(value, int):
        raise IncomeTaxPromptFlowError(
            reason="missing_required_field",
            message=f"Required integer field '{field_name}' is missing in prompt flow.",
            details={"field_name": field_name},
        )
    return value


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_document_evidence_refs_for_prompt_flow(
    *,
    prompt_flow_payload: Mapping[str, object],
) -> dict[str, object]:
    prompt_id = _require_string(cast(dict[str, object], prompt_flow_payload), "prompt_id")
    correlation_id = _require_string(cast(dict[str, object], prompt_flow_payload), "correlation_id")
    trace_id = _require_string(cast(dict[str, object], prompt_flow_payload), "trace_id")
    draft_context = _require_object(cast(dict[str, object], prompt_flow_payload), "draft_context")

    document_id = str(uuid5(NAMESPACE_URL, f"{prompt_id}:document_evidence"))
    representation_id = str(uuid5(NAMESPACE_URL, f"{prompt_id}:canonical_document_evidence"))
    resident_status = (
        "non_resident"
        if "non_resident" in _require_string(draft_context, "supported_lane_id")
        else "resident"
    )
    projection = _build_canonical_evidence_projection(
        document_id=document_id,
        representation_id=representation_id,
        prompt_id=prompt_id,
        resident_status=resident_status,
        supported_lane_id=_require_string(draft_context, "supported_lane_id"),
        historical_version_id=_require_string(draft_context, "historical_version_id"),
        tax_year=_require_int(draft_context, "tax_year"),
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    enforce_income_tax_evidence_mapping_scope(
        projection=projection,
        lane_id=_require_string(draft_context, "supported_lane_id"),
        historical_version_id=_require_string(draft_context, "historical_version_id"),
        tax_year=_require_int(draft_context, "tax_year"),
        tax_domain="income_tax",
    )
    user_execution_request = _build_user_execution_request_for_conflict_detection(
        projection=projection,
        historical_version_id=_require_string(draft_context, "historical_version_id"),
        tax_year=_require_int(draft_context, "tax_year"),
    )
    conflict_report = detect_evidence_input_conflicts(
        evidence_projection=projection,
        user_execution_request=user_execution_request,
    )
    conflict_policy = evaluate_evidence_conflict_policy(conflict_report=conflict_report)

    conflict_detected = cast(bool, conflict_report["conflict_detected"])
    projection_ref_id = _sha256_hex(canonical_json_dumps(projection))
    conflict_report_ref_id = (
        _sha256_hex(canonical_json_dumps(conflict_report)) if conflict_detected else None
    )
    conflict_policy_ref_id = (
        _sha256_hex(canonical_json_dumps(conflict_policy)) if conflict_detected else None
    )
    return {
        "document_id": document_id,
        "representation_id": representation_id,
        "projection_ref_id": projection_ref_id,
        "conflict_report_ref_id": conflict_report_ref_id,
        "conflict_policy_decision_ref_id": conflict_policy_ref_id,
    }


def _build_canonical_evidence_projection(
    *,
    document_id: str,
    representation_id: str,
    prompt_id: str,
    resident_status: str,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
    trace_id: str,
    correlation_id: str,
) -> dict[str, object]:
    """Build target-style semantic evidence for deterministic workflow fixtures.

    This test helper models canonical evidence already resolved from an active
    representation; it deliberately does not emulate a document extractor.
    """

    return {
        "projection_version": "2.0.0",
        "document_id": document_id,
        "representation_id": representation_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "mapped_evidence_fields": {
            "taxpayer_pin": f"P{_sha256_hex(prompt_id)[:9].upper()}",
            "resident_status_assertion": resident_status,
            "document_tax_year": tax_year,
            "employment": {
                "gross_employment_income_kes": 1200000.0,
                "paye_withheld_kes": 120000.0,
                "employer_tax_pin": f"P{_sha256_hex(prompt_id + ':employer')[:9].upper()}",
            },
        },
        "unresolved_fields": [],
        "mapping_warnings": [],
        "traceability": {
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "canonical_representation_id": representation_id,
        },
    }


def _build_user_execution_request_for_conflict_detection(
    *,
    projection: Mapping[str, object],
    historical_version_id: str,
    tax_year: int,
) -> dict[str, object]:
    mapped_evidence_fields = _require_object(
        cast(dict[str, object], projection), "mapped_evidence_fields"
    )
    taxpayer_context = {
        "taxpayer_kind": "individual",
        "resident_status_assertion": mapped_evidence_fields.get("resident_status_assertion")
        or "undetermined",
        "taxpayer_reference_id": mapped_evidence_fields.get("taxpayer_pin"),
    }
    income_sections: dict[str, object] = {}
    employment = mapped_evidence_fields.get("employment")
    if isinstance(employment, dict):
        employment_map = cast(dict[str, object], employment)
        employment_item = {
            "income_subtype": "cash_emolument",
            "amount_kes": _to_money_string(employment_map.get("gross_employment_income_kes")),
            "event_date": f"{tax_year}-12-31",
            "employer_reference_id": employment_map.get("employer_tax_pin"),
            "paye_withheld_kes": _to_money_string(employment_map.get("paye_withheld_kes")),
        }
        income_sections["employment"] = {"employment_items": [employment_item]}
    investment = mapped_evidence_fields.get("qualifying_interest")
    if isinstance(investment, dict):
        investment_map = cast(dict[str, object], investment)
        investment_item = {
            "income_subtype": "interest",
            "gross_amount_kes": _to_money_string(investment_map.get("gross_interest_income_kes")),
            "event_date": f"{tax_year}-12-31",
            "withholding_applied_kes": _to_money_string(
                investment_map.get("withholding_applied_kes")
            ),
        }
        income_sections["investment"] = {"investment_items": [investment_item]}

    return {
        "tax_type": "income_tax",
        "regime_type": "income_tax",
        "regime_identifier": _require_string(
            cast(dict[str, object], projection), "supported_lane_id"
        ),
        "tax_year": tax_year,
        "rule_version": historical_version_id,
        "input_payload": {
            "taxpayer_context": taxpayer_context,
            "income_sections": income_sections,
        },
    }


def build_user_execution_request_for_conflict_detection(
    *,
    projection: Mapping[str, object],
    historical_version_id: str,
    tax_year: int,
) -> dict[str, object]:
    """Public wrapper for deterministic conflict-detection request construction."""

    return _build_user_execution_request_for_conflict_detection(
        projection=projection,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )


def _to_money_string(value: object) -> str:
    if isinstance(value, bool):
        return "0.00"
    if isinstance(value, int | float):
        return f"{float(value):.2f}"
    if isinstance(value, str):
        return value
    return "0.00"


def _optional_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
