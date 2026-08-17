"""Build explicit deterministic rejection envelopes for blocked income-tax action requests."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from collections.abc import Mapping

from services.orchestration.app.trace_context import build_optional_trace_id


class ActionRejectionContext(TypedDict):
    """Represent deterministic rejected context for one blocked action request."""

    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    action_type: str | None
    risk_class: str | None
    tenant_id: str | None


class ActionRejectionEnvelope(TypedDict):
    """Represent canonical deterministic rejection output for blocked action request handling."""

    error_code: str
    message: str
    reason_code: str
    reason: str
    rejected_context: ActionRejectionContext
    required_controls: list[str]
    next_allowed_actions: list[str]
    correlation_id: str | None
    trace_id: str | None


def build_income_tax_action_rejection(
    *,
    policy_decision: Mapping[str, object],
) -> ActionRejectionEnvelope | None:
    """Return canonical rejection envelope when policy decision is blocked or step-up-required."""

    decision = _optional_string(policy_decision.get("policy_decision"))
    if decision == "allowed":
        return None

    reason_code = _optional_string(policy_decision.get("reason_code")) or "invalid_policy_context"
    reason = _optional_string(policy_decision.get("reason")) or (
        "Action request was rejected by deterministic income-tax action policy."
    )
    rejected_context = _build_rejected_context(policy_decision)
    required_controls = _normalized_required_controls(policy_decision)
    correlation_id = _optional_string(policy_decision.get("correlation_id"))
    trace_id = _optional_string(policy_decision.get("trace_id")) or build_optional_trace_id(
        correlation_id
    )

    if reason_code == "confirmation_required":
        return {
            "error_code": "action_rejected_unconfirmed",
            "message": "Action request is blocked until confirmation is completed.",
            "reason_code": reason_code,
            "reason": reason,
            "rejected_context": rejected_context,
            "required_controls": required_controls or ["confirmation"],
            "next_allowed_actions": ["confirm", "reject", "revise_input"],
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        }

    if reason_code == "step_up_auth_required":
        return {
            "error_code": "action_rejected_step_up_required",
            "message": (
                "High-risk action is blocked until required step-up authorization is satisfied."
            ),
            "reason_code": reason_code,
            "reason": reason,
            "rejected_context": rejected_context,
            "required_controls": required_controls or ["step_up_auth"],
            "next_allowed_actions": ["request_step_up_auth", "reject", "revise_input"],
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        }

    if reason_code in {
        "capability_kill_switch_active",
        "action_kill_switch_active",
        "capability_disabled_by_flag",
        "action_disabled_by_flag",
        "unknown_capability_flag_config",
        "unknown_action_flag_config",
    }:
        return {
            "error_code": "action_rejected_safety_control",
            "message": "Action request is blocked by deterministic pilot safety controls.",
            "reason_code": reason_code,
            "reason": reason,
            "rejected_context": rejected_context,
            "required_controls": required_controls or ["review_pilot_safety_controls"],
            "next_allowed_actions": ["revise_input", "retry_when_enabled", "reject"],
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        }

    if reason_code in {
        "missing_tenant_context",
        "tenant_not_allowlisted",
        "tenant_disabled",
        "tenant_lane_not_allowed",
        "tenant_action_not_allowed",
        "allowlist_not_found",
        "invalid_allowlist_json",
        "invalid_allowlist_shape",
        "invalid_allowlist_scope",
        "invalid_allowlist_tenants",
        "invalid_allowlist_tenant_entry",
        "invalid_allowlist_tenant_status",
        "invalid_allowlist_constraint_shape",
        "invalid_allowlist_constraint_item",
    }:
        return {
            "error_code": "pilot_tenant_not_allowed",
            "message": "Tenant is not allowed for deterministic pilot action handling.",
            "reason_code": reason_code,
            "reason": reason,
            "rejected_context": rejected_context,
            "required_controls": required_controls or ["review_tenant_allowlist"],
            "next_allowed_actions": ["revise_input", "contact_pilot_operator", "reject"],
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        }

    return {
        "error_code": "action_rejected_invalid_context",
        "message": "Action request context is invalid for governed pilot action handling.",
        "reason_code": reason_code,
        "reason": reason,
        "rejected_context": rejected_context,
        "required_controls": required_controls or ["revise_action_context"],
        "next_allowed_actions": ["revise_input", "reject"],
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }


def _build_rejected_context(policy_decision: Mapping[str, object]) -> ActionRejectionContext:
    raw_context = policy_decision.get("decision_context")
    if not isinstance(raw_context, Mapping):
        raw_context = {}
    typed_context = cast(Mapping[str, object], raw_context)
    return {
        "supported_lane_id": _optional_string(typed_context.get("supported_lane_id")),
        "historical_version_id": _optional_string(typed_context.get("historical_version_id")),
        "tax_year": _optional_int(typed_context.get("tax_year")),
        "action_type": _optional_string(typed_context.get("action_type")),
        "risk_class": _optional_string(typed_context.get("risk_class")),
        "tenant_id": _optional_string(typed_context.get("tenant_id")),
    }


def _normalized_required_controls(policy_decision: Mapping[str, object]) -> list[str]:
    value = policy_decision.get("required_controls")
    if not isinstance(value, list):
        return []

    controls: list[str] = []
    for raw_control in cast(list[object], value):
        if not isinstance(raw_control, str):
            continue
        if raw_control == "confirmed_state":
            controls.append("confirmation")
        else:
            controls.append(raw_control)
    return controls


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None
