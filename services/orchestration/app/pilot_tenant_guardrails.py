"""Evaluate deterministic pilot tenant allowlist guardrails for runtime boundaries."""

from __future__ import annotations

import json
from typing import cast
from typing import Literal
from typing import TypedDict
from pathlib import Path
from collections.abc import Mapping

from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.feature_flags import SUPPORTED_ORCHESTRATION_FEATURE_KEYS
from services.orchestration.app.feature_flags import SUPPORTED_ORCHESTRATION_ROLLOUT_STATES
from services.orchestration.app.trace_context import build_optional_trace_id

TenantGuardStatus = Literal["allowed", "blocked"]
TenantGuardScope = Literal["capability", "action", "orchestration_feature"]

ALLOWLIST_PATH = Path("contracts") / "capabilities" / "income_tax_pilot_tenant_allowlist.json"


class PilotTenantAllowlistError(RuntimeError):
    """Represent deterministic allowlist load/validation failures."""

    def __init__(self, *, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class PilotTenantGuardDecision(TypedDict):
    """Represent deterministic tenant-guard decision output."""

    guard_scope: TenantGuardScope
    guard_status: TenantGuardStatus
    reason_code: str
    reason: str
    required_controls: list[str]
    correlation_id: str | None
    trace_id: str | None
    tenant_id: str | None
    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    action_type: str | None
    feature_key: str | None
    rollout_state: str | None


def evaluate_income_tax_pilot_tenant_for_capability(
    *,
    tenant_id: str | None,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
) -> PilotTenantGuardDecision:
    """Evaluate one deterministic tenant allowlist decision for capability boundary."""

    trace_id = build_optional_trace_id(correlation_id)
    normalized_tenant_id = _normalize_tenant_id(tenant_id)
    if normalized_tenant_id is None:
        return _decision(
            guard_scope="capability",
            guard_status="blocked",
            reason_code="missing_tenant_context",
            reason="Tenant context is required for deterministic pilot capability execution.",
            required_controls=["provide_tenant_context"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=None,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=None,
            rollout_state=None,
        )

    tenant_entry, load_error_reason = _resolve_tenant_entry(normalized_tenant_id)
    if load_error_reason is not None:
        return _decision(
            guard_scope="capability",
            guard_status="blocked",
            reason_code=load_error_reason,
            reason="Pilot tenant allowlist could not be loaded deterministically.",
            required_controls=["review_tenant_allowlist_config"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=None,
            rollout_state=None,
        )

    if tenant_entry is None:
        return _decision(
            guard_scope="capability",
            guard_status="blocked",
            reason_code="tenant_not_allowlisted",
            reason="Tenant is not allowlisted for income-tax pilot capability execution.",
            required_controls=["request_pilot_allowlist"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=None,
            rollout_state=None,
        )

    if tenant_entry["status"] != "enabled":
        return _decision(
            guard_scope="capability",
            guard_status="blocked",
            reason_code="tenant_disabled",
            reason="Tenant is disabled in governed pilot allowlist.",
            required_controls=["contact_pilot_operator"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=None,
            rollout_state=None,
        )

    if not _is_allowed(
        allowed_values=tenant_entry["allowed_lanes"],
        requested_value=supported_lane_id,
    ):
        return _decision(
            guard_scope="capability",
            guard_status="blocked",
            reason_code="tenant_lane_not_allowed",
            reason="Tenant allowlist does not permit requested lane context.",
            required_controls=["use_allowed_tenant_scope"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=None,
            rollout_state=None,
        )

    return _decision(
        guard_scope="capability",
        guard_status="allowed",
        reason_code="pilot_tenant_allow",
        reason="Tenant is allowlisted for deterministic pilot capability execution.",
        required_controls=[],
        correlation_id=correlation_id,
        trace_id=trace_id,
        tenant_id=normalized_tenant_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        action_type=None,
        feature_key=None,
        rollout_state=None,
    )


def evaluate_income_tax_pilot_tenant_for_action(
    *,
    tenant_id: str | None,
    action_type: str | None,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
) -> PilotTenantGuardDecision:
    """Evaluate one deterministic tenant allowlist decision for action boundary."""

    trace_id = build_optional_trace_id(correlation_id)
    normalized_tenant_id = _normalize_tenant_id(tenant_id)
    if normalized_tenant_id is None:
        return _decision(
            guard_scope="action",
            guard_status="blocked",
            reason_code="missing_tenant_context",
            reason="Tenant context is required for deterministic pilot action handling.",
            required_controls=["provide_tenant_context"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=None,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=action_type,
            feature_key=None,
            rollout_state=None,
        )

    tenant_entry, load_error_reason = _resolve_tenant_entry(normalized_tenant_id)
    if load_error_reason is not None:
        return _decision(
            guard_scope="action",
            guard_status="blocked",
            reason_code=load_error_reason,
            reason="Pilot tenant allowlist could not be loaded deterministically.",
            required_controls=["review_tenant_allowlist_config"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=action_type,
            feature_key=None,
            rollout_state=None,
        )

    if tenant_entry is None:
        return _decision(
            guard_scope="action",
            guard_status="blocked",
            reason_code="tenant_not_allowlisted",
            reason="Tenant is not allowlisted for income-tax pilot action handling.",
            required_controls=["request_pilot_allowlist"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=action_type,
            feature_key=None,
            rollout_state=None,
        )

    if tenant_entry["status"] != "enabled":
        return _decision(
            guard_scope="action",
            guard_status="blocked",
            reason_code="tenant_disabled",
            reason="Tenant is disabled in governed pilot allowlist.",
            required_controls=["contact_pilot_operator"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=action_type,
            feature_key=None,
            rollout_state=None,
        )

    if not _is_allowed(
        allowed_values=tenant_entry["allowed_lanes"],
        requested_value=supported_lane_id,
    ):
        return _decision(
            guard_scope="action",
            guard_status="blocked",
            reason_code="tenant_lane_not_allowed",
            reason="Tenant allowlist does not permit requested lane context for action handling.",
            required_controls=["use_allowed_tenant_scope"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=action_type,
            feature_key=None,
            rollout_state=None,
        )

    if not _is_allowed(
        allowed_values=tenant_entry["allowed_actions"],
        requested_value=action_type,
    ):
        return _decision(
            guard_scope="action",
            guard_status="blocked",
            reason_code="tenant_action_not_allowed",
            reason="Tenant allowlist does not permit requested action type.",
            required_controls=["use_allowed_tenant_scope"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=action_type,
            feature_key=None,
            rollout_state=None,
        )

    return _decision(
        guard_scope="action",
        guard_status="allowed",
        reason_code="pilot_tenant_allow",
        reason="Tenant is allowlisted for deterministic pilot action handling.",
        required_controls=[],
        correlation_id=correlation_id,
        trace_id=trace_id,
        tenant_id=normalized_tenant_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        action_type=action_type,
        feature_key=None,
        rollout_state=None,
    )


def evaluate_orchestration_pilot_tenant_feature(
    *,
    tenant_id: str | None,
    feature_key: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None,
) -> PilotTenantGuardDecision:
    """Evaluate one deterministic tenant allowlist decision for orchestration features."""

    trace_id = build_optional_trace_id(correlation_id)
    normalized_tenant_id = _normalize_tenant_id(tenant_id)
    if normalized_tenant_id is None:
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="missing_tenant_context",
            reason="Tenant context is required for governed orchestration feature access.",
            required_controls=["provide_tenant_context"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=None,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=None,
        )

    tenant_entry, load_error_reason = _resolve_tenant_entry(normalized_tenant_id)
    if load_error_reason is not None:
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code=load_error_reason,
            reason="Pilot tenant allowlist could not be loaded deterministically.",
            required_controls=["review_tenant_allowlist_config"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=None,
        )

    if tenant_entry is None:
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="tenant_not_allowlisted",
            reason="Tenant is not allowlisted for governed orchestration feature access.",
            required_controls=["request_pilot_allowlist"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=None,
        )
    if tenant_entry["status"] != "enabled":
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="tenant_disabled",
            reason="Tenant is disabled in governed pilot allowlist.",
            required_controls=["contact_pilot_operator"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=None,
        )
    if not _is_allowed(
        allowed_values=tenant_entry["allowed_lanes"],
        requested_value=supported_lane_id,
    ):
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="tenant_lane_not_allowed",
            reason=(
                "Tenant allowlist does not permit requested lane context for "
                "orchestration feature access."
            ),
            required_controls=["use_allowed_tenant_scope"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=None,
        )

    rollout = tenant_entry["orchestration_rollout"]
    if rollout is None:
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="missing_orchestration_rollout_state",
            reason="Tenant is missing explicit orchestration rollout state.",
            required_controls=["configure_tenant_rollout_state"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=None,
        )

    rollout_state = rollout["rollout_state"]
    if rollout_state == "blocked":
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="tenant_rollout_blocked",
            reason="Tenant orchestration rollout state is explicitly blocked.",
            required_controls=["update_tenant_rollout_state"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=rollout_state,
        )

    if not _is_allowed(
        allowed_values=rollout["enabled_features"],
        requested_value=feature_key,
    ):
        return _decision(
            guard_scope="orchestration_feature",
            guard_status="blocked",
            reason_code="tenant_feature_not_enabled_for_rollout",
            reason="Tenant rollout state does not permit requested orchestration feature.",
            required_controls=["expand_tenant_feature_rollout"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            tenant_id=normalized_tenant_id,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
            action_type=None,
            feature_key=feature_key,
            rollout_state=rollout_state,
        )

    return _decision(
        guard_scope="orchestration_feature",
        guard_status="allowed",
        reason_code="pilot_tenant_allow",
        reason="Tenant is allowlisted for governed orchestration feature access.",
        required_controls=[],
        correlation_id=correlation_id,
        trace_id=trace_id,
        tenant_id=normalized_tenant_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        action_type=None,
        feature_key=feature_key,
        rollout_state=rollout_state,
    )


def load_income_tax_pilot_tenant_allowlist_document() -> dict[str, object]:
    """Load the governed pilot tenant allowlist document for orchestration release logic."""

    return _load_allowlist_document()


def list_income_tax_pilot_tenant_entries(
    *,
    allowlist_document: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Return normalized tenant allowlist entries for orchestration release logic."""

    allowlist = allowlist_document if allowlist_document is not None else _load_allowlist_document()
    return cast(list[dict[str, object]], _tenant_entries(allowlist))


class _TenantOrchestrationRolloutEntry(TypedDict):
    rollout_state: str
    enabled_features: list[str]


class _TenantAllowlistEntry(TypedDict):
    tenant_id: str
    status: str
    allowed_lanes: list[str]
    allowed_actions: list[str]
    orchestration_rollout: _TenantOrchestrationRolloutEntry | None


def _resolve_tenant_entry(
    tenant_id: str,
) -> tuple[_TenantAllowlistEntry | None, str | None]:
    try:
        allowlist = _load_allowlist_document()
    except PilotTenantAllowlistError as error:
        return None, error.reason

    tenant_entries = _tenant_entries(allowlist)
    for entry in tenant_entries:
        if entry["tenant_id"] == tenant_id:
            return entry, None
    return None, None


def _load_allowlist_document(repo_root: Path | None = None) -> dict[str, object]:
    target_root = repo_root if repo_root is not None else Path.cwd()
    allowlist_path = target_root / ALLOWLIST_PATH
    if not allowlist_path.exists():
        raise PilotTenantAllowlistError(
            reason="allowlist_not_found",
            message="Pilot tenant allowlist file is missing.",
        )

    try:
        raw = json.loads(allowlist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_json",
            message=(
                "Pilot tenant allowlist JSON is invalid "
                f"at line {error.lineno} column {error.colno}."
            ),
        ) from error

    if not isinstance(raw, dict):
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_shape",
            message="Pilot tenant allowlist must be a JSON object.",
        )

    allowlist = cast(dict[str, object], raw)
    _require_string(allowlist, "allowlist_version")
    scope = _require_string(allowlist, "capability_scope")
    if scope != "income_tax_vertical_slice":
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_scope",
            message="Pilot tenant allowlist capability_scope must be 'income_tax_vertical_slice'.",
        )
    _require_string(allowlist, "generated_at")
    _tenant_entries(allowlist)
    return allowlist


def _tenant_entries(allowlist: Mapping[str, object]) -> list[_TenantAllowlistEntry]:
    value = allowlist.get("tenants")
    if not isinstance(value, list):
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_tenants",
            message="Pilot tenant allowlist requires tenants list.",
        )
    entries: list[_TenantAllowlistEntry] = []
    for raw_entry in cast(list[object], value):
        if not isinstance(raw_entry, Mapping):
            raise PilotTenantAllowlistError(
                reason="invalid_allowlist_tenant_entry",
                message="Each pilot tenant allowlist entry must be a JSON object.",
            )
        entry = cast(Mapping[str, object], raw_entry)
        tenant_id = _require_string(entry, "tenant_id")
        status = _require_string(entry, "status")
        if status not in {"enabled", "disabled"}:
            raise PilotTenantAllowlistError(
                reason="invalid_allowlist_tenant_status",
                message=f"Pilot tenant status '{status}' is not supported.",
            )
        allowed_lanes = _string_list_with_wildcard(entry.get("allowed_lanes"))
        allowed_actions = _string_list_with_wildcard(entry.get("allowed_actions"))
        orchestration_rollout = _parse_orchestration_rollout(entry)
        entries.append(
            {
                "tenant_id": tenant_id,
                "status": status,
                "allowed_lanes": allowed_lanes,
                "allowed_actions": allowed_actions,
                "orchestration_rollout": orchestration_rollout,
            }
        )
    return entries


def _parse_orchestration_rollout(
    entry: Mapping[str, object],
) -> _TenantOrchestrationRolloutEntry | None:
    raw_rollout = entry.get("orchestration_rollout")
    if raw_rollout is None:
        return None
    if not isinstance(raw_rollout, Mapping):
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_rollout_shape",
            message="Tenant orchestration_rollout must be a JSON object.",
        )
    rollout = cast(Mapping[str, object], raw_rollout)
    rollout_state = _require_string(rollout, "rollout_state")
    if rollout_state not in SUPPORTED_ORCHESTRATION_ROLLOUT_STATES:
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_rollout_state",
            message=f"Unsupported tenant orchestration rollout_state '{rollout_state}'.",
        )
    enabled_features = _string_list_with_wildcard(rollout.get("enabled_features"))
    if "*" not in enabled_features:
        for feature_key in enabled_features:
            if feature_key not in SUPPORTED_ORCHESTRATION_FEATURE_KEYS:
                raise PilotTenantAllowlistError(
                    reason="invalid_allowlist_rollout_feature",
                    message=(
                        "Tenant orchestration_rollout enabled_features contains unsupported "
                        f"feature '{feature_key}'."
                    ),
                )
    return {
        "rollout_state": rollout_state,
        "enabled_features": enabled_features,
    }


def _string_list_with_wildcard(value: object) -> list[str]:
    if value is None:
        return ["*"]
    if not isinstance(value, list):
        raise PilotTenantAllowlistError(
            reason="invalid_allowlist_constraint_shape",
            message="Allowlist constraints must be list of strings.",
        )
    parsed: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item.strip():
            raise PilotTenantAllowlistError(
                reason="invalid_allowlist_constraint_item",
                message="Allowlist constraints must contain non-empty strings.",
            )
        parsed.append(item)
    if not parsed:
        return ["*"]
    return parsed


def _normalize_tenant_id(tenant_id: str | None) -> str | None:
    if tenant_id is None:
        return None
    normalized = tenant_id.strip()
    return normalized or None


def _is_allowed(*, allowed_values: list[str], requested_value: str | None) -> bool:
    if "*" in allowed_values:
        return True
    if requested_value is None:
        return False
    return requested_value in allowed_values


def _require_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise PilotTenantAllowlistError(
            reason="missing_required_field",
            message=f"Pilot tenant allowlist field '{field_name}' must be a non-empty string.",
        )
    return value


def _decision(
    *,
    guard_scope: TenantGuardScope,
    guard_status: TenantGuardStatus,
    reason_code: str,
    reason: str,
    required_controls: list[str],
    correlation_id: str | None,
    trace_id: str | None,
    tenant_id: str | None,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    action_type: str | None,
    feature_key: str | None,
    rollout_state: str | None,
) -> PilotTenantGuardDecision:
    decision: PilotTenantGuardDecision = {
        "guard_scope": guard_scope,
        "guard_status": guard_status,
        "reason_code": reason_code,
        "reason": reason,
        "required_controls": required_controls,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "action_type": action_type,
        "feature_key": feature_key,
        "rollout_state": rollout_state,
    }
    emit_income_tax_audit_event(
        event_type="pilot_tenant_guard_decision",
        status=guard_status,
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        context={
            "guard_scope": guard_scope,
            "reason_code": reason_code,
            "tenant_id": tenant_id,
            "action_type": action_type,
            "feature_key": feature_key,
            "rollout_state": rollout_state,
            "required_controls": required_controls,
        },
    )
    return decision
