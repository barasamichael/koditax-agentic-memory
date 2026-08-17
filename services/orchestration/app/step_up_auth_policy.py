"""Evaluate deterministic step-up auth policy for income-tax pilot action requests."""

from __future__ import annotations

from typing import Literal
from typing import TypedDict

from services.orchestration.app.trace_context import build_optional_trace_id

StepUpDecision = Literal["required", "not_required", "unsupported_context"]

KNOWN_ACTION_TYPES = {"read_only_review", "submission_execute"}
KNOWN_RISK_CLASSES = {"low", "high"}
KNOWN_CONFIRMATION_STATES = {"draft_ready", "awaiting_confirmation", "confirmed", "rejected"}
SIDE_EFFECT_ACTION_TYPES = {"submission_execute"}


class StepUpPolicyDecisionContext(TypedDict):
    """Represent deterministic context used for one step-up policy decision."""

    current_state: str
    action_type: str
    risk_class: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None


class StepUpPolicyDecisionEnvelope(TypedDict):
    """Represent canonical deterministic step-up policy output."""

    step_up_decision: StepUpDecision
    reason_code: str
    reason: str
    required_controls: list[str]
    decision_context: StepUpPolicyDecisionContext
    correlation_id: str | None
    trace_id: str | None


def evaluate_income_tax_step_up_auth_policy(
    *,
    current_state: str,
    action_type: str,
    risk_class: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
) -> StepUpPolicyDecisionEnvelope:
    """Return deterministic step-up requirement decision for one action request context."""

    context: StepUpPolicyDecisionContext = {
        "current_state": current_state,
        "action_type": action_type,
        "risk_class": risk_class,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
    }

    if current_state not in KNOWN_CONFIRMATION_STATES:
        return _decision(
            step_up_decision="unsupported_context",
            reason_code="invalid_confirmation_state",
            reason="Confirmation state is not supported by governed step-up auth policy.",
            required_controls=[],
            context=context,
            correlation_id=correlation_id,
        )

    if action_type not in KNOWN_ACTION_TYPES:
        return _decision(
            step_up_decision="unsupported_context",
            reason_code="unsupported_action_type",
            reason="Action type is not supported by governed step-up auth policy.",
            required_controls=[],
            context=context,
            correlation_id=correlation_id,
        )

    if risk_class not in KNOWN_RISK_CLASSES:
        return _decision(
            step_up_decision="unsupported_context",
            reason_code="unsupported_risk_class",
            reason="Risk class is not supported by governed step-up auth policy.",
            required_controls=[],
            context=context,
            correlation_id=correlation_id,
        )

    if action_type in SIDE_EFFECT_ACTION_TYPES and risk_class == "high":
        return _decision(
            step_up_decision="required",
            reason_code="step_up_required_for_high_risk_action",
            reason="High-risk side-effect-capable action requires step-up authorization.",
            required_controls=["step_up_auth"],
            context=context,
            correlation_id=correlation_id,
        )

    return _decision(
        step_up_decision="not_required",
        reason_code="step_up_not_required",
        reason="Step-up authorization is not required for this action context.",
        required_controls=[],
        context=context,
        correlation_id=correlation_id,
    )


def _decision(
    *,
    step_up_decision: StepUpDecision,
    reason_code: str,
    reason: str,
    required_controls: list[str],
    context: StepUpPolicyDecisionContext,
    correlation_id: str | None,
) -> StepUpPolicyDecisionEnvelope:
    return {
        "step_up_decision": step_up_decision,
        "reason_code": reason_code,
        "reason": reason,
        "required_controls": required_controls,
        "decision_context": context,
        "correlation_id": correlation_id,
        "trace_id": build_optional_trace_id(correlation_id),
    }
