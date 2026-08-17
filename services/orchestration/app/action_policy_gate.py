"""Evaluate deterministic action policy decisions for income-tax confirmation flows."""

from __future__ import annotations

from typing import Literal
from typing import TypedDict
import hashlib

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.trace_context import build_optional_trace_id
from services.orchestration.app.kill_switch_guard import evaluate_income_tax_action_safety_controls
from services.orchestration.app.step_up_auth_policy import StepUpDecision
from services.orchestration.app.step_up_auth_policy import evaluate_income_tax_step_up_auth_policy
from services.orchestration.app.pilot_tenant_guardrails import (
    evaluate_income_tax_pilot_tenant_for_action,
)

PolicyDecision = Literal["allowed", "blocked", "step_up_required"]
SIDE_EFFECT_ACTION_TYPES = {"submission_execute"}


class ActionPolicyDecisionContext(TypedDict):
    """Represent deterministic context used for one action policy decision."""

    current_state: str
    action_type: str
    risk_class: str
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    tenant_id: str | None
    principal_user_id: str
    action_reference_id: str
    step_up_purpose: str


class ActionPolicyDecisionEnvelope(TypedDict):
    """Represent deterministic policy decision envelope for one action request."""

    policy_decision: PolicyDecision
    step_up_decision: StepUpDecision
    step_up_reason_code: str
    reason_code: str
    reason: str
    required_controls: list[str]
    decision_context: ActionPolicyDecisionContext
    correlation_id: str | None
    trace_id: str | None


def evaluate_income_tax_action_policy(
    *,
    current_state: str,
    action_type: str,
    risk_class: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
    tenant_id: str | None = "pilot_tenant_alpha",
    principal_user_id: str | None = None,
    action_reference_id: str | None = None,
) -> ActionPolicyDecisionEnvelope:
    """Return deterministic policy decision by confirmation-state, action-type, and risk-class."""

    resolved_principal_user_id = _resolve_principal_user_id(
        principal_user_id=principal_user_id,
        correlation_id=correlation_id,
    )
    resolved_action_reference_id = _resolve_action_reference_id(
        action_reference_id=action_reference_id,
        action_type=action_type,
        risk_class=risk_class,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        principal_user_id=resolved_principal_user_id,
    )
    context: ActionPolicyDecisionContext = {
        "current_state": current_state,
        "action_type": action_type,
        "risk_class": risk_class,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "tenant_id": tenant_id,
        "principal_user_id": resolved_principal_user_id,
        "action_reference_id": resolved_action_reference_id,
        "step_up_purpose": "high_risk_action_step_up",
    }
    tenant_decision = evaluate_income_tax_pilot_tenant_for_action(
        tenant_id=tenant_id,
        action_type=action_type,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
    )
    if tenant_decision["guard_status"] != "allowed":
        return _decision(
            decision="blocked",
            step_up_decision="unsupported_context",
            step_up_reason_code=tenant_decision["reason_code"],
            reason_code=tenant_decision["reason_code"],
            reason=tenant_decision["reason"],
            required_controls=tenant_decision["required_controls"],
            context=context,
            correlation_id=correlation_id,
        )

    safety_decision = evaluate_income_tax_action_safety_controls(
        action_type=action_type,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
    )
    if safety_decision["control_status"] != "allowed":
        return _decision(
            decision="blocked",
            step_up_decision="unsupported_context",
            step_up_reason_code=safety_decision["reason_code"],
            reason_code=safety_decision["reason_code"],
            reason=safety_decision["reason"],
            required_controls=safety_decision["required_controls"],
            context=context,
            correlation_id=correlation_id,
        )

    step_up_policy = evaluate_income_tax_step_up_auth_policy(
        current_state=current_state,
        action_type=action_type,
        risk_class=risk_class,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
    )
    step_up_decision = step_up_policy["step_up_decision"]
    step_up_reason_code = step_up_policy["reason_code"]

    if step_up_decision == "unsupported_context":
        return _decision(
            decision="blocked",
            step_up_decision=step_up_decision,
            step_up_reason_code=step_up_reason_code,
            reason_code=step_up_reason_code,
            reason=step_up_policy["reason"],
            required_controls=step_up_policy["required_controls"],
            context=context,
            correlation_id=correlation_id,
        )

    if action_type in SIDE_EFFECT_ACTION_TYPES and current_state != "confirmed":
        return _decision(
            decision="blocked",
            step_up_decision=step_up_decision,
            step_up_reason_code=step_up_reason_code,
            reason_code="confirmation_required",
            reason="Side-effect-capable action requires confirmed confirmation state.",
            required_controls=["confirmed_state"],
            context=context,
            correlation_id=correlation_id,
        )

    if step_up_decision == "required":
        return _decision(
            decision="step_up_required",
            step_up_decision=step_up_decision,
            step_up_reason_code=step_up_reason_code,
            reason_code="step_up_auth_required",
            reason="High-risk side-effect-capable action requires step-up authorization.",
            required_controls=["step_up_auth"],
            context=context,
            correlation_id=correlation_id,
        )

    return _decision(
        decision="allowed",
        step_up_decision=step_up_decision,
        step_up_reason_code=step_up_reason_code,
        reason_code="policy_allow",
        reason="Action is allowed by governed deterministic action policy.",
        required_controls=[],
        context=context,
        correlation_id=correlation_id,
    )


def _decision(
    *,
    decision: PolicyDecision,
    step_up_decision: StepUpDecision,
    step_up_reason_code: str,
    reason_code: str,
    reason: str,
    required_controls: list[str],
    context: ActionPolicyDecisionContext,
    correlation_id: str | None,
) -> ActionPolicyDecisionEnvelope:
    trace_id = build_optional_trace_id(correlation_id)
    decision_envelope: ActionPolicyDecisionEnvelope = {
        "policy_decision": decision,
        "step_up_decision": step_up_decision,
        "step_up_reason_code": step_up_reason_code,
        "reason_code": reason_code,
        "reason": reason,
        "required_controls": required_controls,
        "decision_context": context,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    emit_income_tax_audit_event(
        event_type="policy_decision",
        status=decision,
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=context["supported_lane_id"],
        historical_version_id=context["historical_version_id"],
        tax_year=context["tax_year"],
        context={
            "reason_code": reason_code,
            "step_up_decision": step_up_decision,
            "step_up_reason_code": step_up_reason_code,
            "tenant_id": context["tenant_id"],
            "principal_user_id": context["principal_user_id"],
            "action_reference_id": context["action_reference_id"],
            "step_up_purpose": context["step_up_purpose"],
            "action_type": context["action_type"],
            "risk_class": context["risk_class"],
            "current_state": context["current_state"],
        },
    )
    return decision_envelope


def _resolve_principal_user_id(
    *,
    principal_user_id: str | None,
    correlation_id: str | None,
) -> str:
    if principal_user_id is not None and principal_user_id.strip():
        return principal_user_id
    if correlation_id is not None and correlation_id.strip():
        digest = _sha256_hex(correlation_id)[:24]
        return f"principal::{digest}"
    return "principal::anonymous"


def _resolve_action_reference_id(
    *,
    action_reference_id: str | None,
    action_type: str,
    risk_class: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
    tenant_id: str | None,
    principal_user_id: str,
) -> str:
    if action_reference_id is not None and action_reference_id.strip():
        return action_reference_id
    action_identity = {
        "scope": "income_tax_action_reference",
        "action_type": action_type,
        "risk_class": risk_class,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "principal_user_id": principal_user_id,
    }
    return _sha256_hex(canonical_json_dumps(action_identity))


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
