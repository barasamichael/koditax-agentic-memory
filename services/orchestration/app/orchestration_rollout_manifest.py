"""Deterministic rollout-manifest projection for orchestration release control."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
import hashlib

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.pilot_tenant_guardrails import list_income_tax_pilot_tenant_entries
from services.orchestration.app.pilot_tenant_guardrails import (
    load_income_tax_pilot_tenant_allowlist_document,
)


class RolloutManifestTenantEntry(TypedDict):
    """Represent one normalized tenant rollout entry."""

    tenant_id: str
    tenant_status: str
    rollout_state: str | None
    enabled_features: list[str]


class OrchestrationRolloutManifest(TypedDict):
    """Represent deterministic orchestration rollout manifest output."""

    manifest_id: str
    manifest_version: str
    allowlist_version: str
    rollout_status: str
    canary_tenants: list[str]
    general_availability_tenants: list[str]
    blocked_tenants: list[str]
    tenant_entries: list[RolloutManifestTenantEntry]
    blocking_reasons: list[str]


def build_orchestration_rollout_manifest(
    *,
    allowlist_document: dict[str, object] | None = None,
) -> OrchestrationRolloutManifest:
    """Build deterministic rollout manifest from governed tenant allowlist state."""

    allowlist = (
        allowlist_document
        if allowlist_document is not None
        else load_income_tax_pilot_tenant_allowlist_document()
    )
    allowlist_version = str(allowlist["allowlist_version"])
    tenants = list_income_tax_pilot_tenant_entries(allowlist_document=allowlist)

    canary_tenants: list[str] = []
    ga_tenants: list[str] = []
    blocked_tenants: list[str] = []
    tenant_entries: list[RolloutManifestTenantEntry] = []
    blocking_reasons: list[str] = []
    for tenant in tenants:
        tenant_id = str(tenant["tenant_id"])
        tenant_status = str(tenant["status"])
        rollout = tenant.get("orchestration_rollout")
        rollout_state: str | None = None
        enabled_features: list[str] = []
        if isinstance(rollout, dict):
            rollout_dict = cast(dict[str, object], rollout)
            rollout_state_value = rollout_dict.get("rollout_state")
            enabled_features_value = rollout_dict.get("enabled_features", [])
            if isinstance(rollout_state_value, str):
                rollout_state = rollout_state_value
            if isinstance(enabled_features_value, list):
                enabled_features = [
                    value
                    for value in cast(list[object], enabled_features_value)
                    if isinstance(value, str)
                ]
        elif tenant_status == "enabled":
            blocking_reasons.append(f"missing_rollout_state:{tenant_id}")

        if tenant_status == "disabled" or rollout_state == "blocked":
            blocked_tenants.append(tenant_id)
        elif rollout_state == "canary":
            canary_tenants.append(tenant_id)
        elif rollout_state == "general_availability":
            ga_tenants.append(tenant_id)
        elif tenant_status == "enabled":
            blocking_reasons.append(f"invalid_rollout_state:{tenant_id}")

        if tenant_status == "enabled" and rollout_state in {"canary", "general_availability"}:
            if not enabled_features:
                blocking_reasons.append(f"missing_rollout_features:{tenant_id}")

        tenant_entries.append(
            {
                "tenant_id": tenant_id,
                "tenant_status": tenant_status,
                "rollout_state": rollout_state,
                "enabled_features": enabled_features,
            }
        )

    if not canary_tenants and not ga_tenants:
        blocking_reasons.append("no_rollout_targets")
    rollout_status = "consistent" if not blocking_reasons else "inconsistent"
    manifest_core = {
        "allowlist_version": allowlist_version,
        "rollout_status": rollout_status,
        "canary_tenants": sorted(canary_tenants),
        "general_availability_tenants": sorted(ga_tenants),
        "blocked_tenants": sorted(blocked_tenants),
        "tenant_entries": tenant_entries,
        "blocking_reasons": sorted(blocking_reasons),
    }
    manifest_id = hashlib.sha256(canonical_json_dumps(manifest_core).encode("utf-8")).hexdigest()
    return {
        "manifest_id": manifest_id,
        "manifest_version": "1.0.0",
        "allowlist_version": allowlist_version,
        "rollout_status": rollout_status,
        "canary_tenants": sorted(canary_tenants),
        "general_availability_tenants": sorted(ga_tenants),
        "blocked_tenants": sorted(blocked_tenants),
        "tenant_entries": tenant_entries,
        "blocking_reasons": sorted(blocking_reasons),
    }
