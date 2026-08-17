"""Evaluate deterministic feature-flag and kill-switch safety controls."""

from __future__ import annotations

from typing import cast
from typing import Literal
from typing import TypedDict
from collections.abc import Mapping

from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.feature_flags import SUPPORTED_ACTION_KEYS
from services.orchestration.app.feature_flags import get_runtime_safety_control_config
from services.orchestration.app.feature_flags import SUPPORTED_ORCHESTRATION_FEATURE_KEYS
from services.orchestration.app.trace_context import build_optional_trace_id

SafetyControlStatus = Literal["allowed", "blocked"]
SafetyControlScope = Literal["capability", "action", "orchestration"]


class SafetyControlDecision(TypedDict):
    """Represent deterministic runtime safety-control decision envelope."""

    control_scope: SafetyControlScope
    control_status: SafetyControlStatus
    reason_code: str
    reason: str
    required_controls: list[str]
    correlation_id: str | None
    trace_id: str | None
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None


def evaluate_income_tax_capability_safety_controls(
    *,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
    correlation_id: str | None,
) -> SafetyControlDecision:
    """Evaluate deterministic capability-level runtime safety controls."""

    config = get_runtime_safety_control_config()
    trace_id = build_optional_trace_id(correlation_id)

    if _switch_enabled(config, "global_capability"):
        return _decision(
            control_scope="capability",
            control_status="blocked",
            reason_code="capability_kill_switch_active",
            reason="Capability path is blocked by global pilot capability kill-switch.",
            required_controls=["disable_kill_switch"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    if _switch_enabled(config, f"capability:{supported_lane_id}"):
        return _decision(
            control_scope="capability",
            control_status="blocked",
            reason_code="capability_kill_switch_active",
            reason="Capability lane is blocked by targeted pilot capability kill-switch.",
            required_controls=["disable_kill_switch"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    capability_flags = config["capability_flags"]
    if supported_lane_id not in capability_flags:
        if config["default_fail_closed"]:
            return _decision(
                control_scope="capability",
                control_status="blocked",
                reason_code="unknown_capability_flag_config",
                reason=(
                    "Capability flag configuration is unknown and is blocked by fail-closed rule."
                ),
                required_controls=["update_feature_flags"],
                correlation_id=correlation_id,
                trace_id=trace_id,
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=tax_year,
            )

    if capability_flags.get(supported_lane_id) is False:
        return _decision(
            control_scope="capability",
            control_status="blocked",
            reason_code="capability_disabled_by_flag",
            reason="Capability lane is disabled by pilot runtime feature flag.",
            required_controls=["enable_capability_flag"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    return _decision(
        control_scope="capability",
        control_status="allowed",
        reason_code="safety_control_allow",
        reason="Capability lane is enabled by pilot runtime safety controls.",
        required_controls=[],
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )


def evaluate_income_tax_action_safety_controls(
    *,
    action_type: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
) -> SafetyControlDecision:
    """Evaluate deterministic action-level runtime safety controls."""

    config = get_runtime_safety_control_config()
    trace_id = build_optional_trace_id(correlation_id)

    if _switch_enabled(config, "global_action"):
        return _decision(
            control_scope="action",
            control_status="blocked",
            reason_code="action_kill_switch_active",
            reason="Action path is blocked by global pilot action kill-switch.",
            required_controls=["disable_kill_switch"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    if _switch_enabled(config, f"action:{action_type}"):
        return _decision(
            control_scope="action",
            control_status="blocked",
            reason_code="action_kill_switch_active",
            reason="Action is blocked by targeted pilot action kill-switch.",
            required_controls=["disable_kill_switch"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    action_flags = config["action_flags"]
    if action_type in SUPPORTED_ACTION_KEYS and action_type not in action_flags:
        if config["default_fail_closed"]:
            return _decision(
                control_scope="action",
                control_status="blocked",
                reason_code="unknown_action_flag_config",
                reason="Action flag configuration is unknown and is blocked by fail-closed rule.",
                required_controls=["update_feature_flags"],
                correlation_id=correlation_id,
                trace_id=trace_id,
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=tax_year,
            )

    if action_type in action_flags and action_flags.get(action_type) is False:
        return _decision(
            control_scope="action",
            control_status="blocked",
            reason_code="action_disabled_by_flag",
            reason="Action is disabled by pilot runtime feature flag.",
            required_controls=["enable_action_flag"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    return _decision(
        control_scope="action",
        control_status="allowed",
        reason_code="safety_control_allow",
        reason="Action is enabled by pilot runtime safety controls.",
        required_controls=[],
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )


def evaluate_orchestration_feature_safety_controls(
    *,
    feature_key: str,
    correlation_id: str | None,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
) -> SafetyControlDecision:
    """Evaluate deterministic orchestration-level runtime safety controls."""

    config = get_runtime_safety_control_config()
    trace_id = build_optional_trace_id(correlation_id)

    if _switch_enabled(config, "global_orchestration"):
        return _decision(
            control_scope="orchestration",
            control_status="blocked",
            reason_code="orchestration_kill_switch_active",
            reason="Orchestration feature is blocked by global orchestration kill-switch.",
            required_controls=["disable_kill_switch"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    if _switch_enabled(config, f"orchestration:{feature_key}"):
        return _decision(
            control_scope="orchestration",
            control_status="blocked",
            reason_code="orchestration_kill_switch_active",
            reason="Orchestration feature is blocked by targeted orchestration kill-switch.",
            required_controls=["disable_kill_switch"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    orchestration_flags = config["orchestration_flags"]
    if (
        feature_key in SUPPORTED_ORCHESTRATION_FEATURE_KEYS
        and feature_key not in orchestration_flags
    ):
        if config["default_fail_closed"]:
            return _decision(
                control_scope="orchestration",
                control_status="blocked",
                reason_code="unknown_orchestration_flag_config",
                reason=(
                    "Orchestration feature flag configuration is unknown and is blocked by "
                    "fail-closed rule."
                ),
                required_controls=["update_feature_flags"],
                correlation_id=correlation_id,
                trace_id=trace_id,
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=tax_year,
            )

    if feature_key in orchestration_flags and orchestration_flags.get(feature_key) is False:
        return _decision(
            control_scope="orchestration",
            control_status="blocked",
            reason_code="orchestration_disabled_by_flag",
            reason="Orchestration feature is disabled by runtime feature flag.",
            required_controls=["enable_orchestration_flag"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )

    return _decision(
        control_scope="orchestration",
        control_status="allowed",
        reason_code="safety_control_allow",
        reason="Orchestration feature is enabled by runtime safety controls.",
        required_controls=[],
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )


def _switch_enabled(config: Mapping[str, object], switch_key: str) -> bool:
    raw_kill_switches = config.get("kill_switches")
    if not isinstance(raw_kill_switches, Mapping):
        return False
    kill_switches = cast(Mapping[str, object], raw_kill_switches)
    value = kill_switches.get(switch_key)
    return value is True


def _decision(
    *,
    control_scope: SafetyControlScope,
    control_status: SafetyControlStatus,
    reason_code: str,
    reason: str,
    required_controls: list[str],
    correlation_id: str | None,
    trace_id: str | None,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
) -> SafetyControlDecision:
    decision: SafetyControlDecision = {
        "control_scope": control_scope,
        "control_status": control_status,
        "reason_code": reason_code,
        "reason": reason,
        "required_controls": required_controls,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
    }
    emit_income_tax_audit_event(
        event_type="safety_control_decision",
        status=control_status,
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        context={
            "control_scope": control_scope,
            "reason_code": reason_code,
            "required_controls": required_controls,
        },
    )
    return decision
